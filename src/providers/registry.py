from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .base import MarketDataProvider
from .binance import BinanceProvider
from .coinbase import CoinbaseProvider
from .hyperliquid import HyperliquidProvider
from .instruments import InstrumentRegistry
from .kraken import KrakenProvider
from .types import ProviderHealth, ProviderStatus


PROVIDER_TYPES: dict[str, type[MarketDataProvider]] = {
    "hyperliquid": HyperliquidProvider,
    "binance": BinanceProvider,
    "coinbase": CoinbaseProvider,
    "kraken": KrakenProvider,
}


class ProviderRegistry:
    def __init__(self, config: Mapping[str, Any], instruments: InstrumentRegistry):
        self.config = config
        self.instruments = instruments
        self._providers: dict[str, MarketDataProvider] = {}
        for provider_id, settings in config.items():
            if not bool(settings.get("enabled", False)):
                continue
            kind = str(settings.get("kind", provider_id))
            factory = PROVIDER_TYPES.get(kind)
            if factory is None:
                raise ValueError(f"unsupported provider kind: {kind}")
            if provider_id != kind:
                raise ValueError("phase 1 provider_id must equal its adapter kind")
            self._providers[provider_id] = factory(settings, instruments)

    def get(self, provider_id: str) -> MarketDataProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"provider is not enabled: {provider_id}") from exc

    def enabled(self) -> tuple[MarketDataProvider, ...]:
        return tuple(self._providers.values())

    def health(self) -> dict[str, ProviderHealth]:
        result: dict[str, ProviderHealth] = {}
        for provider_id, settings in self.config.items():
            provider = self._providers.get(provider_id)
            if provider is not None:
                result[provider_id] = provider.health()
            else:
                import time
                result[provider_id] = ProviderHealth(
                    provider=provider_id,
                    status=ProviderStatus.DISABLED,
                    checked_at_ms=int(time.time() * 1000),
                )
        return result

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self._providers.values()), return_exceptions=True)


def validate_provider_config(config: Mapping[str, Any]) -> None:
    providers = config.get("providers")
    if not isinstance(providers, Mapping) or not providers:
        raise ValueError("providers must be a non-empty mapping")
    allowed_hosts: dict[str, dict[str, set[str]]] = {
        "hyperliquid": {
            "rest_url": {"api.hyperliquid.xyz"},
            "websocket_url": {"api.hyperliquid.xyz"},
        },
        "binance": {
            "spot_rest_url": {"data-api.binance.vision", "api.binance.com"},
            "futures_rest_url": {"fapi.binance.com"},
            "spot_websocket_url": {"stream.binance.com"},
            "futures_websocket_url": {"fstream.binance.com"},
        },
        "coinbase": {
            "rest_url": {"api.coinbase.com"},
            "websocket_url": {"advanced-trade-ws.coinbase.com"},
        },
        "kraken": {
            "rest_url": {"api.kraken.com"},
            "websocket_url": {"ws.kraken.com"},
        },
    }
    for provider_id, settings in providers.items():
        if provider_id not in allowed_hosts:
            raise ValueError(f"unsupported provider: {provider_id}")
        if not isinstance(settings, Mapping):
            raise ValueError(f"provider {provider_id} settings must be a mapping")
        if str(settings.get("kind", provider_id)) != provider_id:
            raise ValueError(f"provider {provider_id} kind mismatch")
        timeout = float(settings.get("request_timeout_seconds", 8))
        if not 0 < timeout <= 10:
            raise ValueError(f"provider {provider_id} request timeout must be > 0 and <= 10 seconds")
        max_subscriptions = int(settings.get("max_subscriptions", 0))
        if max_subscriptions <= 0:
            raise ValueError(f"provider {provider_id} max_subscriptions must be positive")
        concurrency = int(settings.get("max_concurrent_requests", 4))
        if not 1 <= concurrency <= 10:
            raise ValueError(f"provider {provider_id} max_concurrent_requests must be in [1,10]")
        interval_ms = int(settings.get("minimum_request_interval_ms", 100))
        if not 0 <= interval_ms <= 10_000:
            raise ValueError(f"provider {provider_id} minimum_request_interval_ms must be in [0,10000]")
        for key, hosts in allowed_hosts[provider_id].items():
            value = str(settings.get(key, ""))
            parsed = urlparse(value)
            expected_scheme = "wss" if "websocket" in key else "https"
            if parsed.scheme != expected_scheme or parsed.hostname not in hosts:
                raise ValueError(f"{provider_id}.{key} must use an approved official {expected_scheme} endpoint")
        lowered_keys = {str(key).lower() for key in settings}
        if any(token in key for key in lowered_keys for token in ("secret", "private_key", "api_key", "token")):
            raise ValueError(f"provider {provider_id} public config must not contain credential fields")

    entries = config.get("instruments")
    if not isinstance(entries, list) or not entries:
        raise ValueError("instruments must be a non-empty list")
    instrument_registry = InstrumentRegistry.from_config(entries)
    if "hyperliquid" not in providers or not providers["hyperliquid"].get("enabled", False):
        raise ValueError("Hyperliquid provider must remain enabled in phase 1")
    unknown = {instrument.provider for instrument in instrument_registry.all(active_only=False)} - set(providers)
    if unknown:
        raise ValueError(f"instruments reference unknown providers: {sorted(unknown)}")

    universe = config.get("universe", {})
    scan_limit = int(universe.get("max_scan_instruments", 0))
    deep_limit = int(universe.get("max_deep_watchlist", 0))
    if not 1 <= scan_limit <= 150:
        raise ValueError("universe.max_scan_instruments must be in [1, 150]")
    if not 1 <= deep_limit <= 30 or deep_limit > scan_limit:
        raise ValueError("universe.max_deep_watchlist must be in [1, 30] and <= scan limit")

    truth = config.get("market_truth", {})
    if int(truth.get("minimum_quorum", 0)) < 2:
        raise ValueError("market_truth.minimum_quorum must be at least 2")
    if float(truth.get("max_source_age_seconds", 0)) <= 0:
        raise ValueError("market_truth.max_source_age_seconds must be positive")
    if str(truth.get("quote_normalization", "none")).lower() != "none":
        raise ValueError("quote normalization requires a separately verified FX provider")
    catalog = config.get("market_catalog", {})
    refresh = int(float(catalog.get("refresh_interval_seconds", 900)))
    maximum_age = int(float(catalog.get("max_age_seconds", 1800)))
    if refresh < 60 or maximum_age < refresh:
        raise ValueError("market catalog requires refresh >= 60s and max_age >= refresh interval")
    if float(catalog.get("minimum_quote_turnover_24h", 0)) < 0:
        raise ValueError("market catalog minimum quote turnover must be non-negative")
    if float(catalog.get("maximum_spread_bps", 0)) <= 0:
        raise ValueError("market catalog maximum spread must be positive")
    if not config.get("market_catalog", {}).get("allowed_quote_assets"):
        raise ValueError("market catalog allowed_quote_assets must be configured")
