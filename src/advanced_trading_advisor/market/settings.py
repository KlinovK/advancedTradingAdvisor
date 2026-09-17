from collections.abc import Mapping
from dataclasses import dataclass, field

from advanced_trading_advisor.configuration import local_environment
from advanced_trading_advisor.domain.validation import require


@dataclass(frozen=True, slots=True)
class CoinGeckoSettings:
    api_key: str = field(repr=False)
    api_tier: str = "demo"
    timeout_seconds: int = 10
    max_retries: int = 1

    def __post_init__(self):
        require(
            isinstance(self.api_key, str)
            and bool(self.api_key.strip())
            and self.api_key == self.api_key.strip()
            and all(33 <= ord(c) <= 126 for c in self.api_key),
            "Invalid CoinGecko API key.",
        )
        require(self.api_tier == "demo", "Only CoinGecko Demo is configured in this phase.")
        require(
            type(self.timeout_seconds) is int and 1 <= self.timeout_seconds <= 60,
            "Market timeout must be an integer from 1 to 60 seconds.",
        )
        require(
            type(self.max_retries) is int and 0 <= self.max_retries <= 2,
            "Market retries must be 0–2.",
        )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        # Explicit mappings isolate tests. Only read cwd/.env, never search parent projects.
        # No interpolation or mutation of the process environment.
        env = environment if environment is not None else local_environment()
        raw_timeout = env.get("MARKET_DATA_TIMEOUT_SECONDS", "10")
        require(raw_timeout.isascii() and raw_timeout.isdigit(), "Invalid market timeout.")
        return cls(
            env.get("COINGECKO_API_KEY", ""),
            env.get("COINGECKO_API_TIER", "demo"),
            int(raw_timeout),
        )
