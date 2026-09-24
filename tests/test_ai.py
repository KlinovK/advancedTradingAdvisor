import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from test_active_phase9 import daily_asset
from test_advisor import FakeProvider, fixture, proposed
from test_portfolio_risk import NOW, asset, market, order, state

from advanced_trading_advisor.ai.context import build_context
from advanced_trading_advisor.ai.models import ACTION_PROPERTIES, AIRecommendationPlan
from advanced_trading_advisor.ai.service import INSTRUCTIONS, OpenAIReasoner
from advanced_trading_advisor.ai.settings import AIConfigurationError, OpenAISettings
from advanced_trading_advisor.analysis.active import ActiveManagementEngine
from advanced_trading_advisor.analysis.engine import advice
from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.plan import ActivePlanValidator
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.reasoning import AI_WARNING, ReasoningMode
from advanced_trading_advisor.analysis.recommendations import (
    ActionType,
    canonical_action_terms,
)
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.cli.report import render_analysis
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import TradingSide
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)

D = Decimal
SENTINEL = "UNSAFE unrelated trade narrative"


def wire(**changes):
    action = dict.fromkeys(ACTION_PROPERTIES)
    action.update(asset="BTC", action_type="hold", priority=3, reason=SENTINEL)
    action.update(changes)
    return {"summary": SENTINEL, "observations": [SENTINEL], "actions": [action]}


def settings():
    # Synthetic credential only; no real environment is consulted by adapter tests.
    return OpenAISettings(True, "synthetic-test-credential")


def response(plan):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(plan)}],
            }
        ],
    }


def adapter(plan=None, *, status=200, payload=None, error=None):
    calls = []

    def handle(request):
        calls.append(request)
        if error:
            raise error
        return httpx.Response(
            status, json=payload if payload is not None else response(plan or wire())
        )

    return OpenAIReasoner(settings(), transport=httpx.MockTransport(handle)), calls


def exact_terms_adapter(mutator=lambda actions: actions):
    contexts = []

    def handle(request):
        body = json.loads(request.content)
        context = json.loads(body["input"][0]["content"])
        contexts.append(context)
        actions = []
        for terms in context["allowed_action_terms"]:
            action = dict.fromkeys(ACTION_PROPERTIES)
            action.update({key: terms[key] for key in ACTION_PROPERTIES if key in terms})
            action["reason"] = SENTINEL
            action["invalidation_condition"] = SENTINEL
            actions.append(action)
        return httpx.Response(
            200,
            json=response(
                {
                    "summary": SENTINEL,
                    "observations": [SENTINEL],
                    "actions": mutator(actions),
                }
            ),
        )

    return OpenAIReasoner(settings(), transport=httpx.MockTransport(handle)), contexts


def legacy_payload(result, version):
    """Simulate an actual older wire shape, not just rewrite its version number."""
    payload = json.loads(AnalysisResultCodec.encode(result))
    payload["schema_version"] = version
    payload["data"].pop("active")
    for value in payload["data"]["analyzed_assets"]:
        value.pop("daily_context", None)
        value.pop("closed_1h_candles", None)
    for recommendation in payload["data"]["recommendations"]:
        recommendation.pop("setup_id", None)
    for field in (
        "hard_expense_reserve_usd",
        "market_buffer_usd",
        "potential_trading_liquidity_usd",
        "strategy_dry_powder_usd",
        "recommended_deployment_usd",
    ):
        payload["data"]["financial"].pop(field)
    if version < 3:
        payload["data"].pop("configuration")
    if version == 1:
        for field in ("reasoning_mode", "reasoning_model", "ai_warning"):
            payload["data"].pop(field)
    return payload


async def execute(db_path, reasoner, snapshot=None, provider_factory=FakeProvider):
    db = Database(db_path)
    await db.initialize()
    snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
    saved = await ConfirmedStateService(snapshots).initialize(snapshot or fixture()[0])
    result = await AnalyzeCurrentPortfolio(
        snapshots, runs, provider_factory, now=lambda: NOW, reasoner=reasoner
    ).execute()
    assert await snapshots.get_current() == saved
    stored = await runs.get(result.run_id)
    assert stored.run.status is AnalysisRunStatus.COMPLETED
    assert AnalysisResultCodec.decode(stored.result_json) == result
    assert SENTINEL not in stored.result_json
    assert SENTINEL not in render_analysis(result)
    return result


def test_settings_defaults_and_enabled():
    assert not OpenAISettings.from_environment({}).enabled
    configured = OpenAISettings.from_environment(
        {"OPENAI_ENABLED": "true", "OPENAI_API_KEY": "synthetic"}
    )
    assert configured.enabled and configured.model == "gpt-5.6-terra"
    assert "synthetic" not in repr(configured)
    assert configured.reasoning_effort == "low" and configured.timeout_seconds == 45
    assert not OpenAISettings.from_environment({"OPENAI_ENABLED": "true"}, enabled=False).enabled


@pytest.mark.parametrize(
    "change",
    [
        {"OPENAI_API_KEY": ""},
        {"OPENAI_API_KEY": " whitespace "},
        {"OPENAI_MODEL": ""},
        {"OPENAI_MODEL": "bad\nmodel"},
        {"OPENAI_REASONING_EFFORT": "invalid"},
        {"OPENAI_TIMEOUT_SECONDS": "0"},
        {"OPENAI_TIMEOUT_SECONDS": "121"},
        {"OPENAI_TIMEOUT_SECONDS": "NaN"},
        {"OPENAI_ENABLED": "maybe"},
    ],
)
def test_settings_invalid_explicit_errors(change):
    with pytest.raises(AIConfigurationError):
        OpenAISettings.from_environment(
            {"OPENAI_ENABLED": "true", "OPENAI_API_KEY": "synthetic", **change}
        )


def test_dotenv_optional_env_overrides_and_mapping_isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert not OpenAISettings.from_environment().enabled
    (tmp_path / ".env").write_text(
        "OPENAI_ENABLED=true\nOPENAI_API_KEY=synthetic\nOPENAI_MODEL=file-model\n"
    )
    monkeypatch.delenv("OPENAI_ENABLED")
    monkeypatch.setenv("OPENAI_MODEL", "process-model")
    assert OpenAISettings.from_environment().model == "process-model"
    assert not OpenAISettings.from_environment({}).enabled


def test_compact_deterministic_context():
    o = order()
    s, m, r = fixture((o,))
    encoded = build_context(s, m, r, RecommendationPolicy())
    assert encoded == build_context(
        s, replace(m, assets=tuple(reversed(m.assets))), r, RecommendationPolicy()
    )
    assert len(encoded) < 7000
    data = json.loads(encoded)
    assert data["open_orders"][0]["id"] == str(o.id)
    assert data["open_orders"][0]["buy_commitment_usd"] == "400"
    assert data["financial"]["monthly_expenses_usd"] == "250"
    assert data["financial"]["target_runway_months"] == "12"
    assert data["financial"]["minimum_stable_reserve_usd"] == "3000"
    assert data["market"]["regime"] == r.market_regime.regime
    assert data["market"]["regime_policy_version"] == 1
    assert data["assets"][0]["features"]["rsi_4h"] == "55"
    assert data["assets"][0]["eligible_buy_limit_usd"] == "83"
    assert D(data["policy"]["max_candidate_buy_budget_usd"]) > 0
    for forbidden in ("api_key", "candles", "history", "summary", "reason", "synthetic"):
        assert forbidden not in encoded


def test_canonical_action_terms_are_the_context_and_acceptance_source():
    snapshot, evidence, risk = fixture()
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    dated = (
        replace(actions[0], review_at=NOW, expires_at=NOW),
        *actions[1:],
    )
    canonical = canonical_action_terms(dated)
    context = json.loads(
        build_context(snapshot, evidence, risk, RecommendationPolicy(), active, dated)
    )
    assert context["allowed_action_terms"] == [terms.as_dict() for terms in canonical]
    assert set(context["allowed_action_terms"][0]) == {
        "action_type",
        "asset",
        "side",
        "amount_usd",
        "quantity_asset",
        "target_price",
        "setup_id",
        "existing_order_id",
        "priority",
        "review_at",
        "expires_at",
    }
    assert canonical[0].priority == actions[0].priority
    assert canonical[0].review_at == NOW.isoformat(timespec="microseconds")
    assert canonical[0].expires_at == NOW.isoformat(timespec="microseconds")
    assert canonical[0].amount_usd is None
    trade = canonical_action_terms((proposed(snapshot),))[0]
    assert trade.target_price == "83" and trade.quantity_asset == "1"
    assert trade.amount_usd == "83"
    assert trade.review_at is None and trade.expires_at is None


def test_context_sell_reservations_and_buy_block():
    o = order(side=TradingSide.SELL, symbol="BTC", quantity="9")
    s, m, _ = fixture((o,))
    s = replace(s, financial_settings=replace(s.financial_settings, monthly_expenses_usd=D("9999")))
    r = PortfolioRiskPolicy().assess(s, m, now=NOW)
    data = json.loads(build_context(s, m, r, RecommendationPolicy()))
    assert data["assets"][0]["max_candidate_sell_quantity"] == "1"
    assert data["policy"]["max_candidate_buy_budget_usd"] == "0"
    assert data["assets"][0]["max_candidate_buy_quantity"] == "0"
    assert data["risk"]["buying_blocked"]


def test_schema_exact_decimal_mapping():
    value = "0.123456789012345678901234567890"
    parsed = AIRecommendationPlan.decode(
        json.dumps(
            wire(
                action_type="place_limit_order",
                side="buy",
                target_price="83",
                quantity_asset=value,
                review_at=NOW.isoformat(),
                expires_at=NOW.isoformat(),
            )
        ),
        uuid4(),
    )
    assert parsed.actions[0].quantity_asset == D(value)
    assert parsed.actions[0].review_at == NOW


@pytest.mark.parametrize(
    "change",
    [
        {"surprise": True},
        {"action_type": "leverage"},
        {"priority": True},
        {"priority": 0},
        {"reason": ""},
        {"asset": "btc"},
        {"target_price": 1},
        {"target_price": "NaN"},
        {"target_price": "1e2"},
        {"quantity_asset": "0"},
        {"quantity_asset": "-1"},
        {"side": "buy"},
        {"existing_order_id": "not-a-uuid"},
        {"review_at": "2026-01-01"},
        {"invalidation_condition": 1},
    ],
)
def test_strict_wire_rejects_malformed(change):
    with pytest.raises(ValueError, match="Invalid AI recommendation plan"):
        AIRecommendationPlan.decode(json.dumps(wire(**change)), uuid4())


@pytest.mark.parametrize(
    "text",
    [
        "{",
        "[]",
        '{"summary":"x","summary":"y"}',
        '{"summary":NaN}',
        json.dumps({**wire(), "extra": 1}),
    ],
)
def test_invalid_plan_shapes(text):
    with pytest.raises(ValueError):
        AIRecommendationPlan.decode(text, uuid4())


def test_ai_alternative_without_deterministic_terms_falls_back_and_http_contract(tmp_path):
    ai, calls = adapter()
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.reasoning_model == settings().model and result.ai_warning == AI_WARNING
    assert result.recommendations
    assert len(calls) == 1
    request = calls[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert body["store"] is False
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["instructions"] == INSTRUCTIONS
    assert body["max_output_tokens"] == 4000
    assert all(t == 45 for t in request.extensions["timeout"].values())
    assert result.configuration.ai_failure_category == "exact_terms_mismatch"


def test_passive_no_setup_exact_wire_terms_are_ai_assisted(tmp_path):
    ai, contexts = exact_terms_adapter()
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai))
    assert result.reasoning_mode is ReasoningMode.AI_ASSISTED
    assert result.configuration.ai_failure_category is None
    assert (
        result.configuration.ai_context_version,
        result.configuration.ai_schema_version,
        result.configuration.ai_prompt_version,
    ) == (4, 3, 4)
    context = contexts[0]
    assert context["recommended_deployment_usd"] == "0"
    assert context["execution_order_actions_allowed"] is False
    assert context["deterministic_setup_levels"] == []
    assert context["policy"]["capacity_fields_are_constraints_not_authorization"] is True
    assert any(D(asset["max_candidate_sell_quantity"]) > 0 for asset in context["assets"])
    assert [action["priority"] for action in context["allowed_action_terms"]] == [2, 2, 2]
    assert all(
        action["review_at"] is None and action["expires_at"] is None
        for action in context["allowed_action_terms"]
    )


@pytest.mark.parametrize(
    "kind",
    ["priority", "review_at", "expires_at", "missing", "additional", "reordered"],
)
def test_exact_term_mismatches_fall_back_with_safe_diagnostic(tmp_path, kind):
    def mutate(actions):
        if kind == "priority":
            actions[0]["priority"] = 3
        elif kind in {"review_at", "expires_at"}:
            actions[0][kind] = NOW.isoformat()
        elif kind == "missing":
            actions.pop()
        elif kind == "additional":
            actions.append(dict(actions[-1]))
        else:
            actions.reverse()
        return actions

    ai, _ = exact_terms_adapter(mutate)
    result = asyncio.run(execute(tmp_path / f"{kind}.sqlite", ai))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.configuration.ai_failure_category == "exact_terms_mismatch"
    assert SENTINEL not in AnalysisResultCodec.encode(result)


def test_ai_can_explain_exact_deterministic_plan_but_cannot_change_terms(tmp_path):
    class ExactTermsReasoner:
        model_name = "offline-test-model"
        contract_versions = (2, 2, 2)

        async def propose(self, snapshot, market, risk, policy, active, baseline_actions):
            assert active is not None and baseline_actions
            return tuple(replace(action, reason=SENTINEL) for action in baseline_actions)

    result = asyncio.run(execute(tmp_path / "db.sqlite", ExactTermsReasoner()))
    assert result.reasoning_mode is ReasoningMode.AI_ASSISTED
    assert result.configuration.ai_context_version == 2
    assert SENTINEL not in AnalysisResultCodec.encode(result)
    assert all("passed whole-plan checks" in action.reason for action in result.recommendations)


@pytest.mark.parametrize(
    "failure",
    [
        "timeout",
        "network",
        "api",
        "refusal",
        "incomplete",
        "malformed",
        "schema",
        "empty",
        "unsupported",
    ],
)
def test_runtime_failures_complete_with_baseline(tmp_path, failure):
    if failure == "timeout":
        ai, calls = adapter(error=httpx.ReadTimeout(SENTINEL))
    elif failure == "network":
        ai, calls = adapter(error=httpx.ConnectError(SENTINEL))
    elif failure == "api":
        ai, calls = adapter(status=429, payload={"error": SENTINEL})
    elif failure == "refusal":
        ai, calls = adapter(
            payload={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": SENTINEL}],
                    }
                ],
            }
        )
    elif failure == "incomplete":
        ai, calls = adapter(payload={"status": "incomplete", "output": []})
    elif failure == "malformed":
        ai, calls = adapter(payload={"status": "completed", "output": "wrong"})
    elif failure == "schema":
        ai, calls = adapter({**wire(), "extra": True})
    elif failure == "empty":
        ai, calls = adapter({**wire(), "actions": []})
    else:
        ai, calls = adapter(wire(action_type="futures"))
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai))
    assert len(calls) == 1  # Never repeat a paid request automatically.
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.ai_warning == AI_WARNING and result.reasoning_model == settings().model
    assert AI_WARNING in render_analysis(result)


@pytest.mark.parametrize(
    "case,status,payload,error,expected",
    [
        ("unauthorized", 401, {"error": SENTINEL}, None, "ai_authentication_permission"),
        ("forbidden", 403, {"error": SENTINEL}, None, "ai_authentication_permission"),
        ("rate-limit", 429, {"error": SENTINEL}, None, "ai_rate_limit"),
        ("server", 503, {"error": SENTINEL}, None, "ai_provider_server"),
        ("transport", 200, None, httpx.ConnectError(SENTINEL), "ai_transport"),
        ("timeout", 200, None, httpx.ReadTimeout(SENTINEL), "ai_timeout"),
        (
            "refusal",
            200,
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": SENTINEL}],
                    }
                ],
            },
            None,
            "ai_refusal",
        ),
        (
            "incomplete",
            200,
            {"status": "incomplete", "error": {"message": SENTINEL}, "output": []},
            None,
            "ai_incomplete",
        ),
        ("unknown", 400, {"error": SENTINEL}, None, "ai_provider"),
        ("unexpected-exception", 200, None, RuntimeError(SENTINEL), "ai_provider"),
    ],
)
def test_provider_failures_persist_only_safe_allowlisted_category(
    tmp_path, case, status, payload, error, expected
):
    ai, calls = adapter(status=status, payload=payload, error=error)
    result = asyncio.run(execute(tmp_path / f"{case}.sqlite", ai))
    encoded = AnalysisResultCodec.encode(result)

    assert len(calls) == 1
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.configuration.ai_failure_category == expected
    assert SENTINEL not in encoded
    assert settings().api_key not in encoded


def test_historical_generic_ai_provider_category_remains_readable(tmp_path):
    ai, _ = adapter(status=400, payload={"error": SENTINEL})
    result = asyncio.run(execute(tmp_path / "historical-provider.sqlite", ai))
    encoded = AnalysisResultCodec.encode(result)
    decoded = AnalysisResultCodec.decode(encoded)

    assert decoded.configuration.ai_failure_category == "ai_provider"
    assert decoded == result


class FakeReasoner:
    model_name = "test-model"

    def __init__(self, proposals):
        self.proposals = proposals

    async def propose(self, snapshot, market, risk, policy, active=None, baseline_actions=()):
        return self.proposals(snapshot)


@pytest.mark.parametrize(
    "kind",
    [
        "budget",
        "deficit",
        "oversell",
        "inventory",
        "bad_id",
        "conflict",
        "replacement_side",
        "replacement_budget",
        "duplicate",
        "future_proceeds",
        "cancel_release",
        "allocation",
    ],
)
def test_unsafe_whole_plans_use_identical_baseline(tmp_path, kind):
    buy = order(symbol="BTC", quantity="1", price="83")
    sell = order(symbol="BTC", side=TradingSide.SELL, quantity="9", price="110")
    s, m, _ = fixture((buy, sell))
    if kind == "deficit":
        s = replace(
            s, financial_settings=replace(s.financial_settings, monthly_expenses_usd=D("9999"))
        )
    if kind == "allocation":
        s = replace(
            s,
            portfolio=replace(
                s.portfolio,
                accounts=(
                    replace(
                        s.portfolio.accounts[0],
                        positions=tuple(
                            replace(p, amount=D("50")) if p.symbol == "BTC" else p
                            for p in s.portfolio.accounts[0].positions
                        ),
                    ),
                ),
            ),
        )

    def candidates(snapshot):
        if kind in {"budget", "deficit", "allocation"}:
            return (
                proposed(
                    snapshot,
                    quantity={"budget": "30", "deficit": "1", "allocation": "18"}[kind],
                    reason=SENTINEL,
                ),
            )
        if kind in {"oversell", "inventory"}:
            return (
                proposed(
                    snapshot,
                    action=ActionType.SELL,
                    side=TradingSide.SELL,
                    price=None,
                    quantity="100" if kind == "oversell" else "2",
                    reason=SENTINEL,
                ),
            )
        if kind == "bad_id":
            return (
                advice(
                    snapshot.id, "BTC", ActionType.CANCEL_LIMIT_ORDER, SENTINEL, order_id=uuid4()
                ),
            )
        if kind == "conflict":
            return tuple(
                advice(snapshot.id, "BTC", a, SENTINEL, order_id=buy.id)
                for a in (ActionType.KEEP_LIMIT_ORDER, ActionType.CANCEL_LIMIT_ORDER)
            )
        if kind.startswith("replacement"):
            return (
                proposed(
                    snapshot,
                    action=ActionType.REPLACE_LIMIT_ORDER,
                    order_id=buy.id,
                    side=TradingSide.SELL if kind.endswith("side") else TradingSide.BUY,
                    quantity="21",
                    reason=SENTINEL,
                ),
            )
        if kind == "duplicate":
            a = proposed(snapshot, reason=SENTINEL)
            return a, replace(a, recommendation_id=uuid4())
        if kind == "future_proceeds":
            return (
                proposed(snapshot, quantity="30", reason=SENTINEL),
                proposed(
                    snapshot,
                    symbol="ETH",
                    action=ActionType.SELL,
                    side=TradingSide.SELL,
                    price=None,
                    quantity="10",
                    reason=SENTINEL,
                ),
            )
        return (
            advice(snapshot.id, "BTC", ActionType.CANCEL_LIMIT_ORDER, SENTINEL, order_id=sell.id),
            proposed(
                snapshot,
                action=ActionType.SELL,
                side=TradingSide.SELL,
                price=None,
                quantity="10",
                reason=SENTINEL,
            ),
        )

    result = asyncio.run(execute(tmp_path / "db.sqlite", FakeReasoner(candidates), s))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    risk = PortfolioRiskPolicy().assess(s, m, now=NOW)
    active, generated = ActiveManagementEngine().evaluate(s, m, risk)
    baseline = ActivePlanValidator().validate_active(generated, s, m, risk, active)
    assert result.recommendations == baseline.recommendations


def test_unranked_replacement_is_rejected(tmp_path):
    o = order(symbol="BTC", quantity="1", price="90")
    s, _, _ = fixture((o,))
    ai, _ = adapter(
        wire(
            action_type="replace_limit_order",
            side="buy",
            existing_order_id=str(o.id),
            target_price="83",
            quantity_asset="1",
            invalidation_condition=SENTINEL,
        )
    )
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai, s))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert all(a.invalidation_condition is None for a in result.recommendations)


def test_legacy_report_compatibility(tmp_path):
    result = asyncio.run(execute(tmp_path / "db.sqlite", None))
    payload = legacy_payload(result, 1)
    decoded = AnalysisResultCodec.decode(json.dumps(payload))
    assert decoded.active is None and decoded.configuration is None
    assert decoded.snapshot_id == result.snapshot_id


def test_historical_plan_validation_category_remains_readable(tmp_path):
    result = asyncio.run(execute(tmp_path / "db.sqlite", adapter()[0]))
    historical = replace(
        result,
        configuration=replace(result.configuration, ai_failure_category="plan_validation"),
    )
    decoded = AnalysisResultCodec.decode(AnalysisResultCodec.encode(historical))
    assert decoded.configuration.ai_failure_category == "plan_validation"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_ai_trade_without_ranked_candidate_does_not_survive_validation(tmp_path, side):
    ai, _ = adapter(
        wire(
            action_type="place_limit_order",
            side=side,
            target_price="83" if side == "buy" else "110",
            quantity_asset="1",
        )
    )
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.configuration.ai_failure_category == "validator_rejected"
    assert not any(a.reason == SENTINEL for a in result.recommendations)


@pytest.mark.parametrize(
    "action_type,side,target_price",
    [("buy", "buy", None), ("sell", "sell", None), ("place_limit_order", "buy", "83")],
)
def test_no_setup_capacity_never_authorizes_invented_trade(
    tmp_path, action_type, side, target_price
):
    ai, _ = adapter(
        wire(
            action_type=action_type,
            side=side,
            target_price=target_price,
            quantity_asset="1",
        )
    )
    result = asyncio.run(execute(tmp_path / f"{action_type}-{side}.sqlite", ai))
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.configuration.ai_failure_category == "validator_rejected"


@pytest.mark.parametrize(
    "kind",
    ["setup_id", "target_price", "quantity_asset", "side", "priority", "review_at", "expires_at"],
)
def test_confirmed_setup_requires_every_exact_deterministic_term(tmp_path, kind):
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class ConfirmedProvider:
        async def get_market_snapshot(self, symbols):
            return evidence

    def mutate(actions):
        action = actions[0]
        if kind == "setup_id":
            action["setup_id"] = str(uuid4())
        elif kind == "target_price":
            action["target_price"] = format(D(action["target_price"]) + 1, "f")
        elif kind == "quantity_asset":
            action["quantity_asset"] = format(D(action["quantity_asset"]) / 2, "f")
        elif kind == "side":
            action["side"] = "sell"
        elif kind == "priority":
            action["priority"] = 2
        else:
            action[kind] = NOW.isoformat()
        return actions

    ai, contexts = exact_terms_adapter(mutate)
    result = asyncio.run(
        execute(
            tmp_path / f"confirmed-{kind}.sqlite",
            ai,
            snapshot,
            provider_factory=ConfirmedProvider,
        )
    )
    assert contexts[0]["execution_order_actions_allowed"] is True
    assert contexts[0]["recommended_deployment_usd"] != "0"
    assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
    assert result.configuration.ai_failure_category in {
        "validator_rejected",
        "exact_terms_mismatch",
    }


def test_confirmed_setup_exact_deterministic_terms_are_ai_assisted(tmp_path):
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class ConfirmedProvider:
        async def get_market_snapshot(self, symbols):
            return evidence

    ai, contexts = exact_terms_adapter()
    result = asyncio.run(
        execute(
            tmp_path / "confirmed-exact.sqlite",
            ai,
            snapshot,
            provider_factory=ConfirmedProvider,
        )
    )
    assert contexts[0]["execution_order_actions_allowed"] is True
    assert contexts[0]["allowed_action_terms"][0]["amount_usd"] == format(
        result.recommendations[0].target_price * result.recommendations[0].quantity_asset,
        "f",
    )
    assert result.reasoning_mode is ReasoningMode.AI_ASSISTED
    assert result.configuration.ai_failure_category is None


def test_total_timeout_bounds_even_nonresponsive_transport(tmp_path):
    async def wait_forever(request):
        await asyncio.Event().wait()

    ai = OpenAIReasoner(
        replace(settings(), timeout_seconds=1), transport=httpx.MockTransport(wait_forever)
    )

    async def run():
        async with asyncio.timeout(3):
            return await execute(tmp_path / "db.sqlite", ai)

    assert asyncio.run(run()).reasoning_mode is ReasoningMode.AI_FALLBACK


def test_cancellation_propagates_and_does_not_claim_fallback(tmp_path):
    class CancelledReasoner:
        model_name = "test-model"

        async def propose(self, *args):
            raise asyncio.CancelledError()

    async def run():
        db = Database(tmp_path / "db.sqlite")
        await db.initialize()
        snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
        initial = await ConfirmedStateService(snapshots).initialize(fixture()[0])
        with pytest.raises(asyncio.CancelledError):
            await AnalyzeCurrentPortfolio(
                snapshots, runs, FakeProvider, now=lambda: NOW, reasoner=CancelledReasoner()
            ).execute()
        stored = (await runs.list_all_recent())[0]
        assert stored.run.status is AnalysisRunStatus.FAILED and stored.result_json is None
        assert await snapshots.get_current() == initial

    asyncio.run(run())


def test_malformed_http_json_falls_back(tmp_path):
    ai = OpenAIReasoner(
        settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"not JSON")),
    )
    assert (
        asyncio.run(execute(tmp_path / "db.sqlite", ai)).reasoning_mode is ReasoningMode.AI_FALLBACK
    )


def test_stored_fallback_warning_cannot_contain_provider_text(tmp_path):
    ai, _ = adapter(status=500)
    result = asyncio.run(execute(tmp_path / "db.sqlite", ai))
    with pytest.raises(ValueError):
        replace(result, ai_warning=SENTINEL)


def test_legacy_report_is_readable_after_database_reopen(tmp_path):
    result = asyncio.run(execute(tmp_path / "db.sqlite", None))
    payload = legacy_payload(result, 1)

    async def run():
        from advanced_trading_advisor.analysis.models import AnalysisRun

        db = Database(tmp_path / "db.sqlite")
        await db.initialize()
        runs = SQLiteAnalysisRunRepository(db)
        old_run = AnalysisRun.start(result.snapshot_id, NOW)
        await runs.create(old_run)
        payload["data"]["run_id"] = str(old_run.run_id)
        await runs.mark_completed(
            old_run.run_id,
            completed_at=NOW,
            result_json=json.dumps(payload),
            market_cutoff=result.market_cutoff,
        )
        reopened = SQLiteAnalysisRunRepository(Database(tmp_path / "db.sqlite"))
        stored = await reopened.get(old_run.run_id)
        decoded = AnalysisResultCodec.decode(stored.result_json)
        assert decoded.reasoning_mode is ReasoningMode.DETERMINISTIC
        assert decoded.reasoning_model is None

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["disabled", "assisted", "fallback", "misconfigured"])
def test_cli_modes_no_secrets(tmp_path, monkeypatch, mode):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "db.sqlite"

    async def setup():
        database = Database(db)
        await database.initialize()
        await ConfirmedStateService(SQLiteSnapshotRepository(database)).initialize(fixture()[0])

    asyncio.run(setup())
    monkeypatch.setenv("OPENAI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "" if mode == "misconfigured" else "synthetic-cli-secret")
    ai, calls = adapter(status=500 if mode == "fallback" else 200)
    output = []
    code = main(
        ["--db", str(db), "analyze", *(["--no-ai"] if mode == "disabled" else [])],
        now=lambda: NOW,
        provider_factory=FakeProvider,
        reasoner_factory=lambda _: ai,
        output=output.append,
    )
    text = "\n".join(output)
    assert "synthetic-cli-secret" not in text and SENTINEL not in text
    if mode == "misconfigured":
        assert code == 1 and "configure OPENAI_API_KEY" in text and not calls
    else:
        assert code == 0
        label = {"disabled": "Deterministic", "assisted": "AI fallback", "fallback": "AI fallback"}[
            mode
        ]
        assert f"Reasoning: {label}" in text
        assert len(calls) == (0 if mode == "disabled" else 1)
