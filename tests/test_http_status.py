"""Offline checks for public HTTP liveness and fail-closed PostgreSQL readiness."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from advanced_trading_advisor.http import app as http_app
from advanced_trading_advisor.http import serve
from advanced_trading_advisor.http.app import create_app
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_schema import (
    FINANCIAL_FUNCTION_CONTRACT,
    FINANCIAL_TRIGGER_CONTRACT,
    POSTGRES_COLUMNS,
    POSTGRES_SCHEMA_VERSION,
)

TOKEN = "synthetic-status-token"


class NoStorage:
    async def get_current(self):
        raise AssertionError("Status routes must not read portfolio state.")


def request(app, path, *, headers=None):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(run())


def status_app(check=None):
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=NoStorage(),
        readiness_check=check,
    )


def test_health_is_public_minimal_and_independent_of_storage():
    async def forbidden():
        raise AssertionError("Health must not check the database.")

    app = status_app(forbidden)
    response = request(app, "/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    assert app.openapi()["paths"]["/health"]["get"].get("security") is None


@pytest.mark.parametrize("authorization", [None, "invalid", "Bearer wrong"])
def test_ready_is_public_and_does_not_require_a_portfolio(authorization):
    calls = []

    async def ready():
        calls.append("schema")

    headers = {"Authorization": authorization} if authorization else None
    response = request(status_app(ready), "/ready", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert calls == ["schema"]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("private database URL"), TimeoutError("private credential")],
)
def test_ready_failure_is_sanitized(failure):
    async def unavailable():
        raise failure

    response = request(status_app(unavailable), "/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text


def test_ready_fails_closed_without_a_database_dependency():
    response = request(status_app(), "/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


@pytest.mark.parametrize(
    "version",
    [None, POSTGRES_SCHEMA_VERSION - 1, POSTGRES_SCHEMA_VERSION + 1, POSTGRES_SCHEMA_VERSION],
)
def test_postgres_readiness_checks_schema_in_read_only_transaction(monkeypatch, version):
    events = []

    class Connection:
        @asynccontextmanager
        async def transaction(self, *, readonly=False):
            events.append(("transaction", readonly))
            yield self

        async def fetchval(self, query, *_args):
            if query.startswith("SELECT version FROM schema_metadata"):
                if version is None:
                    raise RuntimeError("schema_metadata absent; private connection detail")
                return version
            assert "to_regprocedure" in query
            return "present"

        async def fetch(self, query, *_args):
            if "information_schema.columns" in query:
                return [
                    {"table_name": table, "column_name": column}
                    for table, columns in POSTGRES_COLUMNS.items()
                    for column in columns
                ]
            if "pg_trigger" in query:
                return [
                    {
                        "table_name": table,
                        "trigger_name": trigger,
                        "enabled": "O",
                        "tgtype": spec[0],
                        "function_name": spec[1],
                        "function_in_schema": True,
                        "is_constraint": spec[2],
                        "deferrable": spec[3],
                        "initially_deferred": spec[4],
                    }
                    for (table, trigger), spec in FINANCIAL_TRIGGER_CONTRACT.items()
                ]
            assert "pg_proc" in query
            return [
                {
                    "function_name": name,
                    "language": spec[0],
                    "return_type": spec[1],
                    "volatility": spec[2],
                    "strict": spec[3],
                    "source": spec[4],
                    "security_definer": False,
                }
                for name, spec in FINANCIAL_FUNCTION_CONTRACT.items()
            ]

    @asynccontextmanager
    async def connection(_self):
        events.append(("connection", None))
        yield Connection()

    monkeypatch.setattr(PostgresDatabase, "connection", connection)
    database = PostgresDatabase("postgresql://placeholder.invalid/test")
    response = request(status_app(database.check_ready), "/ready")
    assert response.status_code == (200 if version == POSTGRES_SCHEMA_VERSION else 503)
    assert response.json() == {
        "status": "ready" if version == POSTGRES_SCHEMA_VERSION else "not_ready"
    }
    assert events == [("connection", None), ("transaction", True)]
    assert "private" not in response.text


def test_readiness_has_a_finite_outer_timeout(monkeypatch):
    import advanced_trading_advisor.storage.postgres_database as module

    @asynccontextmanager
    async def stalled(_self):
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(module.PostgresDatabase, "connection", stalled)
    monkeypatch.setattr(module, "READINESS_TIMEOUT_SECONDS", 0.01)
    database = PostgresDatabase("postgresql://placeholder.invalid/test")
    response = request(status_app(database.check_ready), "/ready")
    assert response.status_code == 503


def test_legacy_readiness_accepts_v3_without_initializing(monkeypatch):
    from advanced_trading_advisor.storage.postgres_schema import POSTGRES_LEGACY_COLUMNS

    class Connection:
        @asynccontextmanager
        async def transaction(self, *, readonly=False):
            assert readonly
            yield self

        async def fetchval(self, query, *_args):
            return 3 if "schema_metadata" in query else None

        async def fetch(self, query):
            assert "information_schema.columns" in query
            return [
                {"table_name": table, "column_name": column}
                for table, columns in POSTGRES_LEGACY_COLUMNS.items()
                for column in columns
            ]

    @asynccontextmanager
    async def connection(_self):
        yield Connection()

    monkeypatch.setattr(PostgresDatabase, "connection", connection)
    response = request(
        status_app(PostgresDatabase("postgresql://placeholder.invalid/test").check_legacy_ready),
        "/ready",
    )
    assert response.status_code == 200


def test_http_factory_validates_config_without_connecting_or_using_providers(monkeypatch):
    events = []

    class Database:
        async def check_legacy_ready(self):
            events.append("ready")

        async def initialize(self):
            raise AssertionError("HTTP startup must not migrate.")

    def storage(_settings):
        events.append("constructed")
        return SimpleNamespace(database=Database(), snapshots=NoStorage(), analyses=None)

    monkeypatch.setattr(http_app, "create_storage", storage)
    environment = {
        "ADVISOR_API_TOKEN": TOKEN,
        "ADVISOR_STORAGE_BACKEND": "postgres",
        "ADVISOR_DATABASE_URL": "postgresql://placeholder.invalid/test",
    }
    app = create_app(environment=environment)
    assert events == ["constructed"]
    assert request(app, "/ready").json() == {"status": "ready"}
    assert events == ["constructed", "ready"]
    with pytest.raises(ValueError):
        create_app(environment={**environment, "ADVISOR_API_TOKEN": ""})
    with pytest.raises(RuntimeError, match="HTTP storage configuration invalid"):
        create_app(environment={"ADVISOR_API_TOKEN": TOKEN})
    assert events == ["constructed", "ready"]


@pytest.mark.parametrize("authorization", [None, "Basic bad", "Bearer wrong", f"Bearer {TOKEN}"])
def test_product_authentication_unchanged(authorization):
    headers = {"Authorization": authorization} if authorization else None
    response = request(status_app(), "/v1/portfolio", headers=headers)
    if authorization == f"Bearer {TOKEN}":
        assert response.status_code == 500  # Auth passed; NoStorage intentionally fails.
    else:
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


def test_status_routes_do_not_enable_public_openapi():
    app = status_app()
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert request(app, path).status_code == 404
    assert app.openapi()["paths"]["/ready"]["get"].get("security") is None
    assert (
        sum(
            len(methods)
            for path, methods in app.openapi()["paths"].items()
            if path.startswith("/v1/")
        )
        == 14
    )


def test_render_configuration_and_launcher_are_safe(monkeypatch):
    blueprint = (Path(__file__).resolve().parents[1] / "render.yaml").read_text()
    assert "healthCheckPath: /health" in blueprint
    assert "startCommand: python -m advanced_trading_advisor.http.serve" in blueprint
    assert "ADVISOR_STORAGE_BACKEND" in blueprint
    assert "ADVISOR_DATABASE_URL" in blueprint
    assert "ADVISOR_API_TOKEN" in blueprint
    assert blueprint.count("sync: false") == 2
    assert not any(
        word in blueprint.lower() for word in ("migrate", "setup", "reset", "password", "secret")
    )
    assert (Path(__file__).resolve().parents[1] / ".python-version").read_text().strip() == "3.13"

    calls = []
    monkeypatch.setenv("PORT", "12567")
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    serve.main()
    assert calls == [
        (
            ("advanced_trading_advisor.http.app:create_app",),
            {"factory": True, "host": "0.0.0.0", "port": 12567},
        )
    ]
    for value in ("", "0", "65536", "not-a-port"):
        monkeypatch.setenv("PORT", value)
        with pytest.raises(SystemExit, match="PORT must be"):
            serve.main()
    assert len(calls) == 1
    assert TOKEN not in json.dumps(calls)
