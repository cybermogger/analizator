from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable, Protocol

from .types import (
    CandidateAssessment,
    CandidateScores,
    CandidateStatus,
    OnChainEvent,
    OnChainEventType,
    PaperExitQuote,
    TokenIdentity,
)


class OnChainRepository(Protocol):
    def insert_event(self, event: OnChainEvent) -> str: ...
    def canonical_events(self, identity: TokenIdentity, as_of_ms: int) -> tuple[OnChainEvent, ...]: ...
    def save_assessment(self, assessment: CandidateAssessment) -> None: ...


@dataclass(slots=True)
class _State:
    identity: TokenIdentity
    first_seen_at_ms: int
    candidate_origin: bool = False
    verified_pool_evidence: bool = False
    verified_liquidity_evidence: bool = False
    verified_authority_evidence: bool = False
    verified_holder_evidence: bool = False
    verified_contract_risk_evidence: bool = False
    symbol: str | None = None
    name: str | None = None
    deployer: str | None = None
    funding_path: list[str] = field(default_factory=list)
    linked_wallets: set[str] = field(default_factory=set)
    pool_id: str | None = None
    pool_provider: str | None = None
    liquidity_usd: float = 0.0
    proven_sellable_capacity_usd: float = 0.0
    removed_liquidity_usd: float = 0.0
    market_cap_usd: float | None = None
    mint_authority: str | None = "unknown"
    freeze_authority: str | None = "unknown"
    update_authority: str | None = "unknown"
    owner: str | None = None
    proxy_implementation: str | None = None
    top_holder_percent: float | None = None
    top10_holder_percent: float | None = None
    top_token_account_percent: float | None = None
    top10_token_account_percent: float | None = None
    holder_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    repeat_traders: set[str] = field(default_factory=set)
    wallet_trades: dict[str, int] = field(default_factory=dict)
    realized_volume_usd: float = 0.0
    wash_volume_usd: float = 0.0
    self_trade_volume_usd: float = 0.0
    bundled_wallet_percent: float = 0.0
    sniper_wallet_percent: float = 0.0
    synchronized_buy_percent: float = 0.0
    organic_wallet_growth: float = 0.0
    smart_wallet_net_usd: float = 0.0
    exchange_net_usd: float = 0.0
    treasury_net_usd: float = 0.0
    honeypot: bool = False
    sell_tax_percent: float = 0.0
    blacklist: bool = False
    transfer_restricted: bool = False
    contract_risk_checked: bool = False
    upgradeable_proxy: bool = False
    official_identity_confirmed: bool = False
    official_confirmation_received_at_ms: int | None = None
    detection_latency_ms: int | None = None
    quote: PaperExitQuote | None = None
    quotes: dict[float, PaperExitQuote] = field(default_factory=dict)
    quote_metadata: dict[float, dict[str, float | None]] = field(default_factory=dict)
    entry_price: float | None = None
    latency_disadvantage_bps: float = 0.0
    mev_disadvantage_bps: float = 0.0
    chain_verified_events: int = 0
    evidence: list[str] = field(default_factory=list)
    unavailable_capabilities: set[str] = field(default_factory=set)
    required_unavailable_capabilities: set[str] = field(default_factory=set)


DEFAULT_RISK_CONFIG: dict[str, Any] = {
    "paper_notional_usd": 1000.0,
    "minimum_liquidity_usd": 25_000.0,
    "minimum_sellable_liquidity_multiple": 3.0,
    "maximum_slippage_bps": 500.0,
    "maximum_top10_holder_percent": 70.0,
    "maximum_sell_tax_percent": 10.0,
    "maximum_manipulation_risk": 55.0,
    "require_official_identity": True,
    "require_revoked_mint_authority": True,
    "require_revoked_freeze_authority": True,
    "ttl_minutes": 30,
}


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


class OnChainCandidatePipeline:
    """High-recall discovery followed by a separate strict, executable-exit filter."""

    def __init__(self, config: dict[str, Any] | None = None, repository: OnChainRepository | None = None):
        self.config = {**DEFAULT_RISK_CONFIG, **(config or {})}
        self.repository = repository
        self._events_by_id: dict[str, OnChainEvent] = {}
        self._position_event: dict[str, str] = {}
        self._identities: dict[str, TokenIdentity] = {}
        self._latest: dict[str, CandidateAssessment] = {}

    def ingest(self, event: OnChainEvent) -> CandidateAssessment | None:
        existing = self._events_by_id.get(event.event_id)
        if existing is not None:
            if existing.payload_hash != event.payload_hash or existing.block_hash != event.block_hash:
                raise ValueError(f"event_id collision with changed evidence: {event.event_id}")
            return self._latest.get(event.identity.key)

        if self.repository is not None:
            result = self.repository.insert_event(event)
            if result == "duplicate":
                return self._latest.get(event.identity.key)
        self._events_by_id[event.event_id] = event
        previous_id = self._position_event.get(event.position_key)
        if previous_id and self._events_by_id[previous_id].block_hash != event.block_hash:
            self._position_event[event.position_key] = event.event_id
        elif previous_id is None:
            self._position_event[event.position_key] = event.event_id
        self._identities[event.identity.key] = event.identity
        assessment = self.assess(event.identity, event.received_at_ms)
        if assessment is not None:
            self._latest[event.identity.key] = assessment
            if self.repository is not None:
                self.repository.save_assessment(assessment)
        return assessment

    def _canonical_events(self, identity: TokenIdentity, as_of_ms: int) -> tuple[OnChainEvent, ...]:
        if self.repository is not None:
            return self.repository.canonical_events(identity, as_of_ms)
        visible = [
            event for event in self._events_by_id.values()
            if event.identity == identity and event.received_at_ms <= as_of_ms
        ]
        latest_by_position: dict[str, OnChainEvent] = {}
        for event in visible:
            previous = latest_by_position.get(event.position_key)
            if previous is None or (event.received_at_ms, event.event_id) > (
                previous.received_at_ms, previous.event_id
            ):
                latest_by_position[event.position_key] = event
        values = list(latest_by_position.values())
        return tuple(sorted(values, key=lambda item: (
            item.block_number, item.transaction_index, item.log_index,
            item.exchange_timestamp_ms, item.received_at_ms, item.event_id,
        )))

    def assess(self, identity: TokenIdentity, as_of_ms: int) -> CandidateAssessment | None:
        events = self._canonical_events(identity, as_of_ms)
        if not events:
            return None
        origin_times = [
            item.received_at_ms for item in events
            if item.chain_verified and item.event_type in {
                OnChainEventType.TOKEN_CREATED, OnChainEventType.POOL_CREATED,
            }
        ]
        if not origin_times:
            return None
        state = _State(identity, min(origin_times))
        for event in events:
            self._apply(state, event)
        if not state.candidate_origin:
            return None
        self._select_quote_evidence(state, as_of_ms)
        scores, flags, hard_reject = self._score(state, as_of_ms)
        if hard_reject:
            status = CandidateStatus.REJECTED
        elif self.config["require_official_identity"] and not state.official_identity_confirmed:
            status = CandidateStatus.OBSERVE
        elif state.quote is None:
            status = CandidateStatus.OBSERVE
        else:
            status = CandidateStatus.ELIGIBLE
        entry_price = None
        if state.entry_price is not None:
            entry_price = state.entry_price * (
                1.0 + (state.latency_disadvantage_bps + state.mev_disadvantage_bps) / 10_000
            )
        invalidation = (
            "fresh proven exit capacity below configured paper-notional support",
            "liquidity removal or authority/proxy risk",
            "official identity contradiction or contract mismatch",
            "slippage/manipulation above configured limits",
        )
        holder_available = state.verified_holder_evidence
        wash_available = "wash_trade_attribution" not in state.unavailable_capabilities
        bundle_available = "bundle_sniper_attribution" not in state.unavailable_capabilities
        contract_risk_available = state.contract_risk_checked
        metrics = {
            "deployer": state.deployer,
            "funding_path": tuple(state.funding_path),
            "linked_wallets": tuple(sorted(state.linked_wallets)),
            "holder_count": state.holder_count if holder_available else None,
            "top_token_account_percent": state.top_token_account_percent,
            "top10_token_account_percent": state.top10_token_account_percent,
            "buy_count": state.buy_count,
            "sell_count": state.sell_count,
            "unique_buyers": len(state.buyers),
            "unique_sellers": len(state.sellers),
            "repeat_traders": len(state.repeat_traders),
            "organic_wallet_growth": state.organic_wallet_growth if holder_available else None,
            "realized_volume_usd": state.realized_volume_usd,
            "wash_volume_usd": state.wash_volume_usd if wash_available else None,
            "self_trade_volume_usd": state.self_trade_volume_usd,
            "wash_volume_ratio": (
                state.wash_volume_usd / max(state.realized_volume_usd, 1.0)
                if wash_available else None
            ),
            "bundled_wallet_percent": state.bundled_wallet_percent if bundle_available else None,
            "sniper_wallet_percent": state.sniper_wallet_percent if bundle_available else None,
            "synchronized_buy_percent": state.synchronized_buy_percent if bundle_available else None,
            "smart_wallet_net_usd": state.smart_wallet_net_usd,
            "exchange_net_usd": state.exchange_net_usd,
            "treasury_net_usd": state.treasury_net_usd,
            "honeypot": state.honeypot if contract_risk_available else None,
            "sell_tax_percent": state.sell_tax_percent if contract_risk_available else None,
            "blacklist": state.blacklist if contract_risk_available else None,
            "transfer_restricted": state.transfer_restricted if contract_risk_available else None,
            "contract_risk_checked": state.contract_risk_checked,
            "upgradeable_proxy": state.upgradeable_proxy,
            "chain_verified_events": state.chain_verified_events,
            "verified_pool_evidence": state.verified_pool_evidence,
            "verified_liquidity_evidence": state.verified_liquidity_evidence,
            "verified_authority_evidence": state.verified_authority_evidence,
            "verified_holder_evidence": state.verified_holder_evidence,
            "verified_contract_risk_evidence": state.verified_contract_risk_evidence,
            "paper_exit_quotes": {
                str(notional): {
                    "expected_output_usd": quote.expected_output_usd,
                    "net_output_usd": quote.net_output_usd,
                    "slippage_bps": quote.slippage_bps,
                    "effective_slippage_bps": quote.effective_slippage_bps,
                    "price_impact_bps": quote.price_impact_bps,
                    "fees_usd": quote.fees_usd,
                    "provider": quote.provider,
                    "quoted_at_ms": quote.quoted_at_ms,
                }
                for notional, quote in sorted(state.quotes.items())
            },
            "paper_notional_usd": float(self.config["paper_notional_usd"]),
            "required_sellable_capacity_usd": self._required_capacity_usd(),
            "proven_sellable_capacity_usd": state.proven_sellable_capacity_usd,
            "unavailable_capabilities": tuple(sorted(state.unavailable_capabilities)),
            "required_unavailable_capabilities": tuple(sorted(state.required_unavailable_capabilities)),
        }
        return CandidateAssessment(
            identity=identity,
            symbol=state.symbol,
            name=state.name,
            status=status,
            first_seen_at_ms=state.first_seen_at_ms,
            as_of_ms=as_of_ms,
            pool_id=state.pool_id,
            pool_provider=state.pool_provider,
            liquidity_usd=max(0.0, state.liquidity_usd),
            proven_sellable_capacity_usd=max(0.0, state.proven_sellable_capacity_usd),
            removed_liquidity_usd=max(0.0, state.removed_liquidity_usd),
            market_cap_usd=state.market_cap_usd,
            paper_exit_quote=state.quote,
            top_holder_percent=state.top_holder_percent,
            top10_holder_percent=state.top10_holder_percent,
            mint_authority=state.mint_authority,
            freeze_authority=state.freeze_authority,
            update_authority=state.update_authority,
            owner=state.owner,
            proxy_implementation=state.proxy_implementation,
            official_identity_confirmed=state.official_identity_confirmed,
            official_confirmation_received_at_ms=state.official_confirmation_received_at_ms,
            scores=scores,
            risk_flags=tuple(sorted(set(flags))),
            evidence_event_ids=tuple(state.evidence),
            ttl_minutes=int(self.config["ttl_minutes"]),
            invalidation=invalidation,
            detection_latency_ms=state.detection_latency_ms,
            mev_disadvantage_bps=state.mev_disadvantage_bps,
            feasible_entry_price=entry_price,
            metrics=metrics,
        )

    def _apply(self, state: _State, event: OnChainEvent) -> None:
        payload = event.payload
        state.evidence.append(event.event_id)
        if event.chain_verified:
            state.chain_verified_events += 1
        if event.event_type in {OnChainEventType.TOKEN_CREATED, OnChainEventType.POOL_CREATED} and event.chain_verified:
            state.candidate_origin = True
        if event.event_type is OnChainEventType.TOKEN_CREATED:
            state.symbol = str(payload.get("symbol")) if payload.get("symbol") is not None else state.symbol
            state.name = str(payload.get("name")) if payload.get("name") is not None else state.name
            state.deployer = payload.get("deployer") or state.deployer
            if "mint_authority" in payload:
                state.mint_authority = payload.get("mint_authority")
            if "freeze_authority" in payload:
                state.freeze_authority = payload.get("freeze_authority")
            if event.chain_verified and (
                "mint_authority" in payload or "freeze_authority" in payload
            ):
                state.verified_authority_evidence = True
            state.funding_path = list(payload.get("funding_path", state.funding_path))
            state.linked_wallets.update(str(item) for item in payload.get("linked_wallets", []))
        elif event.event_type is OnChainEventType.POOL_CREATED:
            state.verified_pool_evidence = state.verified_pool_evidence or event.chain_verified
            state.verified_liquidity_evidence = state.verified_liquidity_evidence or event.chain_verified
            state.pool_id = str(payload.get("pool_id")) if payload.get("pool_id") else state.pool_id
            state.pool_provider = str(payload.get("pool_provider")) if payload.get("pool_provider") else state.pool_provider
            state.liquidity_usd = max(0.0, float(payload.get("liquidity_usd", state.liquidity_usd)))
        elif event.event_type is OnChainEventType.LIQUIDITY_ADDED:
            state.verified_liquidity_evidence = state.verified_liquidity_evidence or event.chain_verified
            state.liquidity_usd += max(0.0, float(payload.get("liquidity_usd", 0)))
        elif event.event_type is OnChainEventType.LIQUIDITY_REMOVED:
            state.verified_liquidity_evidence = state.verified_liquidity_evidence or event.chain_verified
            removed = max(0.0, float(payload.get("liquidity_usd", 0)))
            state.removed_liquidity_usd += removed
            state.liquidity_usd = max(0.0, state.liquidity_usd - removed)
        elif event.event_type is OnChainEventType.LIQUIDITY_SNAPSHOT:
            if event.chain_verified and bool(payload.get("pool_verified", False)):
                state.verified_pool_evidence = True
            state.verified_liquidity_evidence = state.verified_liquidity_evidence or event.chain_verified
            state.pool_id = str(payload.get("pool_id")) if payload.get("pool_id") else state.pool_id
            state.pool_provider = str(payload.get("pool_provider")) if payload.get("pool_provider") else state.pool_provider
            liquidity = max(0.0, float(payload.get("liquidity_usd", 0)))
            if state.liquidity_usd > liquidity:
                state.removed_liquidity_usd += state.liquidity_usd - liquidity
            state.liquidity_usd = liquidity
        elif event.event_type is OnChainEventType.TRADE:
            side = str(payload.get("side", "")).lower()
            wallet = str(payload.get("wallet", ""))
            counterparty = str(payload.get("counterparty", ""))
            volume = max(0.0, float(payload.get("volume_usd", 0)))
            state.realized_volume_usd += volume
            if payload.get("wash_suspected", False):
                state.wash_volume_usd += volume
            if wallet and counterparty and wallet == counterparty:
                state.self_trade_volume_usd += volume
            if side == "buy":
                state.buy_count += 1
                if wallet:
                    state.buyers.add(wallet)
            elif side == "sell":
                state.sell_count += 1
                if wallet:
                    state.sellers.add(wallet)
            if wallet:
                state.wallet_trades[wallet] = state.wallet_trades.get(wallet, 0) + 1
                if state.wallet_trades[wallet] > 1:
                    state.repeat_traders.add(wallet)
            state.bundled_wallet_percent = max(state.bundled_wallet_percent, float(payload.get("bundled_wallet_percent", 0)))
            state.sniper_wallet_percent = max(state.sniper_wallet_percent, float(payload.get("sniper_wallet_percent", 0)))
            state.synchronized_buy_percent = max(state.synchronized_buy_percent, float(payload.get("synchronized_buy_percent", 0)))
        elif event.event_type is OnChainEventType.HOLDER_SNAPSHOT:
            complete = bool(payload.get("holder_aggregation_available", True)) and (
                payload.get("top_holder_percent") is not None
                or payload.get("top10_holder_percent") is not None
            )
            state.verified_holder_evidence = state.verified_holder_evidence or (
                event.chain_verified and complete
            )
            state.top_holder_percent = float(payload["top_holder_percent"]) if payload.get("top_holder_percent") is not None else state.top_holder_percent
            state.top10_holder_percent = float(payload["top10_holder_percent"]) if payload.get("top10_holder_percent") is not None else state.top10_holder_percent
            state.top_token_account_percent = float(payload["top_token_account_percent"]) if payload.get("top_token_account_percent") is not None else state.top_token_account_percent
            state.top10_token_account_percent = float(payload["top10_token_account_percent"]) if payload.get("top10_token_account_percent") is not None else state.top10_token_account_percent
            state.holder_count = int(payload.get("holder_count", state.holder_count))
            state.organic_wallet_growth = float(payload.get("organic_wallet_growth", state.organic_wallet_growth))
            state.bundled_wallet_percent = max(state.bundled_wallet_percent, float(payload.get("bundled_wallet_percent", 0)))
            state.sniper_wallet_percent = max(state.sniper_wallet_percent, float(payload.get("sniper_wallet_percent", 0)))
        elif event.event_type is OnChainEventType.AUTHORITY_STATE:
            state.verified_authority_evidence = state.verified_authority_evidence or event.chain_verified
            state.mint_authority = payload.get("mint_authority")
            state.freeze_authority = payload.get("freeze_authority")
            state.update_authority = payload.get("update_authority")
            state.owner = payload.get("owner") or state.owner
            state.proxy_implementation = payload.get("proxy_implementation")
            state.upgradeable_proxy = bool(payload.get("upgradeable_proxy", state.upgradeable_proxy))
        elif event.event_type is OnChainEventType.CONTRACT_RISK:
            if event.chain_verified:
                state.contract_risk_checked = True
                state.verified_contract_risk_evidence = True
            state.honeypot = bool(payload.get("honeypot", state.honeypot))
            state.sell_tax_percent = max(state.sell_tax_percent, float(payload.get("sell_tax_percent", 0)))
            state.blacklist = bool(payload.get("blacklist", state.blacklist))
            state.transfer_restricted = bool(payload.get("transfer_restricted", state.transfer_restricted))
            state.upgradeable_proxy = bool(payload.get("upgradeable_proxy", state.upgradeable_proxy))
            state.proxy_implementation = payload.get("proxy_implementation") or state.proxy_implementation
        elif event.event_type is OnChainEventType.TRANSFER:
            amount = float(payload.get("amount_usd", 0))
            category = str(payload.get("category", ""))
            direction = 1.0 if str(payload.get("direction", "in")) == "in" else -1.0
            if category == "smart_wallet":
                state.smart_wallet_net_usd += amount * direction
            elif category == "exchange":
                state.exchange_net_usd += amount * direction
            elif category in {"treasury", "foundation"}:
                state.treasury_net_usd += amount * direction
        elif event.event_type is OnChainEventType.WALLET_LINK:
            state.linked_wallets.update(str(item) for item in payload.get("wallets", []))
            state.funding_path = list(payload.get("funding_path", state.funding_path))
        elif event.event_type is OnChainEventType.OFFICIAL_IDENTITY:
            credibility = float(payload.get("credibility", 0))
            exact = bool(payload.get("exact_chain_contract", False))
            policy_verified = bool(payload.get("source_policy_verified", False))
            if exact and credibility >= 0.8 and policy_verified:
                state.official_identity_confirmed = True
                state.official_confirmation_received_at_ms = event.received_at_ms
                published = int(payload.get("published_at_ms", event.exchange_timestamp_ms))
                state.detection_latency_ms = max(0, event.received_at_ms - published)
        elif event.event_type is OnChainEventType.MARKET_SNAPSHOT:
            state.market_cap_usd = float(payload["market_cap_usd"]) if payload.get("market_cap_usd") is not None else state.market_cap_usd
        elif event.event_type is OnChainEventType.PAPER_EXIT_QUOTE:
            if not (state.verified_pool_evidence and state.verified_liquidity_evidence):
                return
            quote = PaperExitQuote(
                notional_usd=float(payload["notional_usd"]),
                expected_output_usd=float(payload["expected_output_usd"]),
                slippage_bps=float(payload["slippage_bps"]),
                price_impact_bps=float(payload.get("price_impact_bps", payload["slippage_bps"])),
                fees_usd=float(payload.get("fees_usd", 0)),
                provider=str(payload.get("provider", event.provider_id)),
                quoted_at_ms=event.received_at_ms,
            )
            key = self._notional_key(quote.notional_usd)
            state.quotes[key] = quote
            state.quote_metadata[key] = {
                "entry_price": float(payload["entry_price"]) if payload.get("entry_price") is not None else None,
                "latency_disadvantage_bps": float(payload.get("latency_disadvantage_bps", 0)),
                "mev_disadvantage_bps": float(payload.get("mev_disadvantage_bps", 0)),
            }
        elif event.event_type is OnChainEventType.CAPABILITY_STATE:
            state.unavailable_capabilities.update(
                str(value) for value in payload.get("not_available", [])
            )
            state.required_unavailable_capabilities.update(
                str(value) for value in payload.get("eligibility_required_not_available", [])
            )

    @staticmethod
    def _notional_key(value: float) -> float:
        return round(float(value), 8)

    def _required_capacity_usd(self) -> float:
        return float(self.config["paper_notional_usd"]) * float(
            self.config["minimum_sellable_liquidity_multiple"]
        )

    def _quote_is_fresh(self, quote: PaperExitQuote, as_of_ms: int) -> bool:
        age_ms = as_of_ms - quote.quoted_at_ms
        return 0 <= age_ms <= int(self.config["ttl_minutes"]) * 60_000

    def _quote_slippage_acceptable(self, quote: PaperExitQuote) -> bool:
        return quote.effective_slippage_bps <= float(self.config["maximum_slippage_bps"]) + 1e-9

    def _select_quote_evidence(self, state: _State, as_of_ms: int) -> None:
        """Select exact position quote and derive capacity only from input notionals."""
        paper_key = self._notional_key(float(self.config["paper_notional_usd"]))
        state.quote = state.quotes.get(paper_key)
        metadata = state.quote_metadata.get(paper_key, {})
        state.entry_price = metadata.get("entry_price")
        state.latency_disadvantage_bps = float(metadata.get("latency_disadvantage_bps") or 0)
        state.mev_disadvantage_bps = float(metadata.get("mev_disadvantage_bps") or 0)
        valid = [
            quote for quote in state.quotes.values()
            if self._quote_is_fresh(quote, as_of_ms)
            and self._quote_slippage_acceptable(quote)
        ]
        state.proven_sellable_capacity_usd = max(
            (quote.notional_usd for quote in valid), default=0.0
        )

    def _score(self, state: _State, as_of_ms: int) -> tuple[CandidateScores, list[str], bool]:
        cfg = self.config
        total_trades = state.buy_count + state.sell_count
        attention = _clamp(
            10 + min(35, math.log10(max(state.realized_volume_usd, 1)) * 8)
            + min(25, total_trades * 1.5) + min(30, len(state.buyers) * 2)
        )
        authenticity = 15.0
        if state.official_identity_confirmed:
            authenticity += 60
        if state.mint_authority is None:
            authenticity += 12.5
        if state.freeze_authority is None:
            authenticity += 12.5
        liquidity = _clamp(
            50 * state.proven_sellable_capacity_usd / max(float(cfg["minimum_liquidity_usd"]), 1)
            + 50 * state.liquidity_usd / max(float(cfg["minimum_liquidity_usd"]), 1)
        )
        top10 = state.top10_holder_percent if state.top10_holder_percent is not None else 100.0
        distribution = _clamp(100 - top10 + min(25, math.log10(max(state.holder_count, 1)) * 8))
        buy_ratio = state.buy_count / max(total_trades, 1)
        momentum = _clamp(45 * buy_ratio + min(30, len(state.buyers) * 3) + 25 * max(0.0, state.organic_wallet_growth))
        wash_ratio = state.wash_volume_usd / max(state.realized_volume_usd, 1.0)
        manipulation = 0.0
        manipulation += min(60, wash_ratio * 80)
        manipulation += min(35, state.bundled_wallet_percent * 0.5)
        manipulation += min(25, state.sniper_wallet_percent * 0.4)
        manipulation += min(25, state.synchronized_buy_percent * 0.4)
        manipulation += 100 if state.honeypot else 0
        manipulation += min(50, state.sell_tax_percent * 3)
        manipulation += 25 if state.blacklist else 0
        manipulation += 25 if state.transfer_restricted else 0
        manipulation += 20 if state.upgradeable_proxy else 0
        manipulation = _clamp(manipulation)
        if state.quote is None or state.honeypot:
            exit_score = 0.0
        else:
            support = state.proven_sellable_capacity_usd / max(self._required_capacity_usd(), 1)
            slippage_quality = 1 - state.quote.effective_slippage_bps / max(float(cfg["maximum_slippage_bps"]), 1)
            exit_score = _clamp(55 * min(1.0, support) + 45 * max(0.0, slippage_quality))
        scores = CandidateScores(
            round(attention, 4), round(authenticity, 4), round(liquidity, 4),
            round(distribution, 4), round(momentum, 4), round(manipulation, 4), round(exit_score, 4),
        )

        flags: list[str] = []
        hard = False
        for capability in sorted(state.unavailable_capabilities):
            flags.append(f"NOT_AVAILABLE_{capability.upper()}")
        if state.required_unavailable_capabilities:
            hard = True
        if state.chain_verified_events == 0:
            flags.append("NO_DIRECT_CHAIN_VERIFICATION")
            hard = True
        if not state.verified_pool_evidence:
            flags.append("NO_VERIFIED_POOL")
            hard = True
        if not state.verified_liquidity_evidence:
            flags.append("LIQUIDITY_NOT_CHAIN_VERIFIED")
            hard = True
        if not state.official_identity_confirmed:
            flags.append("UNCONFIRMED_OFFICIAL_IDENTITY")
        if state.liquidity_usd < float(cfg["minimum_liquidity_usd"]):
            flags.append("LOW_LIQUIDITY")
            hard = True
        required_capacity = self._required_capacity_usd()
        if state.proven_sellable_capacity_usd + 1e-9 < required_capacity:
            flags.append("PAPER_NOTIONAL_NOT_EXITABLE")
            hard = True
            capacity_quotes = [
                quote for quote in state.quotes.values()
                if quote.notional_usd + 1e-9 >= required_capacity
            ]
            if not capacity_quotes:
                flags.append("MISSING_CAPACITY_QUOTE")
            elif not any(self._quote_is_fresh(quote, as_of_ms) for quote in capacity_quotes):
                flags.append("STALE_CAPACITY_QUOTE")
            elif not any(
                self._quote_is_fresh(quote, as_of_ms)
                and self._quote_slippage_acceptable(quote)
                for quote in capacity_quotes
            ):
                flags.append("EXCESSIVE_CAPACITY_SLIPPAGE")
        if state.quote is None:
            flags.append("MISSING_PAPER_EXIT_QUOTE")
            hard = True
        elif not self._quote_slippage_acceptable(state.quote):
            flags.append("EXCESSIVE_SLIPPAGE")
            hard = True
        elif as_of_ms - state.quote.quoted_at_ms > int(cfg["ttl_minutes"]) * 60_000:
            flags.append("STALE_EXIT_QUOTE")
            hard = True
        if state.top10_holder_percent is None:
            flags.append("HOLDER_DISTRIBUTION_UNKNOWN")
            hard = True
        elif not state.verified_holder_evidence:
            flags.append("HOLDER_DISTRIBUTION_UNVERIFIED")
            hard = True
        elif state.top10_holder_percent > float(cfg["maximum_top10_holder_percent"]):
            flags.append("CONCENTRATED_HOLDERS")
            hard = True
        if state.honeypot:
            flags.append("HONEYPOT_OR_UNSELLABLE")
            hard = True
        if state.sell_tax_percent > float(cfg["maximum_sell_tax_percent"]):
            flags.append("HIGH_SELL_TAX")
            hard = True
        if state.blacklist:
            flags.append("BLACKLIST_CAPABILITY")
            hard = True
        if state.transfer_restricted:
            flags.append("TRANSFER_RESTRICTIONS")
            hard = True
        if not state.contract_risk_checked:
            flags.append("CONTRACT_RISK_UNKNOWN")
            hard = True
        if not state.verified_authority_evidence:
            flags.append("AUTHORITY_STATE_UNVERIFIED")
            hard = True
        if state.upgradeable_proxy:
            flags.append("UPGRADEABLE_PROXY_RISK")
            hard = True
        if cfg["require_revoked_mint_authority"] and state.mint_authority is not None:
            flags.append("MINT_AUTHORITY_ACTIVE_OR_UNKNOWN")
            hard = True
        if cfg["require_revoked_freeze_authority"] and state.freeze_authority is not None:
            flags.append("FREEZE_AUTHORITY_ACTIVE_OR_UNKNOWN")
            hard = True
        if manipulation > float(cfg["maximum_manipulation_risk"]):
            flags.append("MANIPULATION_RISK")
            hard = True
        if state.removed_liquidity_usd > 0 and state.removed_liquidity_usd >= state.liquidity_usd:
            flags.append("MATERIAL_LIQUIDITY_REMOVAL")
            hard = True
        return scores, flags, hard

    def latest(self, limit: int = 10) -> tuple[CandidateAssessment, ...]:
        return tuple(sorted(self._latest.values(), key=lambda item: item.as_of_ms, reverse=True)[:limit])

    @classmethod
    def replay(
        cls,
        events: Iterable[OnChainEvent],
        *,
        as_of_ms: int,
        config: dict[str, Any] | None = None,
    ) -> tuple[CandidateAssessment, ...]:
        pipeline = cls(config)
        for event in sorted(events, key=lambda item: (item.received_at_ms, item.exchange_timestamp_ms, item.event_id)):
            if event.received_at_ms <= as_of_ms:
                pipeline.ingest(event)
        return pipeline.latest(10_000)
