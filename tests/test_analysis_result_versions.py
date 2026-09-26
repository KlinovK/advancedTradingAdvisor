"""Real encoder round-trips at each current writer version boundary."""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_http_analyses import completed_result

from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.market.models import DailyContext


def result_for_version(version):
    result = completed_result()
    if version == 5:
        return result
    result = replace(
        result,
        market=replace(
            result.market,
            assets=tuple(
                replace(asset, closed_1h_candles=(), daily_context=DailyContext())
                if version < 4
                else replace(asset, closed_1h_candles=())
                for asset in result.market.assets
            ),
        ),
        recommendations=tuple(replace(r, setup_id=None) for r in result.recommendations),
    )
    if version == 4:
        return replace(
            result,
            active=replace(
                result.active,
                policy_version=1,
                setups=tuple(replace(s, trigger=None) for s in result.active.setups),
            ),
        )
    return replace(
        result,
        active=None,
        configuration=None if version == 2 else result.configuration,
        financial=replace(
            result.financial,
            hard_expense_reserve_usd=Decimal("0"),
            market_buffer_usd=Decimal("0"),
            potential_trading_liquidity_usd=Decimal("0"),
            strategy_dry_powder_usd=Decimal("0"),
            recommended_deployment_usd=Decimal("0"),
        ),
    )


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_real_encoder_emits_only_version_legal_fields_and_roundtrips(version):
    original = result_for_version(version)
    encoded = AnalysisResultCodec.encode(original)
    payload = json.loads(encoded)
    assert payload["schema_version"] == version
    for recommendation in payload["data"]["recommendations"]:
        assert ("setup_id" in recommendation) is (version >= 5)
    if version >= 4:
        for setup in payload["data"]["active"]["setups"]:
            assert ("trigger" in setup) is (version >= 5)
    decoded = AnalysisResultCodec.decode(encoded)
    assert decoded == original
    assert AnalysisResultCodec.encode(decoded) == encoded
    if version == 5:
        assert decoded.recommendations[0].setup_id is not None
        assert decoded.active.setups[0].trigger is not None


@pytest.mark.parametrize("version", [2, 3, 4])
def test_historical_decoder_still_rejects_future_recommendation_fields(version):
    payload = json.loads(AnalysisResultCodec.encode(result_for_version(version)))
    payload["data"]["recommendations"][0]["setup_id"] = None
    with pytest.raises(ValueError):
        AnalysisResultCodec.decode(json.dumps(payload))


def test_v4_decoder_still_rejects_v5_trigger_field():
    payload = json.loads(AnalysisResultCodec.encode(result_for_version(4)))
    payload["data"]["active"]["setups"][0]["trigger"] = None
    with pytest.raises(ValueError):
        AnalysisResultCodec.decode(json.dumps(payload))
