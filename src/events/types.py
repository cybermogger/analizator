from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class SourceTier(StrEnum):
    REGULATOR = "regulator"
    MACRO_CALENDAR = "macro_calendar"
    OFFICIAL_EXCHANGE = "official_exchange"
    OFFICIAL_PROJECT = "official_project"
    OFFICIAL_FOUNDER = "official_founder"
    PRIMARY_CODE = "primary_code"
    SECURITY_ADVISORY = "security_advisory"
    SECONDARY_NEWS = "secondary_news"
    UNOFFICIAL_SOCIAL = "unofficial_social"
    MARKET_CONFIRMATION = "market_confirmation"


class EventType(StrEnum):
    OFFICIAL_TOKEN_LAUNCH = "OFFICIAL_TOKEN_LAUNCH"
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    HACK_EXPLOIT = "HACK/EXPLOIT"
    REGULATORY = "REGULATORY"
    ETF = "ETF"
    PARTNERSHIP = "PARTNERSHIP"
    TOKEN_UNLOCK = "TOKEN_UNLOCK"
    BURN = "BURN"
    GOVERNANCE = "GOVERNANCE"
    OUTAGE = "OUTAGE"
    WHALE_TRANSFER = "WHALE_TRANSFER"
    TREASURY_FOUNDATION_ACTION = "TREASURY/FOUNDATION_ACTION"
    MACRO = "MACRO"
    RUMOR = "RUMOR"

    @classmethod
    def _missing_(cls, value: object) -> "EventType | None":
        aliases = {
            "HACK_EXPLOIT": cls.HACK_EXPLOIT,
            "TREASURY_ACTION": cls.TREASURY_FOUNDATION_ACTION,
        }
        return aliases.get(str(value))


class AuthenticityState(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    COMPROMISE_SUSPECTED = "compromise_suspected"
    DELETED = "deleted"
    RETRACTED = "retracted"


class ContradictionState(StrEnum):
    NONE = "none"
    DISPUTED = "disputed"
    CONTRADICTED = "contradicted"
    RETRACTED = "retracted"


class RelationType(StrEnum):
    ORIGINAL = "original"
    REPLY = "reply"
    QUOTE = "quote"
    REPOST = "repost"


class ExpectedDirection(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ClaimType(StrEnum):
    CONFIRMED_ONCHAIN_TRADE = "confirmed_onchain_trade"
    ATTRIBUTED_WALLET_CLUSTER = "attributed_wallet_cluster"
    UNVERIFIED_SOCIAL_CLAIM = "unverified_social_claim"
    REALIZED_NET_RESULT = "realized_net_result"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawEvent:
    raw_event_id: str
    source_id: str
    source_tier: SourceTier
    author_id: str | None
    url: str
    published_at_ms: int
    received_at_ms: int
    content_hash: str
    language: str | None
    payload: Mapping[str, Any]
    text: str
    relation_type: RelationType = RelationType.ORIGINAL
    related_event_id: str | None = None
    thread_id: str | None = None
    authenticity: AuthenticityState = AuthenticityState.UNVERIFIED
    explicit_project_id: str | None = None
    contract_addresses: tuple[tuple[str, str], ...] = ()
    deleted: bool = False

    def __post_init__(self) -> None:
        if not self.raw_event_id or not self.source_id or not self.url:
            raise ValueError("raw event identity, source and URL are required")
        if self.published_at_ms <= 0 or self.received_at_ms <= 0:
            raise ValueError("event timestamps must be positive UTC epoch milliseconds")
        if self.received_at_ms < self.published_at_ms:
            raise ValueError("received_at cannot precede published_at")
        if len(self.content_hash) != 64:
            raise ValueError("content_hash must be a sha256 hex digest")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def build(
        cls,
        *,
        raw_event_id: str,
        source_id: str,
        source_tier: SourceTier,
        author_id: str | None,
        url: str,
        published_at_ms: int,
        received_at_ms: int,
        payload: Mapping[str, Any],
        text: str,
        language: str | None = None,
        relation_type: RelationType = RelationType.ORIGINAL,
        related_event_id: str | None = None,
        thread_id: str | None = None,
        authenticity: AuthenticityState = AuthenticityState.UNVERIFIED,
        explicit_project_id: str | None = None,
        contract_addresses: tuple[tuple[str, str], ...] = (),
        deleted: bool = False,
    ) -> "RawEvent":
        return cls(
            raw_event_id=raw_event_id,
            source_id=source_id,
            source_tier=source_tier,
            author_id=author_id,
            url=url,
            published_at_ms=published_at_ms,
            received_at_ms=received_at_ms,
            content_hash=payload_hash(payload),
            language=language,
            payload=payload,
            text=text,
            relation_type=relation_type,
            related_event_id=related_event_id,
            thread_id=thread_id,
            authenticity=authenticity,
            explicit_project_id=explicit_project_id,
            contract_addresses=contract_addresses,
            deleted=deleted,
        )

    @property
    def ingestion_latency_ms(self) -> int:
        return self.received_at_ms - self.published_at_ms


@dataclass(frozen=True, slots=True)
class ImpactedAsset:
    project_id: str
    canonical_asset_id: str
    chain_id: str | None
    contract_address: str | None
    expected_direction: ExpectedDirection
    likely_horizons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalizedEvent:
    event_id: str
    event_type: EventType
    summary: str
    novelty: float
    credibility: float
    confirmation_count: int
    impacted_assets: tuple[ImpactedAsset, ...]
    expected_direction: ExpectedDirection
    likely_horizons: tuple[str, ...]
    decay_half_life_minutes: float
    contradiction_state: ContradictionState
    confidence: float
    first_published_at_ms: int
    first_received_at_ms: int
    latest_received_at_ms: int
    raw_event_ids: tuple[str, ...]
    authenticity_risk: bool
    high_impact: bool
    source_ids: tuple[str, ...]
    evidence_urls: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("novelty", "credibility", "confidence"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0,1]")
        if self.confirmation_count < 1 or not self.raw_event_ids:
            raise ValueError("normalized events require evidence")
        if self.first_received_at_ms < self.first_published_at_ms:
            raise ValueError("invalid normalized event chronology")

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "summary": self.summary,
            "novelty": self.novelty,
            "credibility": self.credibility,
            "confirmation_count": self.confirmation_count,
            "impacted_assets": [
                {
                    "project_id": item.project_id,
                    "canonical_asset_id": item.canonical_asset_id,
                    "chain_id": item.chain_id,
                    "contract_address": item.contract_address,
                    "expected_direction": item.expected_direction.value,
                    "likely_horizons": list(item.likely_horizons),
                }
                for item in self.impacted_assets
            ],
            "expected_direction": self.expected_direction.value,
            "likely_horizons": list(self.likely_horizons),
            "decay_half_life_minutes": self.decay_half_life_minutes,
            "contradiction_state": self.contradiction_state.value,
            "confidence": self.confidence,
            "first_published_at_ms": self.first_published_at_ms,
            "first_received_at_ms": self.first_received_at_ms,
            "latest_received_at_ms": self.latest_received_at_ms,
            "raw_event_ids": list(self.raw_event_ids),
            "authenticity_risk": self.authenticity_risk,
            "high_impact": self.high_impact,
            "source_ids": list(self.source_ids),
            "evidence_urls": list(self.evidence_urls),
            "trade_action": None,
        }


@dataclass(frozen=True, slots=True)
class EventClaim:
    claim_id: str
    event_id: str
    claim_type: ClaimType
    evidence_url: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = dict(self.payload)
        if self.claim_type is ClaimType.REALIZED_NET_RESULT:
            required = {"proceeds", "cost_basis", "fees", "slippage"}
            if not required.issubset(values):
                raise ValueError("realized result requires proceeds, cost_basis, fees and slippage")
            values["net_realized"] = (
                float(values["proceeds"]) - float(values["cost_basis"])
                - float(values["fees"]) - float(values["slippage"])
            )
            values.pop("peak_mark_to_market", None)
        object.__setattr__(self, "payload", MappingProxyType(values))
