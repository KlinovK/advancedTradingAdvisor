class MarketDataError(RuntimeError):
    """Sanitized market-data failure; no HTTP response, request or credentials attached."""


class UnsupportedSymbolError(MarketDataError):
    pass


class MalformedMarketDataError(MarketDataError):
    pass


class StaleMarketDataError(MarketDataError):
    pass


class MarketCutoffMismatchError(MarketDataError):
    pass


class ProviderUnavailableError(MarketDataError):
    pass


class ProviderRateLimitedError(MarketDataError):
    pass
