"""Strict wire schema + independent decoding; financial JSON numbers are never accepted."""

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from advanced_trading_advisor.analysis.recommendations import ActionType, Recommendation
from advanced_trading_advisor.domain.numeric import parse_decimal
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.portfolio.models import TradingSide


def nullable(kind):
    return {"type": [kind, "null"]}


SCHEMA_VERSION = 3

ACTION_PROPERTIES = {
    "asset": nullable("string"),
    "action_type": {
        "type": "string",
        "enum": [a.value for a in ActionType if a is not ActionType.REBALANCE],
    },
    "side": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
    "existing_order_id": nullable("string"),
    "setup_id": nullable("string"),
    "target_price": nullable("string"),
    "quantity_asset": nullable("string"),
    "priority": {"type": "integer", "minimum": 1, "maximum": 5},
    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
    "invalidation_condition": nullable("string"),
    "review_at": nullable("string"),
    "expires_at": nullable("string"),
}
PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "actions", "observations"],
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "actions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 20,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": list(ACTION_PROPERTIES),
                "properties": ACTION_PROPERTIES,
            },
        },
        "observations": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 300,
            },
        },
    },
}


def strict_json(text):
    def pairs(values):
        result = {}
        for k, v in values:
            require(k not in result, "Duplicate JSON key.")
            result[k] = v
        return result

    def reject(_):
        raise ValueError("JSON decimal numbers prohibited.")

    return json.loads(text, object_pairs_hook=pairs, parse_float=reject, parse_constant=reject)


def prose(value, maximum):
    require(
        isinstance(value, str) and bool(value.strip()) and len(value) <= maximum,
        "Invalid commentary.",
    )


@dataclass(frozen=True, slots=True)
class AIRecommendationPlan:
    summary: str
    actions: tuple[Recommendation, ...]
    observations: tuple[str, ...]

    @classmethod
    def decode(cls, text: str, snapshot_id: UUID):
        try:
            require(isinstance(text, str) and len(text) <= 24000, "Oversized plan.")
            data = strict_json(text)
            require(
                isinstance(data, dict) and set(data) == {"summary", "actions", "observations"},
                "Unexpected plan fields.",
            )
            prose(data["summary"], 500)
            require(
                isinstance(data["observations"], list) and len(data["observations"]) <= 12,
                "Invalid observations.",
            )
            for value in data["observations"]:
                prose(value, 300)
            require(
                isinstance(data["actions"], list) and 1 <= len(data["actions"]) <= 20,
                "Invalid actions.",
            )
            actions = []
            for index, raw in enumerate(data["actions"]):
                require(
                    isinstance(raw, dict) and set(raw) == set(ACTION_PROPERTIES),
                    "Unexpected action fields.",
                )
                a = dict(raw)
                prose(a["reason"], 500)
                if a["invalidation_condition"] is not None:
                    prose(a["invalidation_condition"], 500)
                a["action_type"] = ActionType(a["action_type"])
                require(
                    a["action_type"] is not ActionType.REBALANCE,
                    "Legacy ambiguous rebalance is not an AI action.",
                )
                if a["side"] is not None:
                    a["side"] = TradingSide(a["side"])
                if a["existing_order_id"] is not None:
                    raw_id = a["existing_order_id"]
                    require(isinstance(raw_id, str), "Order ID must be text.")
                    a["existing_order_id"] = UUID(raw_id)
                    require(str(a["existing_order_id"]) == raw_id, "Noncanonical order ID.")
                if a["setup_id"] is not None:
                    raw_id = a["setup_id"]
                    require(isinstance(raw_id, str), "Setup ID must be text.")
                    a["setup_id"] = UUID(raw_id)
                    require(str(a["setup_id"]) == raw_id, "Noncanonical setup ID.")
                for key in ("target_price", "quantity_asset"):
                    if a[key] is not None:
                        require(
                            isinstance(a[key], str) and len(a[key]) <= 100,
                            "Decimal must be bounded text.",
                        )
                        a[key] = parse_decimal(a[key], positive=True)
                for key in ("review_at", "expires_at"):
                    if a[key] is not None:
                        require(isinstance(a[key], str), "Date must be text.")
                        a[key] = datetime.fromisoformat(a[key])
                actions.append(Recommendation(uuid5(snapshot_id, f"ai-plan:{index}"), **a))
            return cls(data["summary"], tuple(actions), tuple(data["observations"]))
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, RecursionError):
            raise ValueError("Invalid AI recommendation plan.") from None
