from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import math
from typing import Any


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class ProviderStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    DISCONNECTED = "disconnected"
    DISABLED = "disabled"


def _positive(value: float, field_name: str, *, allow_zero: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"{field_name} must be finite and {'non-negative' if allow_zero else 'positive'}")
    return number


@dataclass(frozen=True, slots=True)
class Instrument:
    """Venue identity plus an explicit, non-ticker canonical asset identity."""

    instrument_id: str
    canonical_asset_id: str
    provider: str
    venue: str
    market_type: MarketType
    base: str
    quote: str
    venue_symbol: str
    price_decimals: int
    size_decimals: int
    chain: str | None = None
    contract_address: str | None = None
    active: bool = True
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self) -> None:
        required = (
            self.instrument_id,
            self.canonical_asset_id,
            self.provider,
            self.venue,
            self.base,
            self.quote,
            self.venue_symbol,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("instrument identity fields must be non-empty")
        if self.price_decimals < 0 or self.size_decimals < 0:
            raise ValueError("instrument decimals must be non-negative")
        if bool(self.chain) != bool(self.contract_address):
            raise ValueError("on-chain instruments require both chain and contract_address")

    @property
    def venue_identity(self) -> tuple[str, str, MarketType, str, str, str]:
        return self.provider, self.venue, self.market_type, self.base, self.quote, self.venue_symbol

    @property
    def onchain_identity(self) -> tuple[str, str] | None:
        if self.chain and self.contract_address:
            return self.chain, self.contract_address.lower()
        return None


@dataclass(frozen=True, slots=True)
class MarketCatalogEntry:
    """One venue's observed market metadata and cheap public ticker statistics."""

    instrument: Instrument
    scope: str
    source: str
    received_at_ms: int
    exchange_timestamp_ms: int | None
    quote_turnover_24h: float | None
    last_price: float | None
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    abs_price_change_24h_pct: float | None = None
    active: bool = True
    admitted: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.scope or not self.source or self.received_at_ms <= 0:
            raise ValueError("invalid market catalog lineage")
        for name in ("quote_turnover_24h", "last_price", "bid", "ask", "bid_size", "ask_size", "abs_price_change_24h_pct"):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
                raise ValueError(f"{name} must be finite and non-negative when available")
        if (self.bid is None) != (self.ask is None):
            raise ValueError("catalog BBO must have both bid and ask")
        if self.bid is not None and self.ask is not None and self.bid >= self.ask:
            raise ValueError("catalog BBO must not be crossed")


@dataclass(frozen=True, slots=True)
class MarketCatalogScope:
    """Atomic refresh unit; incomplete scopes never replace a usable catalog."""

    provider: str
    scope: str
    source: str
    received_at_ms: int
    entries: tuple[MarketCatalogEntry, ...]
    complete: bool
    page_count: int = 1
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.scope or not self.source or self.received_at_ms <= 0:
            raise ValueError("invalid catalog scope identity/lineage")
        if self.page_count < 1:
            raise ValueError("catalog page_count must be positive")


@dataclass(frozen=True, slots=True)
class EventMetadata:
    provider: str
    venue: str
    instrument_id: str
    exchange_timestamp_ms: int
    received_at_ms: int
    source: str
    sequence: int | None = None
    event_id: str | None = None
    exchange_time_kind: str = "EXCHANGE_EVENT"

    def __post_init__(self) -> None:
        if not self.provider or not self.venue or not self.instrument_id or not self.source:
            raise ValueError("event metadata identity fields must be non-empty")
        if self.exchange_timestamp_ms < 0 or self.received_at_ms <= 0:
            raise ValueError("event timestamps must be positive milliseconds")
        if self.exchange_time_kind not in {"EXCHANGE_EVENT", "RECEIVE_TIME_FALLBACK", "DERIVED_INTERVAL_CLOSE"}:
            raise ValueError("unsupported event timestamp provenance")

    @property
    def lineage_id(self) -> str:
        if self.event_id:
            raw = f"{self.provider}|{self.instrument_id}|{self.exchange_time_kind}|{self.event_id}"
        else:
            raw = (
                f"{self.provider}|{self.instrument_id}|{self.source}|"
                f"{self.exchange_time_kind}|{self.exchange_timestamp_ms}|{self.sequence if self.sequence is not None else ''}"
            )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class NormalizedTrade:
    metadata: EventMetadata
    side: Side
    price: float
    size: float
    trade_id: str

    def __post_init__(self) -> None:
        _positive(self.price, "trade price")
        _positive(self.size, "trade size", allow_zero=True)
        if not self.trade_id:
            raise ValueError("trade_id must be non-empty")


@dataclass(frozen=True, slots=True)
class NormalizedBBO:
    metadata: EventMetadata
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    quote_volume_24h: float | None = None

    def __post_init__(self) -> None:
        _positive(self.bid, "bid")
        _positive(self.ask, "ask")
        _positive(self.bid_size, "bid_size", allow_zero=True)
        _positive(self.ask_size, "ask_size", allow_zero=True)
        if self.bid >= self.ask:
            raise ValueError("BBO must not be crossed")
        if self.quote_volume_24h is not None:
            _positive(self.quote_volume_24h, "quote_volume_24h", allow_zero=True)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000.0


@dataclass(frozen=True, slots=True)
class NormalizedBookLevel:
    price: float
    size: float
    order_count: int | None = None

    def __post_init__(self) -> None:
        _positive(self.price, "book price")
        _positive(self.size, "book size", allow_zero=True)
        if self.order_count is not None and self.order_count < 0:
            raise ValueError("order_count must be non-negative")


@dataclass(frozen=True, slots=True)
class NormalizedL2:
    metadata: EventMetadata
    bids: tuple[NormalizedBookLevel, ...]
    asks: tuple[NormalizedBookLevel, ...]
    is_snapshot: bool = True

    def __post_init__(self) -> None:
        if any(left.price < right.price for left, right in zip(self.bids, self.bids[1:])):
            raise ValueError("L2 bids must be descending")
        if any(left.price > right.price for left, right in zip(self.asks, self.asks[1:])):
            raise ValueError("L2 asks must be ascending")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError("L2 must not be crossed")


@dataclass(frozen=True, slots=True)
class NormalizedCandle:
    metadata: EventMetadata
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trade_count: int | None = None
    closed: bool = True

    def __post_init__(self) -> None:
        if not self.interval or self.open_time_ms < 0 or self.close_time_ms < self.open_time_ms:
            raise ValueError("invalid candle interval/timestamps")
        for name in ("open", "high", "low", "close"):
            _positive(float(getattr(self, name)), name)
        _positive(self.volume, "volume", allow_zero=True)
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("invalid OHLC relationship")
        if self.trade_count is not None and self.trade_count < 0:
            raise ValueError("trade_count must be non-negative")


@dataclass(frozen=True, slots=True)
class FundingSnapshot:
    metadata: EventMetadata
    rate: float
    next_funding_time_ms: int | None = None

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.rate)):
            raise ValueError("funding rate must be finite")


@dataclass(frozen=True, slots=True)
class OpenInterestSnapshot:
    metadata: EventMetadata
    open_interest_base: float
    open_interest_quote: float | None = None

    def __post_init__(self) -> None:
        _positive(self.open_interest_base, "open_interest_base", allow_zero=True)
        if self.open_interest_quote is not None:
            _positive(self.open_interest_quote, "open_interest_quote", allow_zero=True)


@dataclass(frozen=True, slots=True)
class Liquidation:
    metadata: EventMetadata
    side: Side
    price: float
    size: float
    notional: float
    liquidation_id: str

    def __post_init__(self) -> None:
        _positive(self.price, "liquidation price")
        _positive(self.size, "liquidation size", allow_zero=True)
        _positive(self.notional, "liquidation notional", allow_zero=True)
        if not self.liquidation_id:
            raise ValueError("liquidation_id must be non-empty")


@dataclass(frozen=True, slots=True)
class MarkIndexSnapshot:
    metadata: EventMetadata
    mark_price: float
    index_price: float

    def __post_init__(self) -> None:
        _positive(self.mark_price, "mark_price")
        _positive(self.index_price, "index_price")


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    provider: str
    status: ProviderStatus
    checked_at_ms: int
    last_received_at_ms: int | None = None
    lag_ms: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None

    def __post_init__(self) -> None:
        if not self.provider or self.checked_at_ms <= 0:
            raise ValueError("invalid provider health identity/timestamp")
        if self.lag_ms is not None and self.lag_ms < 0:
            raise ValueError("provider lag must be non-negative")
        if self.consecutive_failures < 0:
            raise ValueError("consecutive_failures must be non-negative")


@dataclass(frozen=True, slots=True)
class VenueContribution:
    provider: str
    venue: str
    instrument_id: str
    market_type: MarketType
    price: float
    weight: float
    receive_age_ms: int
    latency_ms: int | None
    spread_bps: float | None
    quote_volume_24h: float | None


@dataclass(frozen=True, slots=True)
class CanonicalMarketTruth:
    canonical_asset_id: str
    calculated_at_ms: int
    status: ProviderStatus
    canonical_price: float | None
    quorum: int
    required_quorum: int
    venue_divergence_bps: float | None
    cross_exchange_basis_bps: float | None
    weighted_spread_bps: float | None
    volume_concentration: float | None
    rejected_outliers: tuple[str, ...]
    stale_sources: tuple[str, ...]
    contributions: tuple[VenueContribution, ...]
    reason: str | None = None
    canonical_quote: str | None = None
    uncomparable_sources: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


NormalizedEvent = (
    NormalizedTrade
    | NormalizedBBO
    | NormalizedL2
    | NormalizedCandle
    | FundingSnapshot
    | OpenInterestSnapshot
    | Liquidation
    | MarkIndexSnapshot
)
