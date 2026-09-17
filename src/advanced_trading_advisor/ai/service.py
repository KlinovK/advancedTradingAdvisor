"""Responses API HTTP adapter. No SDK/provider types cross the reasoner protocol."""

import asyncio

import httpx

from advanced_trading_advisor.analysis.reasoning import ReasoningUnavailable
from advanced_trading_advisor.domain.persistence import FailureCategory

from .context import CONTEXT_VERSION, build_context
from .models import PLAN_SCHEMA, SCHEMA_VERSION, AIRecommendationPlan
from .settings import OpenAISettings

INSTRUCTIONS = """You provide bounded reasoning around a complete deterministic spot-only advisory
plan and never execute trades.
Objective: portfolio growth over time, while protecting the supplied stable reserve and monthly
expense runway. No leverage, margin, derivatives, forced selling or unnecessary trading.
Prefer favorable risk/reward; use HOLD/WAIT when evidence is weak. All supplied financial metrics,
prices, closed 4H indicators, regime, holdings, orders and risk/sizing limits are authoritative.
Do not recalculate metrics, invent assets/prices/orders, or alter confirmed state.
Return every item from allowed_action_terms exactly once and preserve its order. Reproduce its
action type, asset, side, quantity_asset, target_price, setup ID, existing order ID, priority,
review_at and expires_at exactly. amount_usd is a deterministic price-times-quantity checksum,
not a separate output key. Do not add, remove, reorder, resize, reprice, reprioritize or retime
actions. You may write concise explanations. Trigger states and transitions are deterministic
facts: never confirm, revive, expire or invalidate a setup yourself.
The deterministic setup levels are the only permissible trading levels. No arbitrary price.
Market BUY is unavailable. Stable SELL is unavailable.
If execution_order_actions_allowed is false, do not introduce BUY, SELL, PLACE_LIMIT_ORDER,
KEEP_LIMIT_ORDER, CANCEL_LIMIT_ORDER or REPLACE_LIMIT_ORDER unless that exact action already
exists in allowed_action_terms. Never invent setup IDs or order IDs. Liquidity, inventory,
sell-capacity, concentration and risk-budget fields are constraints or ceilings, not authorization.
Your role is bounded reasoning and presentation around the deterministic plan, not generation of
new executable financial terms.
An existing order action must reference its exact opaque OPEN order ID and match asset/side.
Cancellation releases no money or inventory for this plan. A replacement consumes its FULL new
cost/inventory alongside the old order. Future SELL proceeds cannot finance BUYs.
Your entire plan will be rejected if ANY action violates deterministic whole-plan validation.
Use existing lowercase action enum values. Financial values are plain decimal STRINGS, no
exponents or floats. All schema keys are required; use null for fields not meaningful to an action.
Do not supply trade terms on HOLD/WAIT/REBALANCE_INFORMATIONAL or KEEP/CANCEL.
REBALANCE_INFORMATIONAL is informational only; never conceal a trade in prose.
Keep summary, reasons and observations concise; no hidden reasoning. All advice is untrusted.
"""


PROMPT_VERSION = 4


class OpenAIReasoner:
    contract_versions = (CONTEXT_VERSION, SCHEMA_VERSION, PROMPT_VERSION)

    @property
    def reasoning_effort(self):
        return self.settings.reasoning_effort

    def __init__(
        self, settings: OpenAISettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        if not settings.enabled:
            raise ValueError("Cannot construct a disabled AI adapter.")
        self.settings, self.transport = settings, transport

    @property
    def model_name(self):
        return self.settings.model

    async def propose(self, snapshot, market, risk, policy, active=None, baseline_actions=()):
        try:
            context = build_context(snapshot, market, risk, policy, active, baseline_actions)
            if len(context) > 24000:
                raise ReasoningUnavailable()
            # Wall clock bounds the full call, not just individual HTTP I/O operations.
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with httpx.AsyncClient(
                    timeout=self.settings.timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                    transport=self.transport,
                ) as client:
                    response = await client.post(
                        "https://api.openai.com/v1/responses",
                        headers={"Authorization": f"Bearer {self.settings.api_key}"},
                        json={
                            "model": self.model_name,
                            "instructions": INSTRUCTIONS,
                            "input": [{"role": "user", "content": context}],
                            "reasoning": {"effort": self.settings.reasoning_effort},
                            "store": False,
                            "max_output_tokens": 4000,
                            "text": {
                                "format": {
                                    "type": "json_schema",
                                    "name": "AIRecommendationPlan",
                                    "strict": True,
                                    "schema": PLAN_SCHEMA,
                                }
                            },
                        },
                    )
                    if response.status_code in {401, 403}:
                        raise ReasoningUnavailable(FailureCategory.AI_AUTHENTICATION_PERMISSION)
                    if response.status_code == 429:
                        raise ReasoningUnavailable(FailureCategory.AI_RATE_LIMIT)
                    if 500 <= response.status_code <= 599:
                        raise ReasoningUnavailable(FailureCategory.AI_PROVIDER_SERVER)
                    if response.status_code != 200 or len(response.content) > 128000:
                        raise ReasoningUnavailable()
                    data = response.json()
            if data.get("status") == "incomplete":
                raise ReasoningUnavailable(FailureCategory.AI_INCOMPLETE)
            if data.get("status") != "completed" or data.get("error") is not None:
                raise ReasoningUnavailable()
            texts = []
            for item in data["output"]:
                if item.get("type") == "reasoning":
                    continue  # Never use, display or persist reasoning content.
                if item.get("type") != "message" or item.get("role") != "assistant":
                    raise ReasoningUnavailable()
                for part in item["content"]:
                    if part.get("type") == "refusal":
                        raise ReasoningUnavailable(FailureCategory.AI_REFUSAL)
                    if part.get("type") != "output_text":
                        raise ReasoningUnavailable()
                    texts.append(part["text"])
            if len(texts) != 1:
                raise ReasoningUnavailable()
            return AIRecommendationPlan.decode(texts[0], snapshot.id).actions
        except (TimeoutError, httpx.TimeoutException):
            raise ReasoningUnavailable(FailureCategory.AI_TIMEOUT) from None
        except httpx.TransportError:
            raise ReasoningUnavailable(FailureCategory.AI_TRANSPORT) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ReasoningUnavailable(FailureCategory.AI_SCHEMA) from None
        except ReasoningUnavailable:
            raise
        except Exception:
            # Never propagate response bodies, request headers or provider exception strings.
            # asyncio.CancelledError is BaseException and deliberately propagates.
            raise ReasoningUnavailable() from None
