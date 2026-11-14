from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from src.data.repository import MarketRepository
from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.providers.base import DerivativesDataProvider, MarketDataProvider, ProviderError
from src.providers.binance import BinanceProvider
from src.providers.coinbase import CoinbaseProvider
from src.providers.instruments import InstrumentRegistry
from src.providers.kraken import KrakenProvider
from src.providers.market_truth import MarketTruthEngine
from src.providers.registry import ProviderRegistry, validate_provider_config
from src.providers.types import (
    EventMetadata, Instrument, MarketCatalogEntry, MarketCatalogScope, MarketType,
    NormalizedBBO, NormalizedTrade,
    ProviderHealth, ProviderStatus, Side,
)
from src.providers.universe import DynamicUniverse, UniverseBucket, UniverseCandidate
from src.service.config import load_config
from src.service.market_data import MultiVenueMarketService


FIXTURES = Path(__file__).parent / "fixtures" / "providers"


def loaded() -> tuple[dict, InstrumentRegistry]:
    config, _ = load_config("config.yaml")
    return config, InstrumentRegistry.from_config(config["instruments"])


def bbo(instrument: Instrument, price: float, received: int, *, event: str, exchange: int | None = None) -> NormalizedBBO:
    return NormalizedBBO(
        EventMetadata(
            provider=instrument.provider,
            venue=instrument.venue,
            instrument_id=instrument.instrument_id,
            exchange_timestamp_ms=exchange or received,
            received_at_ms=received,
            source="fixture://bbo",
            event_id=event,
        ),
        bid=price - 0.01,
        ask=price + 0.01,
        bid_size=10,
        ask_size=10,
        quote_volume_24h=1_000_000,
    )


def independent_sol_usd() -> Instrument:
    return Instrument("fixture:spot:SOL-USD", "native:solana", "fixture", "fixture-spot",
                      MarketType.SPOT, "SOL", "USD", "SOL/USD", 4, 3)


def test_provider_interfaces_registry_and_official_endpoint_guard():
    config, instruments = loaded()
    providers = ProviderRegistry(config["providers"], instruments)
    try:
        assert isinstance(providers.get("hyperliquid"), DerivativesDataProvider)
        assert isinstance(providers.get("binance"), DerivativesDataProvider)
        assert isinstance(providers.get("coinbase"), MarketDataProvider)
        assert isinstance(providers.get("kraken"), MarketDataProvider)
        validate_provider_config(config)

        bad = copy.deepcopy(config)
        bad["providers"]["coinbase"]["rest_url"] = "https://example.invalid"
        with pytest.raises(ValueError, match="official"):
            validate_provider_config(bad)

        credentials = copy.deepcopy(config)
        credentials["providers"]["kraken"]["api_key"] = "must-not-be-in-config"
        with pytest.raises(ValueError, match="credential"):
            validate_provider_config(credentials)
    finally:
        import asyncio
        asyncio.run(providers.close())


@pytest.mark.asyncio
async def test_official_contract_fixtures_normalize_without_network():
    config, instruments = loaded()
    binance = BinanceProvider(config["providers"]["binance"], instruments)
    coinbase = CoinbaseProvider(config["providers"]["coinbase"], instruments)
    kraken = KrakenProvider(config["providers"]["kraken"], instruments)
    payload = json.loads((FIXTURES / "binance_book_ticker.json").read_text(encoding="utf-8"))

    class FixtureHTTP:
        base_url = "https://data-api.binance.vision"

        async def get_json(self, *_args, **_kwargs):
            return payload, 1_700_000_000_500

        async def close(self):
            return None

    await binance.spot.close()
    binance.spot = FixtureHTTP()
    instrument = instruments.get("binance:spot:SOL-USDT")
    result = await binance.fetch_bbo(instrument)
    assert result.bid == 149.95 and result.ask == 150.05
    assert result.metadata.exchange_timestamp_ms == payload["time"]

    coinbase_payload = json.loads((FIXTURES / "coinbase_ticker.json").read_text(encoding="utf-8"))
    coinbase_bbo = coinbase.parse_ticker_message(coinbase_payload, 1_700_000_000_600)
    assert len(coinbase_bbo) == 1 and coinbase_bbo[0].metadata.instrument_id == "coinbase:spot:SOL-USD"

    kraken_payload = json.loads((FIXTURES / "kraken_ticker.json").read_text(encoding="utf-8"))
    kraken_bbo = kraken.parse_ticker_message(kraken_payload, 1_700_000_000_700)
    assert len(kraken_bbo) == 1 and kraken_bbo[0].metadata.instrument_id == "kraken:spot:SOL-USD"
    await binance.close()
    await coinbase.close()
    await kraken.close()


@pytest.mark.asyncio
async def test_binance_discovery_keeps_exact_identity_and_bulk_bbo_is_bounded():
    config, instruments = loaded()
    provider = BinanceProvider(config["providers"]["binance"], instruments)

    def market(symbol, base, quote, *, kind="spot", status="TRADING"):
        row = {"symbol": symbol, "baseAsset": base, "quoteAsset": quote, "status": status,
               "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                           {"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
        if kind == "perpetual":
            row["contractType"] = "PERPETUAL"
        return row

    class FixtureHTTP:
        def __init__(self, base_url, prefix, info, stats, book):
            self.base_url, self.prefix = base_url, prefix
            self.info, self.stats, self.book = info, stats, book

        async def get_json(self, path, params=None):
            if path.endswith("exchangeInfo"):
                return self.info, 1_700_000_000_000
            if path.endswith("ticker/24hr"):
                return self.stats, 1_700_000_000_100
            if path.endswith("ticker/bookTicker"):
                return self.book, 1_700_000_000_200
            raise AssertionError(f"unexpected endpoint {path}")

        async def close(self):
            return None

    spot_markets = [market("SOLUSDT", "SOL", "USDT"), market("MEWUSDT", "MEW", "USDT"),
                    market("BADUSDT", "BAD", "USDT")]
    spot_stats = [
        {"symbol": "SOLUSDT", "quoteVolume": "900000000", "lastPrice": "150", "priceChangePercent": "-2.5", "closeTime": 1_700_000_000_000, "bidPrice": "149.9", "askPrice": "150.1", "bidQty": "10", "askQty": "12"},
        {"symbol": "MEWUSDT", "quoteVolume": "2200000", "lastPrice": "0.004", "priceChangePercent": "22", "closeTime": 1_700_000_000_000},
        {"symbol": "BADUSDT", "quoteVolume": "2200000", "lastPrice": "0.004", "priceChangePercent": "-5", "closeTime": 1_700_000_000_000, "bidPrice": "0", "askPrice": "0.005"},
    ]
    book = [
        {"symbol": symbol, "bidPrice": "1.0", "askPrice": "1.01", "bidQty": "100", "askQty": "90", "time": 1_700_000_000_150, "u": index}
        for index, symbol in enumerate(("SOLUSDT", "MEWUSDT"), 1)
    ]
    book.append({"symbol": "BADUSDT", "bidPrice": "0", "askPrice": "1", "bidQty": "1", "askQty": "1", "time": 1_700_000_000_150, "u": 3})
    future_markets = [market("MEWUSDT", "MEW", "USDT", kind="perpetual")]
    future_stats = [{"symbol": "MEWUSDT", "quoteVolume": "3100000", "lastPrice": "0.004", "priceChangePercent": "26", "closeTime": 1_700_000_000_000}]
    await provider.spot.close()
    await provider.futures.close()
    provider.spot = FixtureHTTP("https://data-api.binance.vision", "/api/v3", {"symbols": spot_markets}, spot_stats, book)
    provider.futures = FixtureHTTP("https://fapi.binance.com", "/fapi/v1", {"symbols": future_markets}, future_stats, book[1:])
    scopes = await provider.discover_catalog()
    assert [scope.scope for scope in scopes] == ["spot", "perpetual"]
    assert all(scope.complete for scope in scopes)
    sol = next(item for item in scopes[0].entries if item.instrument.venue_symbol == "SOLUSDT")
    mew = next(item for item in scopes[0].entries if item.instrument.base == "MEW")
    bad = next(item for item in scopes[0].entries if item.instrument.base == "BAD")
    assert sol.instrument.canonical_asset_id == "native:solana"
    assert sol.quote_turnover_24h == 900_000_000
    assert sol.abs_price_change_24h_pct == 2.5
    assert "ticker_bbo_invalid" in bad.reasons and not bad.admitted
    assert scopes[0].complete  # a damaged ticker row cannot invalidate the listing catalog
    assert mew.instrument.canonical_asset_id.startswith("unresolved:binance:")
    assert scopes[1].entries[0].instrument.market_type == MarketType.PERPETUAL
    assert scopes[1].entries[0].admitted  # derivatives 24h ticker omits BBO; bulk bookTicker enriches it later
    assert "ticker_bbo_unavailable" not in scopes[1].entries[0].reasons
    bbo_rows = await provider.refresh_bbo_batch((sol.instrument, mew.instrument, bad.instrument))
    assert len(bbo_rows) == 2
    assert all(row.quote_volume_24h is None for row in bbo_rows)

    provider.spot.stats = [{
        "symbol": "SOLUSDT", "quoteVolume": "900000000", "lastPrice": "150",
        "priceChangePercent": "-2.5", "closeTime": 1_699_000_000_000,
    }]
    stale_scopes = await provider.discover_catalog()
    stale_sol = next(item for item in stale_scopes[0].entries if item.instrument.base == "SOL")
    assert stale_scopes[0].complete and "ticker_timestamp_stale" in stale_sol.reasons
    assert stale_sol.quote_turnover_24h is None

    provider.spot.stats = [{
        "symbol": "SOLUSDT", "quoteVolume": "900000000", "lastPrice": "150",
        "priceChangePercent": "-2.5", "closeTime": 1_700_000_010_000,
    }]
    future_scopes = await provider.discover_catalog()
    future_sol = next(item for item in future_scopes[0].entries if item.instrument.base == "SOL")
    assert future_scopes[0].complete and "ticker_timestamp_future" in future_sol.reasons
    assert future_sol.last_price is None
    await provider.close()


@pytest.mark.asyncio
async def test_coinbase_catalog_pages_are_bounded_and_turnover_uses_quote_units():
    config, instruments = loaded()
    provider = CoinbaseProvider(config["providers"]["coinbase"], instruments)

    class FixtureHTTP:
        base_url = "https://api.coinbase.com"
        calls = 0
        cursors = []

        async def get_json(self, path, params=None):
            self.calls += 1
            self.cursors.append(None if params is None else params.get("cursor"))
            fixture = "coinbase_public_products_page_1.json" if self.calls == 1 else "coinbase_public_products_page_2.json"
            return json.loads((FIXTURES / fixture).read_text(encoding="utf-8")), 1_700_000_000_000

        async def close(self):
            return None

    await provider.http.close()
    provider.http = FixtureHTTP()
    scopes = await provider.discover_catalog()
    assert len(scopes) == 1 and scopes[0].complete and scopes[0].page_count == 2
    assert provider.http.calls == 2 and provider.http.cursors == [None, "cursor-page-2"]
    item = scopes[0].entries[0]
    assert item.quote_turnover_24h == 50_000
    assert item.instrument.price_decimals == 2 and item.instrument.size_decimals == 4
    assert item.instrument.canonical_asset_id.startswith("unresolved:coinbase:")
    assert len(scopes[0].entries) == 2
    await provider.close()


@pytest.mark.asyncio
async def test_coinbase_catalog_fails_closed_for_repeated_cursor_page_cap_and_empty_page():
    config, instruments = loaded()
    provider = CoinbaseProvider(config["providers"]["coinbase"], instruments)
    first = json.loads((FIXTURES / "coinbase_public_products_page_1.json").read_text(encoding="utf-8"))

    class FixtureHTTP:
        base_url = "https://api.coinbase.com"
        calls = 0
        mode = "repeat"

        async def get_json(self, _path, params=None):
            self.calls += 1
            if self.mode == "empty":
                return {"products": [], "pagination": {"has_next": False, "next_cursor": ""}}, 1_700_000_000_000
            if self.mode == "cap":
                return first, 1_700_000_000_000
            if self.calls == 1:
                return first, 1_700_000_000_000
            repeated = dict(first)
            repeated["products"] = json.loads(
                (FIXTURES / "coinbase_public_products_page_2.json").read_text(encoding="utf-8")
            )["products"]
            repeated["pagination"] = {"has_next": True, "next_cursor": "cursor-page-2"}
            return repeated, 1_700_000_000_100

        async def close(self):
            return None

    await provider.http.close()
    provider.http = FixtureHTTP()
    scopes = await provider.discover_catalog()
    assert not scopes[0].complete and "cursor" in (scopes[0].error or "")
    assert provider.http.calls == 2

    provider.http.mode = "cap"
    provider.http.calls = 0
    provider.config["catalog_max_pages"] = 1
    scopes = await provider.discover_catalog()
    assert not scopes[0].complete and "exceeded 1 pages" in (scopes[0].error or "")

    provider.http.mode = "empty"
    provider.http.calls = 0
    provider.config["catalog_max_pages"] = 20
    scopes = await provider.discover_catalog()
    assert not scopes[0].complete and "empty product page" in (scopes[0].error or "")
    await provider.close()


@pytest.mark.asyncio
async def test_coinbase_bad_quote_metric_keeps_identity_without_invalidating_catalog():
    config, instruments = loaded()
    provider = CoinbaseProvider(config["providers"]["coinbase"], instruments)

    class FixtureHTTP:
        base_url = "https://api.coinbase.com"

        async def get_json(self, _path, _params=None):
            return {
                "products": [
                    {"product_id": "GOOD-USD", "base_currency_id": "GOOD", "quote_currency_id": "USD",
                     "product_type": "SPOT", "status": "online", "price": "1", "volume_24h": "1000000",
                     "base_increment": "0.01", "quote_increment": "0.01"},
                    {"product_id": "BAD-USD", "base_currency_id": "BAD", "quote_currency_id": "USD",
                     "product_type": "SPOT", "status": "online", "price": "0", "volume_24h": "1000000",
                     "base_increment": "0.01", "quote_increment": "0.01"},
                ],
                "pagination": {"has_next": False, "next_cursor": ""},
            }, 1_700_000_000_000

        async def close(self):
            return None

    await provider.http.close()
    provider.http = FixtureHTTP()
    scopes = await provider.discover_catalog()
    assert scopes[0].complete and len(scopes[0].entries) == 2
    good, bad = scopes[0].entries
    assert good.admitted
    assert not bad.admitted and "last_price_unavailable_or_invalid" in bad.reasons
    assert bad.instrument.venue_symbol == "BAD-USD"
    await provider.close()


@pytest.mark.asyncio
async def test_kraken_catalog_uses_native_pair_identity_and_quote_turnover():
    config, instruments = loaded()
    provider = KrakenProvider(config["providers"]["kraken"], instruments)

    class FixtureHTTP:
        base_url = "https://api.kraken.com"

        async def get_json(self, path, params=None):
            if path.endswith("AssetPairs"):
                return {"error": [], "result": {
                    "MEWUSD": {"base": "MEW", "quote": "ZUSD", "wsname": "MEW/USD", "altname": "MEWUSD",
                                "pair_decimals": 4, "lot_decimals": 6, "status": "online"},
                    "XXBTZUSD": {"base": "XXBT", "quote": "ZUSD", "wsname": "XBT/USD", "altname": "XBTUSD",
                                 "pair_decimals": 1, "lot_decimals": 8, "status": "online"},
                    "HALTEDUSD": {"base": "HALTED", "quote": "ZUSD", "wsname": "HALTED/USD", "altname": "HALTEDUSD",
                                  "pair_decimals": 4, "lot_decimals": 4, "status": "delisted"},
                    "ZEROUSD": {"base": "ZERO", "quote": "ZUSD", "wsname": "ZERO/USD", "altname": "ZEROUSD",
                                "pair_decimals": 4, "lot_decimals": 4, "status": "online"},
                }}, 1_700_000_000_000
            return {"error": [], "result": {
                "MEWUSD": {"a": ["0.051", "1", "90"], "b": ["0.050", "1", "80"],
                            "c": ["0.0505", "2"], "v": ["100", "1000"]},
                "XXBTZUSD": {"a": ["65001", "1", "0.5"], "b": ["65000", "1", "0.4"],
                             "c": ["65000.5", "2"], "v": ["100", "1000"]},
                "HALTEDUSD": {"a": ["0"], "b": [], "c": [], "v": []},
                "ZEROUSD": {"a": ["0.11", "1"], "b": ["0", "1"], "c": ["0.1", "1"], "v": ["10", "100"]},
            }}, 1_700_000_000_100

        async def close(self):
            return None

    await provider.http.close()
    provider.http = FixtureHTTP()
    scopes = await provider.discover_catalog()
    assert scopes[0].complete
    assert len(scopes[0].entries) == 4
    item = scopes[0].entries[0]
    assert item.instrument.venue_symbol == "MEW/USD"
    assert item.quote_turnover_24h == pytest.approx(50.5)
    assert item.bid == pytest.approx(0.050) and item.ask == pytest.approx(0.051)
    bitcoin = next(entry for entry in scopes[0].entries if entry.instrument.venue_symbol == "XBT/USD")
    assert bitcoin.instrument.instrument_id == "kraken:spot:BTC-USD"
    halted = next(entry for entry in scopes[0].entries if entry.instrument.base == "HALTED")
    zero = next(entry for entry in scopes[0].entries if entry.instrument.base == "ZERO")
    assert not halted.active and "market_not_trading" in halted.reasons
    assert not zero.admitted and "ticker_bbo_unavailable_or_invalid" in zero.reasons
    bbo_rows = await provider.refresh_bbo_batch(tuple(entry.instrument for entry in scopes[0].entries))
    assert {row.metadata.instrument_id for row in bbo_rows} == {
        item.instrument.instrument_id for item in scopes[0].entries if item.instrument.base in {"MEW", "XBT"}
    }
    assert bitcoin.instrument.canonical_asset_id == "native:bitcoin"
    refreshed = await provider.refresh_bbo_batch((bitcoin.instrument, item.instrument))
    assert len(refreshed) == 2
    assert next(event for event in refreshed if event.metadata.instrument_id == bitcoin.instrument.instrument_id).bid == 65000
    await provider.close()


@pytest.mark.asyncio
async def test_provider_disconnect_is_health_isolated():
    config, instruments = loaded()
    provider = BinanceProvider(config["providers"]["binance"], instruments)

    class DisconnectedHTTP:
        base_url = "https://data-api.binance.vision"

        async def get_json(self, *_args, **_kwargs):
            raise ProviderError("fixture disconnect")

        async def close(self):
            return None

    await provider.spot.close()
    provider.spot = DisconnectedHTTP()
    with pytest.raises(ProviderError, match="disconnect"):
        await provider.fetch_bbo(instruments.get("binance:spot:SOL-USDT"))
    health = provider.health()
    assert health.status == ProviderStatus.DISCONNECTED
    assert health.consecutive_failures == 1
    await provider.close()


def test_duplicates_and_out_of_order_do_not_overwrite_latest_state():
    config, instruments = loaded()
    engine = MarketTruthEngine(instruments, config["market_truth"])
    instrument = instruments.get("coinbase:spot:SOL-USD")
    latest = bbo(instrument, 100, 10_000, event="new", exchange=9_000)
    older = bbo(instrument, 99, 10_100, event="old", exchange=8_000)
    assert engine.ingest(latest)
    assert not engine.ingest(latest)
    assert not engine.ingest(older)
    assert engine.latest_bbo(instrument.instrument_id) == latest


def test_stale_source_blocks_price_and_single_outlier_is_rejected():
    config, instruments = loaded()
    third = independent_sol_usd()
    instruments.register(third)
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    selected = (
        instruments.get("coinbase:spot:SOL-USD"),
        instruments.get("kraken:spot:SOL-USD"),
        third,
    )
    for instrument, price, event in zip(selected, (100.0, 100.2, 160.0), ("a", "b", "outlier"), strict=True):
        assert engine.ingest(bbo(instrument, price, now - 100, event=event))
    truth = engine.calculate("native:solana", now)
    assert truth.canonical_price == pytest.approx(100.1, abs=0.2)
    assert truth.quorum == 2
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.rejected_outliers == ("fixture:spot:SOL-USD",)

    stale_engine = MarketTruthEngine(instruments, config["market_truth"])
    stale_engine.ingest(bbo(selected[0], 100, now - 100, event="fresh"))
    stale_engine.ingest(bbo(selected[1], 100.1, now - 30_000, event="stale"))
    stale = stale_engine.calculate("native:solana", now)
    assert stale.canonical_price is None
    assert stale.status == ProviderStatus.DEGRADED
    assert stale.quorum == 1


def test_fresh_trade_replaces_stale_bbo_as_truth_fallback():
    config, instruments = loaded()
    binance = independent_sol_usd()
    instruments.register(binance)
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    coinbase = instruments.get("coinbase:spot:SOL-USD")
    engine.ingest(bbo(binance, 90, now - 30_000, event="stale-book"))
    engine.ingest(NormalizedTrade(
        EventMetadata(
            binance.provider, binance.venue, binance.instrument_id,
            now - 120, now - 100, "fixture://trade", event_id="fresh-trade",
        ),
        Side.BUY, 100.0, 1.0, "fresh-trade",
    ))
    engine.ingest(bbo(coinbase, 100.1, now - 100, event="fresh-book"))
    truth = engine.calculate("native:solana", now)
    assert truth.quorum == 2
    assert truth.canonical_price == pytest.approx(100.05, abs=0.1)


def test_two_strongly_divergent_sources_do_not_publish_false_consensus():
    config, instruments = loaded()
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    engine.ingest(bbo(instruments.get("kraken:spot:SOL-USD"), 100, now - 50, event="left"))
    engine.ingest(bbo(instruments.get("coinbase:spot:SOL-USD"), 130, now - 50, event="right"))
    truth = engine.calculate("native:solana", now)
    assert truth.quorum == 2
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.canonical_price is None
    assert truth.venue_divergence_bps is not None
    assert truth.venue_divergence_bps > config["market_truth"]["outlier_threshold_bps"]
    assert "cannot be identified" in str(truth.reason)


def test_different_quote_assets_never_contribute_to_same_quorum():
    config, instruments = loaded()
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    engine.ingest(bbo(instruments.get("binance:spot:SOL-USDT"), 100.0, now - 50, event="usdt"))
    engine.ingest(bbo(instruments.get("coinbase:spot:SOL-USD"), 100.0, now - 50, event="usd"))
    truth = engine.calculate("native:solana", now)
    assert truth.canonical_price is None
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.quorum == 1
    assert truth.reason == "fresh venue quorum not met"


def test_one_quote_group_with_independent_provider_quorum_is_explicitly_labeled():
    config, instruments = loaded()
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    engine.ingest(bbo(instruments.get("coinbase:spot:SOL-USD"), 100.0, now - 50, event="usd-1"))
    engine.ingest(bbo(instruments.get("kraken:spot:SOL-USD"), 100.1, now - 50, event="usd-2"))
    engine.ingest(bbo(instruments.get("binance:spot:SOL-USDT"), 100.2, now - 50, event="usdt"))
    truth = engine.calculate("native:solana", now)
    assert truth.canonical_price is not None
    assert truth.canonical_quote == "USD"
    assert truth.quorum == 2
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.uncomparable_sources == ("binance:spot:SOL-USDT",)


def test_outlier_rejection_does_not_publish_below_required_quorum():
    config, instruments = loaded()
    settings = copy.deepcopy(config["market_truth"])
    settings["minimum_quorum"] = 3
    engine = MarketTruthEngine(instruments, settings)
    now = 1_700_000_100_000
    selected = (
        instruments.get("coinbase:spot:SOL-USD"),
        instruments.get("kraken:spot:SOL-USD"),
        independent_sol_usd(),
    )
    instruments.register(selected[2])
    for instrument, price, event in zip(selected, (100.0, 100.1, 170.0), ("a", "b", "outlier"), strict=True):
        engine.ingest(bbo(instrument, price, now - 100, event=event))
    truth = engine.calculate("native:solana", now)
    assert truth.rejected_outliers == ("fixture:spot:SOL-USD",)
    assert truth.quorum == 2 and truth.required_quorum == 3
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.canonical_price is None


def test_hype_single_independent_venue_remains_degraded_without_price():
    config, instruments = loaded()
    engine = MarketTruthEngine(instruments, config["market_truth"])
    now = 1_700_000_100_000
    instrument = instruments.get("hyperliquid:perpetual:HYPE-USDC")
    engine.ingest(bbo(instrument, 80.0, now - 100, event="hype-only"))
    truth = engine.calculate("native:hyperliquid", now)
    assert truth.quorum == 1 and truth.required_quorum == 2
    assert truth.status == ProviderStatus.DEGRADED
    assert truth.canonical_price is None


@pytest.mark.asyncio
async def test_one_provider_disconnect_does_not_stop_other_providers(tmp_path: Path):
    config, _ = loaded()
    storage = SQLiteStorage(tmp_path / "provider-isolation.db")
    repository = MarketRepository(storage)
    service = MultiVenueMarketService(config, repository)
    checked_at = int(time.time() * 1000)

    class FakeProvider:
        def __init__(self, provider_id: str, disconnected: bool = False):
            self.provider_id = provider_id
            self.disconnected = disconnected

        async def fetch_bbo(self, instrument):
            if self.disconnected:
                raise ProviderError("fixture provider disconnect")
            base_price = {
                "native:solana": 100.0,
                "native:bitcoin": 80_000.0,
                "native:hyperliquid": 80.0,
            }[instrument.canonical_asset_id]
            offset = {"hyperliquid": 0.00, "coinbase": 0.01, "kraken": -0.01}.get(self.provider_id, 0.0)
            return bbo(instrument, base_price * (1 + offset / 100), checked_at, event=instrument.instrument_id)

        def health(self):
            return ProviderHealth(
                provider=self.provider_id,
                status=ProviderStatus.DISCONNECTED if self.disconnected else ProviderStatus.HEALTHY,
                checked_at_ms=checked_at,
                last_received_at_ms=None if self.disconnected else checked_at,
                lag_ms=None if self.disconnected else 0,
                consecutive_failures=1 if self.disconnected else 0,
                last_error="fixture provider disconnect" if self.disconnected else None,
            )

        async def close(self):
            return None

    service.providers._providers = {
        provider_id: FakeProvider(provider_id, disconnected=provider_id == "binance")
        for provider_id in ("hyperliquid", "binance", "coinbase", "kraken")
    }
    try:
        result = await service.refresh_once(include_deep=False)
        assert result["providers"]["binance"]["status"] == "disconnected"
        assert all(result["providers"][name]["status"] == "healthy" for name in ("hyperliquid", "coinbase", "kraken"))
        assert result["canonical"]["native:solana"]["price"] is not None
        assert result["canonical"]["native:bitcoin"]["price"] is not None
        assert result["canonical"]["native:hyperliquid"]["status"] == "degraded"
        assert result["canonical"]["native:hyperliquid"]["price"] is None
        assert result["errors"] and all(key.startswith("binance:") for key in result["errors"])
    finally:
        await service.stop()
        storage.close()


@pytest.mark.asyncio
async def test_per_instrument_bbo_requests_use_provider_rate_gate(tmp_path: Path):
    config, instruments = loaded()
    config = copy.deepcopy(config)
    config["providers"] = {"coinbase": config["providers"]["coinbase"]}
    config["instruments"] = [
        item for item in config["instruments"] if item["provider"] == "coinbase"
    ]
    config["universe"]["max_deep_watchlist"] = 30
    storage = SQLiteStorage(tmp_path / "bbo-rate-gate.db")
    service = MultiVenueMarketService(config, MarketRepository(storage))
    checked_at = int(time.time() * 1000)

    class PerProductProvider:
        provider_id = "coinbase"
        bbo_batch_mode = "per_instrument"

        async def fetch_bbo(self, instrument):
            return bbo(instrument, 100.0, int(time.time() * 1000), event=instrument.instrument_id)

        def health(self):
            return ProviderHealth("coinbase", ProviderStatus.HEALTHY, checked_at, checked_at, 0)

        async def close(self):
            return None

    service.providers._providers = {"coinbase": PerProductProvider()}
    try:
        result = await service.refresh_once(include_deep=False)
        metrics = result["request_metrics"]["coinbase"]
        assert metrics["requests"] == len(config["instruments"])
        assert metrics["maximum_in_flight"] <= 4
        assert metrics["rate_waits"] >= len(config["instruments"]) - 1
    finally:
        await service.stop()
        storage.close()


@pytest.mark.asyncio
async def test_normal_market_service_discovers_ranks_persists_and_delists_markets(tmp_path: Path):
    config, _ = loaded()
    config = copy.deepcopy(config)
    config["providers"] = {"binance": config["providers"]["binance"]}
    config["instruments"] = []
    config["universe"]["bucket_minimums"] = {
        "majors_l1": 0, "derivatives_movers": 0,
        "dex_memecoin_candidates": 0, "liquid_spot_candidates": 0,
    }
    storage = SQLiteStorage(tmp_path / "market-discovery.db")
    repository = MarketRepository(storage)
    service = MultiVenueMarketService(config, repository)
    now = int(time.time() * 1000)

    def market(symbol: str, base: str) -> Instrument:
        return Instrument(
            f"discovered:binance:binance-spot:spot:{symbol}",
            f"unresolved:binance:binance-spot:spot:{base}:USDT:{symbol}",
            "binance", "binance-spot", MarketType.SPOT, base, "USDT", symbol, 6, 4,
            metadata={"catalog_scope": "spot"},
        )

    class DynamicProvider:
        provider_id = "binance"
        bbo_batch_mode = "bulk"

        def __init__(self):
            self.generation = 0
            self.checked = now

        async def discover_catalog(self):
            self.checked = int(time.time() * 1000)
            instrument = market("MEWUSDT", "MEW") if self.generation == 0 else market("PONKEUSDT", "PONKE")
            if self.generation == 2:
                return (MarketCatalogScope("binance", "spot", "https://data-api.binance.vision/api/v3/exchangeInfo",
                                           self.checked, (), False, error="partial page fixture"),)
            entry = MarketCatalogEntry(
                instrument, "spot", "https://data-api.binance.vision/api/v3/ticker/24hr",
                self.checked, self.checked - 5, 2_500_000, 0.05,
                abs_price_change_24h_pct=18.0, active=True, admitted=True,
            )
            return (MarketCatalogScope("binance", "spot", "https://data-api.binance.vision/api/v3/exchangeInfo+24hr",
                                       self.checked, (entry,), True),)

        async def refresh_bbo_batch(self, instruments):
            received = int(time.time() * 1000)
            return tuple(NormalizedBBO(
                EventMetadata("binance", "binance-spot", item.instrument_id,
                              received - 5, received, "fixture://bulk-bbo",
                              event_id=f"{item.venue_symbol}:{received}"),
                0.04995, 0.05005, 100_000, 95_000, 2_500_000,
            ) for item in instruments)

        async def fetch_bbo(self, instrument):
            raise AssertionError("service must use the bounded batch quote path")

        def health(self):
            return ProviderHealth("binance", ProviderStatus.HEALTHY, self.checked, self.checked, 0)

        async def close(self):
            return None

    provider = DynamicProvider()
    service.providers._providers = {"binance": provider}
    try:
        first = await service.refresh_once()
        first_id = "discovered:binance:binance-spot:spot:MEWUSDT"
        assert first["universe_size"] == 1
        assert service.instruments.get(first_id).active
        first_decision = repository.market_discovery_decisions(limit=5)
        assert any(row["instrument_id"] == first_id and row["decision"] == "ADMITTED" for row in first_decision)
        assert any(row["instrument_id"] == first_id and row["source"].startswith("https://") for row in first_decision)

        provider.generation = 1
        service._last_catalog_refresh_ms = 0
        second = await service.refresh_once()
        second_id = "discovered:binance:binance-spot:spot:PONKEUSDT"
        assert second["universe_size"] == 1, (second["errors"], service.instruments.all(), service.truth_engine.latest_bbo("discovered:binance:binance-spot:spot:PONKEUSDT"), service._catalog_health(), service.membership)
        assert not service.instruments.get(first_id).active
        decisions = {row["instrument_id"]: row for row in repository.market_discovery_decisions(limit=10)}
        assert decisions[first_id]["decision"] == "DELISTED"
        assert decisions[second_id]["decision"] == "ADMITTED"

        provider.generation = 2
        service._last_catalog_refresh_ms = 0
        third = await service.refresh_once()
        assert third["universe_size"] == 0
        assert service.instruments.get(second_id).active  # unknown listing status is not treated as delisting
        assert service._catalog_health()["binance:spot"]["status"] == "stale"
        stale_decisions = {row["instrument_id"]: row for row in repository.market_discovery_decisions(limit=10)}
        assert stale_decisions[second_id]["decision"] == "STALE_CATALOG"
        assert storage.health()["integrity"] == "ok"
    finally:
        await service.stop()
        storage.close()


def test_ticker_collision_never_merges_distinct_contracts():
    first = Instrument(
        "dex-a:spot:ABC-USDC", "solana:mint-a", "dex-a", "dex-a", MarketType.SPOT,
        "ABC", "USDC", "mint-a", 6, 6, "solana", "MintA",
    )
    second = Instrument(
        "dex-b:spot:ABC-USDC", "base:0xbbb", "dex-b", "dex-b", MarketType.SPOT,
        "ABC", "USDC", "0xbbb", 6, 6, "base", "0xBbB",
    )
    registry = InstrumentRegistry((first, second))
    assert registry.resolve_onchain("solana", "minta") == first
    assert registry.resolve_onchain("base", "0xbbb") == second
    assert registry.for_canonical_asset(first.canonical_asset_id) == (first,)
    assert registry.for_canonical_asset(second.canonical_asset_id) == (second,)


def test_dynamic_universe_enforces_scan_deep_and_provider_budgets():
    _, instruments = loaded()
    settings = {
        "max_scan_instruments": 4,
        "max_deep_watchlist": 2,
        "bucket_minimums": {"majors_l1": 1, "derivatives_movers": 1, "dex_memecoin_candidates": 0},
        "weights": {"liquidity": 0.35, "volume": 0.25, "change_magnitude": 0.15, "derivatives_activity": 0.15, "freshness": 0.10},
    }
    universe = DynamicUniverse(instruments, settings)
    candidates = [
        UniverseCandidate(item.instrument_id,
                          UniverseBucket.DERIVATIVES_MOVERS if item.market_type == MarketType.PERPETUAL else UniverseBucket.MAJORS_L1,
                          1000 + index, 10000 + index, index / 100, float(item.market_type == MarketType.PERPETUAL), 1.0)
        for index, item in enumerate(instruments.all())
    ]
    membership = universe.select(candidates)
    assert len(membership) == 4
    assert sum(item.deep_watchlist for item in membership) == 2
    plan = universe.subscription_plan(membership, {"hyperliquid": 1, "binance": 2, "coinbase": 1, "kraken": 1})
    assert all(set(tiers["deep"]).issubset(tiers["scan"]) for tiers in plan.values())
    assert all(len(tiers["scan"]) <= {"hyperliquid": 1, "binance": 2, "coinbase": 1, "kraken": 1}[provider]
               for provider, tiers in plan.items())


def test_deep_watchlist_reserves_spot_analysis_slots():
    instruments = InstrumentRegistry(tuple(
        Instrument(f"binance:{'spot' if index < 3 else 'perpetual'}:COIN{index}-USDT",
            f"unresolved:binance:COIN{index}", "binance", "binance-spot" if index < 3 else "binance-usdm",
            MarketType.SPOT if index < 3 else MarketType.PERPETUAL,
            f"COIN{index}", "USDT", f"COIN{index}USDT", 4, 4)
        for index in range(8)))
    settings = {"max_scan_instruments": 8, "max_deep_watchlist": 4,
        "bucket_minimums": {"liquid_spot_candidates": 2},
        "deep_bucket_minimums": {"liquid_spot_candidates": 2},
        "weights": {"liquidity": 0.0, "volume": 1.0, "change_magnitude": 0.0,
            "derivatives_activity": 0.0, "freshness": 0.0}}
    universe = DynamicUniverse(instruments, settings)
    candidates = [UniverseCandidate(item.instrument_id,
        UniverseBucket.LIQUID_SPOT_CANDIDATES if item.market_type == MarketType.SPOT else UniverseBucket.DERIVATIVES_MOVERS,
        1000, 10 + index * 100, 1, 1, 1, quote_asset="USDT")
        for index, item in enumerate(instruments.all())]
    membership = universe.select(candidates)
    deep = [item for item in membership if item.deep_watchlist]
    assert len(deep) == 4
    assert sum(item.bucket == UniverseBucket.LIQUID_SPOT_CANDIDATES for item in deep) >= 2


def test_additive_storage_v5_catalog_lineage_and_duplicate_idempotency(tmp_path: Path):
    config, instruments = loaded()
    storage = SQLiteStorage(tmp_path / "phase1.db")
    repository = MarketRepository(storage)
    try:
        assert storage.health()["schema_version"] == SCHEMA_VERSION == 13
        repository.sync_provider_catalog(config["providers"], instruments, 1_700_000_000_000)
        event = bbo(instruments.get("coinbase:spot:SOL-USD"), 100, 1_700_000_000_100, event="unique")
        assert repository.save_normalized_event(event)
        assert not repository.save_normalized_event(event)
        assert storage.query("SELECT COUNT(*) AS n FROM venues")[0]["n"] >= 4
        assert storage.query("SELECT COUNT(*) AS n FROM instruments")[0]["n"] == len(instruments)
        assert storage.query("SELECT COUNT(*) AS n FROM data_lineage")[0]["n"] == 1
        assert storage.query("SELECT COUNT(*) AS n FROM normalized_ticks")[0]["n"] == 1
        assert storage.health()["integrity"] == "ok"
    finally:
        storage.close()


def test_schema_v4_upgrade_preserves_existing_runtime_data(tmp_path: Path):
    path = tmp_path / "legacy-v4.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
        INSERT INTO schema_migrations VALUES (4, 1700000000000);
        CREATE TABLE signals(
          id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
          primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
          payload_json TEXT NOT NULL, model_status TEXT NOT NULL
        );
        INSERT INTO signals(coin,timestamp,primary_horizon,direction,probability,payload_json,model_status)
        VALUES ('SOL',1700000000000,'15m','WAIT',50.0,'{}','MODEL_WARMING_UP');
    """)
    connection.commit()
    connection.close()

    storage = SQLiteStorage(path)
    try:
        assert storage.health()["schema_version"] == 13
        assert storage.query("SELECT coin,direction FROM signals") == [{"coin": "SOL", "direction": "WAIT"}]
        assert storage.query("SELECT COUNT(*) AS n FROM instruments")[0]["n"] == 0
        assert [row["version"] for row in storage.query("SELECT version FROM schema_migrations ORDER BY version")] == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13]
        assert storage.health()["integrity"] == "ok"
    finally:
        storage.close()
