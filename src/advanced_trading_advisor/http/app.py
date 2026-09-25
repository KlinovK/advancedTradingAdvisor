"""Production PostgreSQL composition and private read-only HTTP routes."""

import os
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from advanced_trading_advisor.analysis.history_query import AnalysisHistoryQuery
from advanced_trading_advisor.analysis.repositories import AnalysisRunRepository
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.repositories import SnapshotRepository
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.settings import StorageBackend, StorageSettings

from .analysis_dtos import AnalysisDetailDTO, RecentAnalysesDTO
from .auth import APITokenSettings, UnauthorizedError, bearer_authentication
from .dtos import CurrentPortfolioDTO


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def create_app(
    *,
    environment: Mapping[str, str] | None = None,
    snapshots: SnapshotRepository | None = None,
    analyses: AnalysisRunRepository | None = None,
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
