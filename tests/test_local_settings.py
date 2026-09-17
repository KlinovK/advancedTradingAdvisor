import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.settings import CoinGeckoSettings


def test_local_dotenv_and_environment_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_API_TIER", raising=False)
    monkeypatch.delenv("MARKET_DATA_TIMEOUT_SECONDS", raising=False)
    (tmp_path / ".env").write_text(
        'COINGECKO_API_KEY="synthetic-local"\nMARKET_DATA_TIMEOUT_SECONDS=12\n'
    )
    assert CoinGeckoSettings.from_environment().api_key == "synthetic-local"
    monkeypatch.setenv("COINGECKO_API_KEY", "synthetic-process")
    assert CoinGeckoSettings.from_environment().api_key == "synthetic-process"
    assert CoinGeckoSettings.from_environment().timeout_seconds == 12
    with pytest.raises(DomainValidationError):
        CoinGeckoSettings.from_environment({})


def test_no_dotenv_is_valid_and_parent_is_not_loaded(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("COINGECKO_API_KEY=synthetic-parent\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.setenv("COINGECKO_API_KEY", "synthetic-process")
    monkeypatch.setenv("COINGECKO_API_TIER", "demo")
    monkeypatch.setenv("MARKET_DATA_TIMEOUT_SECONDS", "10")
    assert CoinGeckoSettings.from_environment().api_key == "synthetic-process"
    monkeypatch.delenv("COINGECKO_API_KEY")
    with pytest.raises(DomainValidationError):
        CoinGeckoSettings.from_environment()
