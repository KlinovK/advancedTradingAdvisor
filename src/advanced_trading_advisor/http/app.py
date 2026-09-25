"""Production PostgreSQL composition and private HTTP portfolio/analysis routes."""

import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from advanced_trading_advisor.analysis.history_query import AnalysisHistoryQuery
from advanced_trading_advisor.analysis.repositories import AnalysisRunRepository
from advanced_trading_advisor.domain.numeric import parse_decimal
from advanced_trading_advisor.domain.persistence import RevisionConflictError, StateConflictError
from advanced_trading_advisor.domain.validation import DomainValidationError, ResourceNotFoundError
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.create_account import CreateAccount, CreateAccountCommand
from advanced_trading_advisor.portfolio.models import AssetPosition
from advanced_trading_advisor.portfolio.repositories import SnapshotRepository
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.settings import StorageBackend, StorageSettings

from .account_dtos import CreateAccountInputDTO
from .analysis_dtos import AnalysisDetailDTO, RecentAnalysesDTO
from .auth import APITokenSettings, UnauthorizedError, bearer_authentication
from .dtos import CurrentPortfolioDTO


def error_response(
    status: int, code: str, message: str, *, reload_required: bool = False
) -> JSONResponse:
    detail: dict[str, str | bool] = {"code": code, "message": message}
    if reload_required:
        detail["reload_required"] = True
    return JSONResponse(
        status_code=status,
        content={"error": detail},
        headers={"Cache-Control": "no-store"},
    )


def write_error_response(error: Exception) -> JSONResponse:
    """Map allowlisted application error types without exposing exception text."""
    if isinstance(error, ResourceNotFoundError):
        return error_response(404, "resource_not_found", "Portfolio resource not found.")
    if isinstance(error, RevisionConflictError):
        return error_response(
            409,
            "revision_conflict",
            "Portfolio changed; reload before confirming.",
            reload_required=True,
        )
    if isinstance(error, StateConflictError):
        return error_response(409, "state_conflict", "Mutation conflicts with portfolio state.")
    if isinstance(error, DomainValidationError):
        return error_response(422, "validation_error", "Invalid portfolio input.")
    return error_response(500, "internal_error", "Portfolio could not be updated.")


def create_app(
    *,
    environment: Mapping[str, str] | None = None,
    snapshots: SnapshotRepository | None = None,
    analyses: AnalysisRunRepository | None = None,
    create_account: CreateAccount | None = None,
) -> FastAPI:
    """Compose without connecting, initializing, or migrating storage.

    Production reads the process environment only; tests may inject a repository.
    """
    values = dict(os.environ if environment is None else environment)
    token_settings = APITokenSettings.from_environment(values)
    if snapshots is None:
        try:
            storage_settings = StorageSettings.from_environment(
                values, sqlite_path=Path("unused-http-sqlite-path")
            )
            if storage_settings.backend is not StorageBackend.POSTGRES:
                raise ValueError("PostgreSQL is required for HTTP production storage.")
            storage = create_storage(storage_settings)
            snapshots = storage.snapshots
            if analyses is None:
                analyses = storage.analyses
        except (TypeError, ValueError):
            raise RuntimeError("HTTP storage configuration invalid.") from None

    state = ConfirmedStateService(snapshots)
    account_creator = create_account if create_account is not None else CreateAccount(snapshots)
    history = AnalysisHistoryQuery(analyses) if analyses is not None else None
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    authenticate = bearer_authentication(token_settings)

    @app.exception_handler(UnauthorizedError)
    async def unauthorized(_request: Request, _error: UnauthorizedError) -> JSONResponse:
        response = error_response(401, "unauthorized", "Authentication required.")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_request", "Invalid request parameters.")

    @app.get("/v1/portfolio", dependencies=[Depends(authenticate)])
    async def get_portfolio() -> JSONResponse:
        try:
            current = await state.load_current()
            if current is None:
                return error_response(404, "portfolio_not_found", "No confirmed portfolio exists.")
            payload = asdict(CurrentPortfolioDTO.from_domain(current))
        except Exception:
            # Storage and decoding errors do not reliably distinguish availability from corruption.
            # Do not return or log exception text, SQL details, or private portfolio state.
            return error_response(500, "internal_error", "Portfolio could not be loaded.")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post("/v1/portfolio/accounts", dependencies=[Depends(authenticate)], status_code=201)
    async def create_portfolio_account(body: CreateAccountInputDTO) -> JSONResponse:
        try:
            command = CreateAccountCommand(
                expected_revision=body.expected_revision,
                account_id=uuid4(),
                name=body.name,
                account_type=body.account_type,
                positions=tuple(
                    AssetPosition(position.symbol, parse_decimal(position.amount, positive=True))
                    for position in body.positions
                ),
            )
            current = await account_creator.execute(command)
            payload = asdict(CurrentPortfolioDTO.from_domain(current))
        except Exception as error:
            return write_error_response(error)
        return JSONResponse(payload, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/v1/analyses/latest", dependencies=[Depends(authenticate)])
    async def latest_analysis() -> JSONResponse:
        try:
            if history is None:
                raise RuntimeError("Analysis history is not configured.")
            detail = await history.latest_completed()
            if detail is None:
                return error_response(404, "analysis_not_found", "Analysis not found.")
            payload = asdict(AnalysisDetailDTO.from_domain(detail))
        except Exception:
            return error_response(500, "internal_error", "Analysis could not be loaded.")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/v1/analyses", dependencies=[Depends(authenticate)])
    async def recent_analyses(limit: int = Query(..., gt=0)) -> JSONResponse:
        try:
            if history is None:
                raise RuntimeError("Analysis history is not configured.")
            payload = asdict(RecentAnalysesDTO.from_domain(await history.recent(limit)))
        except Exception:
            return error_response(500, "internal_error", "Analysis could not be loaded.")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/v1/analyses/{run_id}", dependencies=[Depends(authenticate)])
    async def analysis_detail(run_id: UUID) -> JSONResponse:
        if run_id.int == 0:
            return error_response(422, "invalid_request", "Invalid request parameters.")
        try:
            if history is None:
                raise RuntimeError("Analysis history is not configured.")
            detail = await history.detail(run_id)
            if detail is None:
                return error_response(404, "analysis_not_found", "Analysis not found.")
            payload = asdict(AnalysisDetailDTO.from_domain(detail))
        except Exception:
            return error_response(500, "internal_error", "Analysis could not be loaded.")
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    return app
