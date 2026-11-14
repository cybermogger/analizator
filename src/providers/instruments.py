from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable

from .types import Instrument, MarketType


class InstrumentRegistry:
    """Exact identity registry; ticker text is never a join key."""

    def __init__(self, instruments: Iterable[Instrument] = ()):
        self._by_id: dict[str, Instrument] = {}
        self._by_venue_identity: dict[tuple[str, str, MarketType, str, str, str], Instrument] = {}
        self._by_onchain_identity: dict[tuple[str, str], Instrument] = {}
        self._by_canonical: dict[str, list[Instrument]] = defaultdict(list)
        for instrument in instruments:
            self.register(instrument)

    @classmethod
    def from_config(cls, entries: Iterable[dict[str, Any]]) -> "InstrumentRegistry":
        return cls(
            Instrument(
                instrument_id=str(item["instrument_id"]),
                canonical_asset_id=str(item["canonical_asset_id"]),
                provider=str(item["provider"]),
                venue=str(item["venue"]),
                market_type=MarketType(str(item["market_type"])),
                base=str(item["base"]),
                quote=str(item["quote"]),
                venue_symbol=str(item["venue_symbol"]),
                price_decimals=int(item["price_decimals"]),
                size_decimals=int(item["size_decimals"]),
                chain=str(item["chain"]) if item.get("chain") else None,
                contract_address=str(item["contract_address"]) if item.get("contract_address") else None,
                active=bool(item.get("active", True)),
                metadata=dict(item.get("metadata", {})),
            )
            for item in entries
        )

    def register(self, instrument: Instrument) -> None:
        if instrument.instrument_id in self._by_id:
            raise ValueError(f"duplicate instrument_id: {instrument.instrument_id}")
        if instrument.venue_identity in self._by_venue_identity:
            raise ValueError(f"duplicate venue instrument: {instrument.venue_identity}")
        if instrument.onchain_identity and instrument.onchain_identity in self._by_onchain_identity:
            raise ValueError(f"duplicate on-chain instrument: {instrument.onchain_identity}")
        self._by_id[instrument.instrument_id] = instrument
        self._by_venue_identity[instrument.venue_identity] = instrument
        if instrument.onchain_identity:
            self._by_onchain_identity[instrument.onchain_identity] = instrument
        self._by_canonical[instrument.canonical_asset_id].append(instrument)

    def upsert(self, instrument: Instrument) -> None:
        """Replace an exact instrument identity without leaving stale canonical indexes."""
        current = self._by_id.get(instrument.instrument_id)
        if current is not None:
            if current.venue_identity != instrument.venue_identity:
                raise ValueError(f"instrument id changed venue identity: {instrument.instrument_id}")
            self._by_venue_identity.pop(current.venue_identity, None)
            if current.onchain_identity:
                self._by_onchain_identity.pop(current.onchain_identity, None)
            self._by_canonical[current.canonical_asset_id] = [
                item for item in self._by_canonical[current.canonical_asset_id]
                if item.instrument_id != current.instrument_id
            ]
        identity_owner = self._by_venue_identity.get(instrument.venue_identity)
        if identity_owner is not None and identity_owner.instrument_id != instrument.instrument_id:
            raise ValueError(f"duplicate venue instrument: {instrument.venue_identity}")
        self._by_id[instrument.instrument_id] = instrument
        self._by_venue_identity[instrument.venue_identity] = instrument
        if instrument.onchain_identity:
            self._by_onchain_identity[instrument.onchain_identity] = instrument
        self._by_canonical[instrument.canonical_asset_id].append(instrument)

    def reconcile_catalog_scope(self, provider: str, scope: str, instruments: Iterable[Instrument]) -> tuple[Instrument, ...]:
        """Atomically apply a complete scope and mark absent old markets inactive."""
        incoming = tuple(instruments)
        incoming_by_id = {item.instrument_id: item for item in incoming}
        if len(incoming_by_id) != len(incoming):
            raise ValueError("catalog scope contains duplicate instrument ids")
        incoming_identities = [item.venue_identity for item in incoming]
        if len(set(incoming_identities)) != len(incoming_identities):
            raise ValueError("catalog scope contains duplicate venue identities")
        for item in incoming:
            existing = self._by_id.get(item.instrument_id)
            if existing is not None and existing.venue_identity != item.venue_identity:
                raise ValueError(f"catalog changed venue identity for {item.instrument_id}")
            owner = self._by_venue_identity.get(item.venue_identity)
            if owner is not None and owner.instrument_id != item.instrument_id and not (
                owner.provider == provider and owner.metadata.get("catalog_scope") == scope
                and owner.instrument_id not in incoming_by_id
            ):
                raise ValueError(f"catalog venue identity collides with {owner.instrument_id}")
        old_scope = [
            item for item in self._by_id.values()
            if item.provider == provider and item.metadata.get("catalog_scope") == scope
        ]
        for item in old_scope:
            if item.instrument_id not in incoming_by_id and item.active:
                incoming_by_id[item.instrument_id] = replace(item, active=False)
        for item in incoming_by_id.values():
            self.upsert(item)
        return tuple(incoming_by_id.values())

    def get(self, instrument_id: str) -> Instrument:
        try:
            return self._by_id[instrument_id]
        except KeyError as exc:
            raise KeyError(f"unknown instrument_id: {instrument_id}") from exc

    def resolve_venue(
        self, provider: str, venue: str, market_type: MarketType, base: str, quote: str, venue_symbol: str
    ) -> Instrument:
        key = provider, venue, market_type, base, quote, venue_symbol
        try:
            return self._by_venue_identity[key]
        except KeyError as exc:
            raise KeyError(f"unknown venue identity: {key}") from exc

    def resolve_onchain(self, chain: str, contract_address: str) -> Instrument:
        key = chain, contract_address.lower()
        try:
            return self._by_onchain_identity[key]
        except KeyError as exc:
            raise KeyError(f"unknown on-chain identity: {key}") from exc

    def for_provider(self, provider: str, *, active_only: bool = True) -> tuple[Instrument, ...]:
        return tuple(
            item for item in self._by_id.values()
            if item.provider == provider and (item.active or not active_only)
        )

    def for_canonical_asset(self, canonical_asset_id: str, *, active_only: bool = True) -> tuple[Instrument, ...]:
        return tuple(
            item for item in self._by_canonical.get(canonical_asset_id, ())
            if item.active or not active_only
        )

    def canonical_assets(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_canonical))

    def all(self, *, active_only: bool = True) -> tuple[Instrument, ...]:
        return tuple(item for item in self._by_id.values() if item.active or not active_only)

    def __len__(self) -> int:
        return len(self._by_id)
