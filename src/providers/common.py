from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import time
from typing import Any, Mapping, Sequence

from .types import EventMetadata, Instrument, MarketType


def now_ms() -> int:
    return int(time.time() * 1000)


def as_float(value: Any, field: str, *, allow_zero: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field {field}: {value!r}") from exc
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError(f"invalid numeric field {field}: {value!r}")
    return number


def as_signed_float(value: Any, field: str) -> float:
    """Parse a finite signed metric without weakening price/quantity validation."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric field {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"invalid numeric field {field}: {value!r}")
    return number


def as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer field {field}: {value!r}") from exc


def require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def require_sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    return value


def iso_to_ms(value: str) -> int:
    normalized = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def metadata(
    instrument: Instrument,
    *,
    exchange_timestamp_ms: int,
    received_at_ms: int,
    source: str,
    sequence: int | None = None,
    event_id: str | None = None,
    exchange_time_kind: str = "EXCHANGE_EVENT",
) -> EventMetadata:
    return EventMetadata(
        provider=instrument.provider,
        venue=instrument.venue,
        instrument_id=instrument.instrument_id,
        exchange_timestamp_ms=int(exchange_timestamp_ms),
        received_at_ms=int(received_at_ms),
        source=source,
        sequence=sequence,
        event_id=event_id,
        exchange_time_kind=exchange_time_kind,
    )


def decimal_places(value: Any) -> int:
    try:
        exponent = Decimal(str(value)).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        raise ValueError("invalid increment for instrument precision")
    return max(0, min(18, -int(exponent)))


def discovered_instrument(
    registry: Any,
    *, provider: str, venue: str, market_type: MarketType, base: str, quote: str,
    venue_symbol: str, price_decimals: int, size_decimals: int, scope: str,
    source: str, received_at_ms: int, active: bool = True,
) -> Instrument:
    """Build exact venue identity; only preserve a canonical mapping already configured."""
    base, quote, venue_symbol = base.upper(), quote.upper(), str(venue_symbol)
    existing = None
    identity = (provider, venue, market_type, base, quote, venue_symbol)
    for item in registry.all(active_only=False):
        if item.venue_identity == identity:
            existing = item
            break
    if existing is not None:
        canonical_id = existing.canonical_asset_id
        instrument_id = existing.instrument_id
    else:
        canonical_id = f"unresolved:{provider}:{venue}:{market_type.value}:{base}:{quote}:{venue_symbol}"
        instrument_id = f"discovered:{provider}:{venue}:{market_type.value}:{venue_symbol}"
    return Instrument(
        instrument_id=instrument_id, canonical_asset_id=canonical_id, provider=provider,
        venue=venue, market_type=market_type, base=base, quote=quote,
        venue_symbol=venue_symbol, price_decimals=price_decimals, size_decimals=size_decimals,
        active=active, metadata={
            "catalog_scope": scope, "catalog_source": source,
            "catalog_received_at_ms": received_at_ms,
            "canonical_mapping": "configured_exact_identity" if existing else "unresolved_venue_identity",
        },
    )
