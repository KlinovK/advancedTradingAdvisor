import json
from datetime import UTC, datetime
from functools import wraps
from uuid import UUID

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.domain.numeric import parse_decimal
from advanced_trading_advisor.domain.persistence import (
    ConfirmedUpdate,
    FailureCategory,
    PayloadError,
    StoredAnalysisRun,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import timestamp
from advanced_trading_advisor.portfolio.models import (
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    CorePosition,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)

PAYLOAD_VERSION = 1
SNAPSHOT_PAYLOAD_VERSION = 3


def boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as error:
            raise PayloadError("Malformed or unsupported versioned payload.") from error

    return wrapped


def utc_text(value: datetime) -> str:
    timestamp(value)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamp must be text.")
    result = datetime.fromisoformat(value)
    timestamp(result)
    return result.astimezone(UTC)


def uid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("UUID must be text.")
    result = UUID(value)
    if str(result) != value:
        raise ValueError("UUID must be canonical.")
    return result


def strict_object(value: object, keys: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("Unexpected payload shape.")
    return value


def array(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("Array required.")
    return value


def no_float(_value):
    raise ValueError("JSON floating-point values are prohibited.")


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def load(text: str) -> dict:
    if not isinstance(text, str):
        raise ValueError("JSON must be text.")
    value = json.loads(text, parse_float=no_float, parse_constant=no_float, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object.")
    return value


def dump(value: dict) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def envelope(kind: str, data: dict) -> str:
    return dump({"schema_version": PAYLOAD_VERSION, "kind": kind, "data": data})


def unpack(text: str, kind: str) -> dict:
    value = strict_object(load(text), {"schema_version", "kind", "data"})
    if type(value["schema_version"]) is not int or value["schema_version"] != PAYLOAD_VERSION:
        raise ValueError("Unsupported payload version.")
    if value["kind"] != kind or not isinstance(value["data"], dict):
        raise ValueError("Invalid payload kind.")
    return value["data"]


@boundary
def validate_result(text: str | None) -> str | None:
    """Future results use a versioned object and decimal strings; no opaque binary payload."""
    if text is None:
        return None
    value = strict_object(load(text), {"schema_version", "data"})
    if type(value["schema_version"]) is int and value["schema_version"] in {2, 3, 4, 5}:
        # Typed report v2 adds reasoning provenance; row/snapshot/DB versions stay unchanged.
        from advanced_trading_advisor.analysis.result import AnalysisResultCodec

        AnalysisResultCodec.decode(text)
        return dump(value)
    if type(value["schema_version"]) is not int or value["schema_version"] != PAYLOAD_VERSION:
        raise ValueError("Unsupported result version.")
    if not isinstance(value["data"], dict):
        raise ValueError("Result data must be an object.")
    return dump(value)


class SnapshotCodec:
    @staticmethod
    @boundary
    def encode(snapshot: ConfirmedPortfolioSnapshot) -> str:
        if not isinstance(snapshot, ConfirmedPortfolioSnapshot):
            raise ValueError("Snapshot required.")
        return dump(
            {
                "schema_version": SNAPSHOT_PAYLOAD_VERSION,
                "kind": "snapshot",
                "data": {
                    "id": str(snapshot.id),
                    "confirmed_at": utc_text(snapshot.confirmed_at),
                    "portfolio": {
                        "accounts": [
                            {
                                "id": str(account.id),
                                "name": account.name,
                                "type": account.type.value,
                                "positions": [
                                    {"symbol": p.symbol, "amount": format(p.amount, "f")}
                                    for p in account.positions
                                ],
                            }
                            for account in snapshot.portfolio.accounts
                        ]
                    },
                    "financial_settings": {
                        "monthly_expenses_usd": format(
                            snapshot.financial_settings.monthly_expenses_usd, "f"
                        ),
                        "target_expense_runway_months": format(
                            snapshot.financial_settings.target_expense_runway_months, "f"
                        ),
                    },
                    "orders": [
                        {
                            "id": str(o.id),
                            "asset": o.asset,
                            "side": o.side.value,
                            "target_price": format(o.target_price, "f"),
                            "quantity_asset": format(o.quantity_asset, "f"),
                            "status": o.status.value,
                            "created_at": utc_text(o.created_at),
                            "updated_at": utc_text(o.updated_at),
                            "resolved_at": utc_text(o.resolved_at)
                            if o.resolved_at is not None
                            else None,
                        }
                        for o in snapshot.orders
                    ],
                    "core_positions": [
                        {
                            "symbol": c.symbol,
                            "hard_floor": format(c.hard_floor, "f"),
                            "preferred_quantity": format(c.preferred_quantity, "f"),
                            "policy_version": c.policy_version,
                        }
                        for c in snapshot.core_positions
                    ],
                },
            },
        )

    @staticmethod
    @boundary
    def decode(text: str) -> ConfirmedPortfolioSnapshot:
        root = strict_object(load(text), {"schema_version", "kind", "data"})
        if (
            root["kind"] != "snapshot"
            or type(root["schema_version"]) is not int
            or root["schema_version"] != SNAPSHOT_PAYLOAD_VERSION
        ):
            raise ValueError("Unsupported snapshot payload.")
        data = strict_object(
            root["data"],
            {"id", "confirmed_at", "portfolio", "financial_settings", "orders", "core_positions"},
        )
        portfolio = strict_object(data["portfolio"], {"accounts"})
        accounts = []
        for item in array(portfolio["accounts"]):
            account = strict_object(item, {"id", "name", "type", "positions"})
            positions = []
            for entry in array(account["positions"]):
                position = strict_object(entry, {"symbol", "amount"})
                positions.append(
                    AssetPosition(
                        position["symbol"], parse_decimal(position["amount"], positive=True)
                    )
                )
            accounts.append(
                Account(
                    uid(account["id"]),
                    account["name"],
                    AccountType(account["type"]),
                    tuple(positions),
                )
            )
        settings = strict_object(
            data["financial_settings"], {"monthly_expenses_usd", "target_expense_runway_months"}
        )
        orders = []
        for item in array(data["orders"]):
            o = strict_object(
                item,
                {
                    "id",
                    "asset",
                    "side",
                    "target_price",
                    "quantity_asset",
                    "status",
                    "created_at",
                    "updated_at",
                    "resolved_at",
                },
            )
            orders.append(
                LimitOrder(
                    uid(o["id"]),
                    o["asset"],
                    TradingSide(o["side"]),
                    parse_decimal(o["target_price"], positive=True),
                    parse_decimal(o["quantity_asset"], positive=True),
                    LimitOrderStatus(o["status"]),
                    date(o["created_at"]),
                    date(o["updated_at"]),
                    date(o["resolved_at"]) if o["resolved_at"] is not None else None,
                )
            )
        cores = []
        for item in array(data["core_positions"]):
            c = strict_object(
                item, {"symbol", "hard_floor", "preferred_quantity", "policy_version"}
            )
            cores.append(
                CorePosition(
                    c["symbol"],
                    parse_decimal(c["hard_floor"], positive=True),
                    parse_decimal(c["preferred_quantity"], positive=True),
                    c["policy_version"],
                )
            )
        return ConfirmedPortfolioSnapshot(
            uid(data["id"]),
            date(data["confirmed_at"]),
            Portfolio(tuple(accounts)),
            FinancialSettings(
                parse_decimal(settings["monthly_expenses_usd"], positive=True),
                parse_decimal(settings["target_expense_runway_months"], positive=True),
            ),
            tuple(orders),
            tuple(cores),
        )


class AnalysisRunCodec:
    @staticmethod
    @boundary
    def encode(run: AnalysisRun) -> str:
        if not isinstance(run, AnalysisRun):
            raise ValueError("AnalysisRun required.")
        return envelope(
            "analysis_run",
            {
                "run_id": str(run.run_id),
                "snapshot_id": str(run.snapshot_id),
                "status": run.status.value,
                "started_at": utc_text(run.started_at),
                "completed_at": utc_text(run.completed_at)
                if run.completed_at is not None
                else None,
                "market_cutoff": utc_text(run.market_cutoff)
                if run.market_cutoff is not None
                else None,
            },
        )

    @staticmethod
    @boundary
    def decode(text: str) -> AnalysisRun:
        d = strict_object(
            unpack(text, "analysis_run"),
            {"run_id", "snapshot_id", "status", "started_at", "completed_at", "market_cutoff"},
        )
        return AnalysisRun(
            uid(d["run_id"]),
            uid(d["snapshot_id"]),
            date(d["started_at"]),
            AnalysisRunStatus(d["status"]),
            date(d["completed_at"]) if d["completed_at"] is not None else None,
            date(d["market_cutoff"]) if d["market_cutoff"] is not None else None,
        )

    @staticmethod
    @boundary
    def from_record(record: dict) -> StoredAnalysisRun:
        if type(record["schema_version"]) is not int or record["schema_version"] != PAYLOAD_VERSION:
            raise ValueError("Unsupported analysis row version.")
        run = AnalysisRunCodec.decode(
            envelope(
                "analysis_run",
                {
                    key: record[key]
                    for key in (
                        "run_id",
                        "snapshot_id",
                        "status",
                        "started_at",
                        "completed_at",
                        "market_cutoff",
                    )
                },
            )
        )
        return StoredAnalysisRun(
            run,
            validate_result(record["result_json"]),
            FailureCategory(record["failure_category"])
            if record["failure_category"] is not None
            else None,
        )


class UpdateCodec:
    @staticmethod
    @boundary
    def encode(update: ConfirmedUpdate) -> str:
        if not isinstance(update, ConfirmedUpdate):
            raise ValueError("ConfirmedUpdate required.")
        return envelope(
            "confirmed_update",
            {
                "update_id": str(update.update_id),
                "update_type": update.update_type.value,
                "previous_snapshot_id": str(update.previous_snapshot_id)
                if update.previous_snapshot_id is not None
                else None,
                "new_snapshot_id": str(update.new_snapshot_id),
                "confirmed_at": utc_text(update.confirmed_at),
            },
        )

    @staticmethod
    @boundary
    def decode(text: str) -> ConfirmedUpdate:
        d = strict_object(
            unpack(text, "confirmed_update"),
            {"update_id", "update_type", "previous_snapshot_id", "new_snapshot_id", "confirmed_at"},
        )
        return ConfirmedUpdate(
            uid(d["update_id"]),
            UpdateType(d["update_type"]),
            uid(d["previous_snapshot_id"]) if d["previous_snapshot_id"] is not None else None,
            uid(d["new_snapshot_id"]),
            date(d["confirmed_at"]),
        )
