from __future__ import annotations

from collections import deque
import math
import statistics
from typing import Mapping

from .instruments import InstrumentRegistry
from .types import (
    CanonicalMarketTruth,
    MarketType,
    NormalizedBBO,
    NormalizedEvent,
    NormalizedTrade,
    ProviderHealth,
    ProviderStatus,
    VenueContribution,
)


class MarketTruthEngine:
    """Causal multi-venue state with explicit quorum and degraded-mode semantics."""

    def __init__(self, instruments: InstrumentRegistry, settings: Mapping[str, object]):
        self.instruments = instruments
        self.minimum_quorum = int(settings.get("minimum_quorum", 2))
        self.max_source_age_ms = int(float(settings.get("max_source_age_seconds", 20)) * 1000)
        self.freshness_half_life_ms = max(
            1.0, float(settings.get("freshness_half_life_seconds", 5)) * 1000
        )
        self.latency_scale_ms = max(1.0, float(settings.get("latency_scale_ms", 1000)))
        self.outlier_threshold_bps = float(settings.get("outlier_threshold_bps", 150))
        self.usd_equivalent_quotes = {
            str(value).upper() for value in settings.get("usd_equivalent_quotes", ["USD", "USDC", "USDT"])
        }
        if str(settings.get("quote_normalization", "none")).lower() != "none":
            raise ValueError("quote normalization is unavailable without verified FX observations")
        if self.minimum_quorum < 2 or self.max_source_age_ms <= 0 or self.outlier_threshold_bps <= 0:
            raise ValueError("invalid market truth quorum/freshness/outlier settings")
        self._bbo: dict[str, NormalizedBBO] = {}
        self._trades: dict[str, NormalizedTrade] = {}
        self._latest_state_key: dict[tuple[str, str], tuple[int, int, int]] = {}
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque(maxlen=200_000)
        self._provider_health: dict[str, ProviderHealth] = {}

    def update_provider_health(self, health: ProviderHealth) -> None:
        self._provider_health[health.provider] = health

    def ingest(self, event: NormalizedEvent) -> bool:
        lineage_id = event.metadata.lineage_id
        if lineage_id in self._seen:
            return False
        if len(self._seen_order) == self._seen_order.maxlen:
            evicted = self._seen_order.popleft()
            self._seen.discard(evicted)
        self._seen.add(lineage_id)
        self._seen_order.append(lineage_id)

        event_type = type(event).__name__
        state_key = event.metadata.instrument_id, event_type
        ordering = (
            event.metadata.exchange_timestamp_ms,
            event.metadata.sequence if event.metadata.sequence is not None else -1,
            event.metadata.received_at_ms,
        )
        previous = self._latest_state_key.get(state_key)
        if previous is not None and ordering <= previous:
            return False
        self._latest_state_key[state_key] = ordering
        if isinstance(event, NormalizedBBO):
            self._bbo[event.metadata.instrument_id] = event
        elif isinstance(event, NormalizedTrade):
            self._trades[event.metadata.instrument_id] = event
        return True

    def latest_bbo(self, instrument_id: str) -> NormalizedBBO | None:
        return self._bbo.get(instrument_id)

    def latest_trade(self, instrument_id: str) -> NormalizedTrade | None:
        return self._trades.get(instrument_id)

    def _source_usable(self, provider: str) -> bool:
        health = self._provider_health.get(provider)
        return health is None or health.status not in {
            ProviderStatus.DISCONNECTED,
            ProviderStatus.STALE,
            ProviderStatus.DISABLED,
        }

    def calculate(self, canonical_asset_id: str, calculated_at_ms: int) -> CanonicalMarketTruth:
        candidates: list[tuple[object, float, int, int | None, float | None, float | None]] = []
        stale_sources: list[str] = []
        for instrument in self.instruments.for_canonical_asset(canonical_asset_id):
            if instrument.quote.upper() not in self.usd_equivalent_quotes:
                continue
            available_events = [
                event for event in (
                    self._bbo.get(instrument.instrument_id),
                    self._trades.get(instrument.instrument_id),
                )
                if event is not None
            ]
            if not available_events:
                stale_sources.append(instrument.instrument_id)
                continue
            event = max(available_events, key=lambda item: item.metadata.received_at_ms)
            age_ms = max(0, int(calculated_at_ms) - event.metadata.received_at_ms)
            if age_ms > self.max_source_age_ms or not self._source_usable(instrument.provider):
                stale_sources.append(instrument.instrument_id)
                continue
            latency_ms = (
                max(0, event.metadata.received_at_ms - event.metadata.exchange_timestamp_ms)
                if event.metadata.exchange_time_kind == "EXCHANGE_EVENT" else None
            )
            if isinstance(event, NormalizedBBO):
                price = event.mid
                spread = event.spread_bps
                volume = event.quote_volume_24h
            else:
                price = event.price
                spread = None
                volume = None
            candidates.append((instrument, price, age_ms, latency_ms, spread, volume))

        if not candidates:
            return CanonicalMarketTruth(
                canonical_asset_id, calculated_at_ms, ProviderStatus.STALE, None, 0, self.minimum_quorum,
                None, None, None, None, (), tuple(sorted(stale_sources)), (), "no fresh provider data",
            )

        quote_groups: dict[str, list[tuple[object, float, int, int | None, float | None, float | None]]] = {}
        for item in candidates:
            quote_groups.setdefault(item[0].quote.upper(), []).append(item)
        qualifying_quotes = [
            quote for quote, group in quote_groups.items()
            if len({item[0].provider for item in group}) >= self.minimum_quorum
        ]
        if len(qualifying_quotes) > 1:
            contributions = tuple(
                VenueContribution(
                    provider=item[0].provider, venue=item[0].venue,
                    instrument_id=item[0].instrument_id, market_type=item[0].market_type,
                    price=item[1], weight=1.0, receive_age_ms=item[2], latency_ms=item[3],
                    spread_bps=item[4], quote_volume_24h=item[5],
                ) for item in candidates
            )
            return CanonicalMarketTruth(
                canonical_asset_id, calculated_at_ms, ProviderStatus.DEGRADED, None,
                max(len({item[0].provider for item in quote_groups[quote]}) for quote in qualifying_quotes),
                self.minimum_quorum, None, None, None, None, (), tuple(sorted(stale_sources)),
                contributions, "multiple quote currencies independently meet quorum; explicit FX normalization required",
                None, tuple(sorted(item[0].instrument_id for item in candidates)),
            )
        if qualifying_quotes:
            canonical_quote = qualifying_quotes[0]
            uncomparable_sources = tuple(sorted(
                item[0].instrument_id for quote, group in quote_groups.items() if quote != canonical_quote for item in group
            ))
            candidates = quote_groups[canonical_quote]
        else:
            # Keep one quote group for diagnostics, but never aggregate unlike quote units.
            canonical_quote = max(
                quote_groups,
                key=lambda quote: (len({item[0].provider for item in quote_groups[quote]}), quote),
            )
            uncomparable_sources = tuple(sorted(
                item[0].instrument_id for quote, group in quote_groups.items() if quote != canonical_quote for item in group
            ))
            candidates = quote_groups[canonical_quote]

        prices = [item[1] for item in candidates]
        center = statistics.median(prices)
        filtered = candidates
        rejected: list[str] = []
        if len(candidates) >= 3:
            filtered = []
            for item in candidates:
                deviation_bps = abs(item[1] / center - 1.0) * 10_000
                if deviation_bps > self.outlier_threshold_bps:
                    rejected.append(item[0].instrument_id)
                else:
                    filtered.append(item)

        # Multiple market types on one exchange are not independent sources.
        quorum = len({item[0].provider for item in filtered})
        contributions: list[VenueContribution] = []
        weighted_prices: list[tuple[float, float]] = []
        for instrument, price, age_ms, latency_ms, spread, volume in filtered:
            freshness_weight = math.pow(0.5, age_ms / self.freshness_half_life_ms)
            latency_weight = 1.0 if latency_ms is None else 1.0 / (1.0 + latency_ms / self.latency_scale_ms)
            liquidity_weight = 1.0 + min(2.0, math.log1p(volume or 0.0) / 20.0)
            weight = freshness_weight * latency_weight * liquidity_weight
            weighted_prices.append((price, weight))
            contributions.append(VenueContribution(
                provider=instrument.provider, venue=instrument.venue,
                instrument_id=instrument.instrument_id, market_type=instrument.market_type,
                price=price, weight=weight, receive_age_ms=age_ms, latency_ms=latency_ms,
                spread_bps=spread, quote_volume_24h=volume,
            ))

        if quorum < self.minimum_quorum:
            return CanonicalMarketTruth(
                canonical_asset_id, calculated_at_ms, ProviderStatus.DEGRADED, None, quorum,
                self.minimum_quorum, None, None, None, None, tuple(sorted(rejected)),
                tuple(sorted(stale_sources)), tuple(contributions), "fresh venue quorum not met",
            )

        total_weight = sum(weight for _, weight in weighted_prices)
        canonical_price = sum(price * weight for price, weight in weighted_prices) / total_weight
        divergence = (
            max(price for price, _ in weighted_prices) - min(price for price, _ in weighted_prices)
        ) / canonical_price * 10_000
        if len(filtered) == 2 and divergence > self.outlier_threshold_bps:
            return CanonicalMarketTruth(
                canonical_asset_id=canonical_asset_id,
                calculated_at_ms=calculated_at_ms,
                status=ProviderStatus.DEGRADED,
                canonical_price=None,
                quorum=quorum,
                required_quorum=self.minimum_quorum,
                venue_divergence_bps=divergence,
                cross_exchange_basis_bps=None,
                weighted_spread_bps=None,
                volume_concentration=None,
                rejected_outliers=tuple(sorted(rejected)),
                stale_sources=tuple(sorted(stale_sources)),
                contributions=tuple(contributions),
                reason="two-source disagreement; outlier cannot be identified safely",
            )

        spot = [(entry.price, entry.weight) for entry in contributions if entry.market_type == MarketType.SPOT]
        perp = [(entry.price, entry.weight) for entry in contributions if entry.market_type == MarketType.PERPETUAL]
        basis = None
        if spot and perp:
            spot_price = sum(price * weight for price, weight in spot) / sum(weight for _, weight in spot)
            perp_price = sum(price * weight for price, weight in perp) / sum(weight for _, weight in perp)
            basis = (perp_price / spot_price - 1.0) * 10_000

        spreads = [(entry.spread_bps, entry.weight) for entry in contributions if entry.spread_bps is not None]
        weighted_spread = (
            sum(float(spread) * weight for spread, weight in spreads) / sum(weight for _, weight in spreads)
            if spreads else None
        )
        volumes = [float(entry.quote_volume_24h) for entry in contributions if entry.quote_volume_24h is not None]
        volume_concentration = None
        if volumes and sum(volumes) > 0:
            volume_total = sum(volumes)
            volume_concentration = sum((volume / volume_total) ** 2 for volume in volumes)

        status = ProviderStatus.DEGRADED if rejected or stale_sources or uncomparable_sources else ProviderStatus.HEALTHY
        return CanonicalMarketTruth(
            canonical_asset_id=canonical_asset_id,
            calculated_at_ms=calculated_at_ms,
            status=status,
            canonical_price=canonical_price,
            quorum=quorum,
            required_quorum=self.minimum_quorum,
            venue_divergence_bps=divergence,
            cross_exchange_basis_bps=basis,
            weighted_spread_bps=weighted_spread,
            volume_concentration=volume_concentration,
            rejected_outliers=tuple(sorted(rejected)),
            stale_sources=tuple(sorted(stale_sources)),
            contributions=tuple(contributions),
            reason=(
                "outlier/stale/incomparable source excluded; quote groups are not normalized"
                if status == ProviderStatus.DEGRADED else None
            ),
            canonical_quote=canonical_quote if canonical_price is not None else None,
            uncomparable_sources=uncomparable_sources,
        )
