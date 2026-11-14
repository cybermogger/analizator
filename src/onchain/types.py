from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping


class ChainId(StrEnum):
    SOLANA = "solana"
    BASE = "eip155:8453"


class OnChainEventType(StrEnum):
    TOKEN_CREATED = "TOKEN_CREATED"
    POOL_CREATED = "POOL_CREATED"
    LIQUIDITY_ADDED = "LIQUIDITY_ADDED"
    LIQUIDITY_REMOVED = "LIQUIDITY_REMOVED"
    LIQUIDITY_SNAPSHOT = "LIQUIDITY_SNAPSHOT"
    TRADE = "TRADE"
    HOLDER_SNAPSHOT = "HOLDER_SNAPSHOT"
    AUTHORITY_STATE = "AUTHORITY_STATE"
    CONTRACT_RISK = "CONTRACT_RISK"
    TRANSFER = "TRANSFER"
    WALLET_LINK = "WALLET_LINK"
    OFFICIAL_IDENTITY = "OFFICIAL_IDENTITY"
    MARKET_SNAPSHOT = "MARKET_SNAPSHOT"
    PAPER_EXIT_QUOTE = "PAPER_EXIT_QUOTE"
    CAPABILITY_STATE = "CAPABILITY_STATE"


class CandidateStatus(StrEnum):
    HIGH_RECALL = "HIGH_RECALL"
    OBSERVE = "OBSERVE"
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    REORGED = "REORGED"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def normalize_contract(chain_id: str, contract_address: str) -> str:
    value = contract_address.strip()
    if chain_id.startswith("eip155:"):
        value = value.lower()
        if (
            not value.startswith("0x") or len(value) != 42
            or any(character not in "0123456789abcdef" for character in value[2:])
        ):
            raise ValueError("EVM contract must be a 20-byte 0x address")
    elif chain_id == ChainId.SOLANA:
        base58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        if not 32 <= len(value) <= 44 or any(character not in base58 for character in value):
            raise ValueError("Solana mint must be a base58 address")
    if not value:
        raise ValueError("contract/mint is required")
    return value


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    chain_id: str
    contract_address: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "contract_address", normalize_contract(self.chain_id, self.contract_address))

    @property
    def key(self) -> str:
        return f"{self.chain_id}:{self.contract_address}"


@dataclass(frozen=True, slots=True)
class OnChainEvent:
    event_id: str
    provider_id: str
    identity: TokenIdentity
    event_type: OnChainEventType
    block_number: int
    block_hash: str
    parent_hash: str | None
    transaction_id: str
    transaction_index: int
    log_index: int
    exchange_timestamp_ms: int
    received_at_ms: int
    payload: Mapping[str, Any]
    payload_hash: str
    chain_verified: bool
    confirmations: int = 0

    def __post_init__(self) -> None:
        if not self.event_id or not self.provider_id or not self.transaction_id:
            raise ValueError("event/provider/transaction identity is required")
        if self.block_number < 0 or self.transaction_index < 0 or self.log_index < 0:
            raise ValueError("chain positions must be non-negative")
        if self.exchange_timestamp_ms <= 0 or self.received_at_ms <= 0:
            raise ValueError("timestamps must be UTC epoch milliseconds")
        if len(self.payload_hash) != 64:
            raise ValueError("payload_hash must be sha256")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def build(
        cls,
        *,
        event_id: str,
        provider_id: str,
        chain_id: str,
        contract_address: str,
        event_type: OnChainEventType,
        block_number: int,
        block_hash: str,
        transaction_id: str,
        transaction_index: int,
        log_index: int,
        exchange_timestamp_ms: int,
        received_at_ms: int,
        payload: Mapping[str, Any],
        parent_hash: str | None = None,
        chain_verified: bool = True,
        confirmations: int = 0,
    ) -> "OnChainEvent":
        encoded = canonical_json(payload)
        return cls(
            event_id=event_id,
            provider_id=provider_id,
            identity=TokenIdentity(chain_id, contract_address),
            event_type=event_type,
            block_number=block_number,
            block_hash=block_hash,
            parent_hash=parent_hash,
            transaction_id=transaction_id,
            transaction_index=transaction_index,
            log_index=log_index,
            exchange_timestamp_ms=exchange_timestamp_ms,
            received_at_ms=received_at_ms,
            payload=payload,
            payload_hash=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            chain_verified=chain_verified,
            confirmations=confirmations,
        )

    @property
    def position_key(self) -> str:
        return f"{self.identity.key}:{self.block_number}:{self.transaction_index}:{self.log_index}"


@dataclass(frozen=True, slots=True)
class PaperExitQuote:
    notional_usd: float
    expected_output_usd: float
    slippage_bps: float
    price_impact_bps: float
    fees_usd: float
    provider: str
    quoted_at_ms: int

    def __post_init__(self) -> None:
        if self.notional_usd <= 0 or self.expected_output_usd <= 0:
            raise ValueError("paper quote notionals and output must be positive")
        if self.slippage_bps < 0 or self.price_impact_bps < 0 or self.fees_usd < 0:
            raise ValueError("paper quote costs cannot be negative")
        if self.fees_usd > self.expected_output_usd or not self.provider or self.quoted_at_ms <= 0:
            raise ValueError("paper quote fees/provider/timestamp are invalid")

    @property
    def net_output_usd(self) -> float:
        return max(0.0, self.expected_output_usd - self.fees_usd)

    @property
    def effective_slippage_bps(self) -> float:
        realized = max(0.0, (1.0 - self.net_output_usd / self.notional_usd) * 10_000)
        return max(self.slippage_bps, realized)


@dataclass(frozen=True, slots=True)
class CandidateScores:
    attention: float
    authenticity: float
    liquidity: float
    distribution: float
    onchain_momentum: float
    manipulation_risk: float
    exit_feasibility: float

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = float(getattr(self, name))
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be in [0,100]")


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    identity: TokenIdentity
    symbol: str | None
    name: str | None
    status: CandidateStatus
    first_seen_at_ms: int
    as_of_ms: int
    pool_id: str | None
    pool_provider: str | None
    liquidity_usd: float
    proven_sellable_capacity_usd: float
    removed_liquidity_usd: float
    market_cap_usd: float | None
    paper_exit_quote: PaperExitQuote | None
    top_holder_percent: float | None
    top10_holder_percent: float | None
    mint_authority: str | None
    freeze_authority: str | None
    update_authority: str | None
    owner: str | None
    proxy_implementation: str | None
    official_identity_confirmed: bool
    official_confirmation_received_at_ms: int | None
    scores: CandidateScores
    risk_flags: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    ttl_minutes: int
    invalidation: tuple[str, ...]
    detection_latency_ms: int | None
    mev_disadvantage_bps: float
    feasible_entry_price: float | None
    minimum_launch_price_known: bool = False
    auto_purchase: bool = False
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of_ms < self.first_seen_at_ms:
            raise ValueError("assessment cannot precede first observation")
        if self.minimum_launch_price_known:
            raise ValueError("private/minimum launch price claims are forbidden")
        if self.auto_purchase:
            raise ValueError("automatic purchase is out of scope")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

    @property
    def token_age_seconds(self) -> float:
        return max(0.0, (self.as_of_ms - self.first_seen_at_ms) / 1000)

    def as_dict(self) -> dict[str, Any]:
        quote = None
        if self.paper_exit_quote is not None:
            quote = {
                "notional_usd": self.paper_exit_quote.notional_usd,
                "expected_output_usd": self.paper_exit_quote.expected_output_usd,
                "net_output_usd": self.paper_exit_quote.net_output_usd,
                "slippage_bps": self.paper_exit_quote.slippage_bps,
                "effective_slippage_bps": self.paper_exit_quote.effective_slippage_bps,
                "price_impact_bps": self.paper_exit_quote.price_impact_bps,
                "fees_usd": self.paper_exit_quote.fees_usd,
                "provider": self.paper_exit_quote.provider,
                "quoted_at_ms": self.paper_exit_quote.quoted_at_ms,
            }
        return {
            "chain_id": self.identity.chain_id,
            "contract_address": self.identity.contract_address,
            "symbol": self.symbol,
            "name": self.name,
            "status": self.status.value,
            "first_seen_at_ms": self.first_seen_at_ms,
            "as_of_ms": self.as_of_ms,
            "token_age_seconds": self.token_age_seconds,
            "pool_id": self.pool_id,
            "pool_provider": self.pool_provider,
            "liquidity_usd": self.liquidity_usd,
            "proven_sellable_capacity_usd": self.proven_sellable_capacity_usd,
            "removed_liquidity_usd": self.removed_liquidity_usd,
            "market_cap_usd": self.market_cap_usd,
            "paper_exit_quote": quote,
            "top_holder_percent": self.top_holder_percent,
            "top10_holder_percent": self.top10_holder_percent,
            "authorities": {
                "mint": self.mint_authority,
                "freeze": self.freeze_authority,
                "update": self.update_authority,
                "owner": self.owner,
                "proxy_implementation": self.proxy_implementation,
            },
            "official_identity_confirmed": self.official_identity_confirmed,
            "official_confirmation_received_at_ms": self.official_confirmation_received_at_ms,
            "scores": {name: getattr(self.scores, name) for name in self.scores.__dataclass_fields__},
            "risk_flags": list(self.risk_flags),
            "evidence_event_ids": list(self.evidence_event_ids),
            "ttl_minutes": self.ttl_minutes,
            "invalidation": list(self.invalidation),
            "detection_latency_ms": self.detection_latency_ms,
            "mev_disadvantage_bps": self.mev_disadvantage_bps,
            "feasible_entry_price": self.feasible_entry_price,
            "minimum_launch_price_known": False,
            "auto_purchase": False,
            "metrics": dict(self.metrics),
        }
