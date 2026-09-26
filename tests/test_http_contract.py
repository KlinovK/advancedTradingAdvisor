"""OpenAPI parity and framework-error checks, in-process with no storage or network."""

import asyncio
import json

import httpx
import pytest

from advanced_trading_advisor.http.app import create_app

TOKEN = "synthetic-contract-token"
BASE = "/v1/portfolio"
OPERATIONS = {
    ("get", BASE): (200, "CurrentPortfolioDTO"),
    ("post", BASE + "/accounts"): (201, "CurrentPortfolioDTO"),
    ("patch", BASE + "/accounts/{account_id}"): (200, "CurrentPortfolioDTO"),
    ("put", BASE + "/accounts/{account_id}/holdings"): (200, "CurrentPortfolioDTO"),
    ("delete", BASE + "/accounts/{account_id}"): (200, "CurrentPortfolioDTO"),
    ("put", BASE + "/financial-settings"): (200, "CurrentPortfolioDTO"),
    ("put", BASE + "/core-positions/{symbol}"): (200, "CurrentPortfolioDTO"),
    ("post", BASE + "/orders"): (201, "CurrentPortfolioDTO"),
    ("post", BASE + "/orders/{order_id}/cancel"): (200, "CurrentPortfolioDTO"),
    ("post", BASE + "/orders/{order_id}/expire"): (200, "CurrentPortfolioDTO"),
    ("post", BASE + "/orders/{order_id}/confirm-filled"): (200, "CurrentPortfolioDTO"),
    ("get", "/v1/analyses/latest"): (200, "AnalysisDetailDTO"),
    ("get", "/v1/analyses"): (200, "RecentAnalysesDTO"),
    ("get", "/v1/analyses/{run_id}"): (200, "AnalysisDetailDTO"),
}


class NoStorage:
    async def get_current(self):
        raise AssertionError("Contract probes must not access storage.")


@pytest.fixture
def app():
    return create_app(environment={"ADVISOR_API_TOKEN": TOKEN}, snapshots=NoStorage())


def test_openapi_exact_surface_security_and_success_models(app):
    schema = app.openapi()
    actual = {(method, path) for path, methods in schema["paths"].items() for method in methods}
    assert actual == set(OPERATIONS)
    assert len(actual) == 14
    assert schema["components"]["securitySchemes"]["BearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
    }
    for (method, path), (status, model) in OPERATIONS.items():
        operation = schema["paths"][path][method]
        assert operation["security"] == [{"BearerAuth": []}]
        assert operation["responses"][str(status)]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{model}"
        }
        assert schema["components"]["schemas"][model]["properties"]
    assert TOKEN not in json.dumps(schema)


def test_openapi_errors_are_actual_envelopes_not_framework_validation(app):
    schema = app.openapi()
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    for method, path in OPERATIONS:
        responses = schema["paths"][path][method]["responses"]
        statuses = {"400", "401", "404", "409", "422", "500"} if method != "get" else {"401", "500"}
        assert statuses <= responses.keys()
        if method == "get":
            assert "409" not in responses
        for status, response in responses.items():
            if int(status) >= 400:
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/ErrorEnvelope"
                }
    detail = schema["components"]["schemas"]["ErrorDetail"]
    assert set(detail["required"]) == {"code", "message"}
    assert detail["properties"]["reload_required"]["const"] is True
    assert "portfolio_not_found" in detail["properties"]["code"]["enum"]
    assert "analysis_not_found" in detail["properties"]["code"]["enum"]


@pytest.mark.parametrize(
    "model,field,format_name",
    [
        ("CurrentPortfolioDTO", "snapshot_id", "uuid"),
        ("AccountDTO", "id", "uuid"),
        ("LimitOrderDTO", "account_id", "uuid"),
        ("AnalysisRunDTO", "run_id", "uuid"),
        ("RecommendationDTO", "recommendation_id", "uuid"),
        ("TradeSetupDTO", "id", "uuid"),
        ("CurrentPortfolioDTO", "confirmed_at", "date-time"),
        ("AnalysisResultDTO", "generated_at", "date-time"),
        ("TriggerTransitionDTO", "transitioned_at", "date-time"),
    ],
)
def test_openapi_wire_formats(app, model, field, format_name):
    value = app.openapi()["components"]["schemas"][model]["properties"][field]
    assert value["type"] == "string"
    assert value["format"] == format_name


@pytest.mark.parametrize(
    "model,field",
    [
        ("PositionDTO", "amount"),
        ("FinancialSettingsDTO", "monthly_expenses_usd"),
        ("CorePositionDTO", "hard_floor"),
        ("LimitOrderDTO", "quantity_asset"),
        ("FinancialSummaryDTO", "total_portfolio_usd"),
        ("MarketAssetDTO", "price_usd"),
        ("CandleDTO", "close"),
        ("TradeSetupDTO", "maximum_capital_usd"),
        ("TriggerContextDTO", "zone_low"),
        ("AssetDecisionDTO", "tradable_quantity"),
    ],
)
def test_openapi_financial_output_is_decimal_text(app, model, field):
    value = app.openapi()["components"]["schemas"][model]["properties"][field]
    assert value["type"] == "string"
    assert "decimal" in value["description"]


def test_openapi_nullable_enums_paths_and_strict_inputs(app):
    schema = app.openapi()
    models = schema["components"]["schemas"]
    for model, field in [
        ("LimitOrderDTO", "resolved_at"),
        ("AnalysisDetailDTO", "result"),
        ("AnalysisResultDTO", "active"),
        ("TradeSetupDTO", "trigger"),
        ("RecommendationDTO", "target_price"),
        ("TriggerContextDTO", "confirmation_at"),
    ]:
        assert {"type": "null"} in models[model]["properties"][field]["anyOf"]
        assert field in models[model]["required"]
    assert models["AccountDTO"]["properties"]["type"]["enum"] == [
        "binance",
        "external_wallet",
        "other_exchange",
        "manual",
    ]
    assert models["LimitOrderDTO"]["properties"]["side"]["enum"] == ["buy", "sell"]
    assert "AI_ASSISTED" in models["AnalysisResultDTO"]["properties"]["reasoning_mode"]["enum"]
    for name, model in models.items():
        if name.endswith("InputDTO"):
            assert model["additionalProperties"] is False
            if "expected_revision" in model["properties"]:
                assert "expected_revision" in model["required"]
                assert model["properties"]["expected_revision"]["exclusiveMinimum"] == 0
    for model, field in [
        ("AccountPositionInputDTO", "amount"),
        ("AddLimitOrderInputDTO", "target_price"),
        ("UpdateFinancialSettingsInputDTO", "target_expense_runway_months"),
        ("UpdateCorePositionInputDTO", "preferred_quantity"),
    ]:
        assert models[model]["properties"][field]["type"] == "string"
    for path, operations in schema["paths"].items():
        if any(key in path for key in ("{order_id}", "{account_id}", "{run_id}")):
            for operation in operations.values():
                assert operation["parameters"][0]["schema"]["format"] == "uuid"


def request(app, method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "method,path,content,status,code,message",
    [
        (
            "POST",
            BASE + "/accounts",
            b'{"private":',
            422,
            "invalid_request",
            "Invalid request parameters.",
        ),
        ("POST", BASE + "/accounts", b"\xff", 400, "invalid_request", "Invalid request body."),
        (
            "DELETE",
            BASE + "/accounts/not-uuid",
            b'{"expected_revision":1}',
            422,
            "invalid_request",
            "Invalid request parameters.",
        ),
        (
            "GET",
            "/v1/analyses?limit=nope",
            None,
            422,
            "invalid_request",
            "Invalid request parameters.",
        ),
        ("POST", BASE + "/accounts", b"{}", 422, "invalid_request", "Invalid request parameters."),
        (
            "POST",
            BASE + "/accounts",
            b'{"extra":"private"}',
            422,
            "invalid_request",
            "Invalid request parameters.",
        ),
        ("GET", "/unknown", None, 404, "not_found", "Route not found."),
        ("POST", BASE, None, 405, "method_not_allowed", "Method not allowed."),
        ("GET", BASE + "/", None, 404, "not_found", "Route not found."),
        ("POST", BASE + "/accounts/", b"{}", 404, "not_found", "Route not found."),
    ],
)
def test_normalized_framework_errors(app, method, path, content, status, code, message):
    response = request(
        app,
        method,
        path,
        content=content,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert response.json() == {"error": {"code": code, "message": message}}
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert TOKEN not in response.text
    assert "private" not in response.text
    if status == 405:
        assert response.headers["allow"] == "GET"


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/health", "/ready"])
def test_no_public_docs_or_extra_routes(app, path):
    response = request(app, "GET", path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.headers["cache-control"] == "no-store"


def test_unexpected_dependency_failure_is_sanitized(app):
    async def fail():
        raise RuntimeError("private credential and request body")

    auth = app.routes[0].dependencies[0].dependency
    app.dependency_overrides[auth] = fail
    response = request(app, "GET", BASE)
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Request could not be completed."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "bEaReR"])
def test_native_security_metadata_preserves_valid_case_insensitive_auth(scheme):
    class EmptyStorage:
        reads = 0

        async def get_current(self):
            self.reads += 1
            return None

    storage = EmptyStorage()
    app = create_app(environment={"ADVISOR_API_TOKEN": TOKEN}, snapshots=storage)
    response = request(app, "GET", BASE, headers={"Authorization": f"{scheme} {TOKEN}"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "portfolio_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert storage.reads == 1


def test_extra_field_in_otherwise_valid_body_never_reaches_storage(app):
    response = request(
        app,
        "POST",
        BASE + "/accounts",
        json={
            "expected_revision": 1,
            "name": "Synthetic",
            "account_type": "manual",
            "positions": [],
            "unexpected": "private value",
        },
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
