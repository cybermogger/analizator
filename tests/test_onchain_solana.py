from __future__ import annotations

import json
import copy
from pathlib import Path

import pytest

from src.onchain.pipeline import OnChainCandidatePipeline
from src.onchain.solana import SolanaRPCAdapter, SolanaRPCError, VerifiedSolanaTransaction
from src.onchain.types import CandidateStatus, ChainId, OnChainEvent, OnChainEventType, TokenIdentity


FIXTURES = Path(__file__).parent / "fixtures/onchain"
TRUMP = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
FAKE_TRUMP = "FakeTrumpMint111111111111111111111111111111"
START = 1_737_165_000_000


def event(
    kind: OnChainEventType,
    sequence: int,
    payload: dict,
    *,
    contract: str = TRUMP,
    received_offset_ms: int | None = None,
    block_hash: str | None = None,
    transaction_index: int | None = None,
    log_index: int | None = None,
    verified: bool = True,
) -> OnChainEvent:
    received = START + (received_offset_ms if received_offset_ms is not None else sequence * 1000)
    return OnChainEvent.build(
        event_id=f"solana:{contract}:{kind.value}:{sequence}:{block_hash or 'canonical'}",
        provider_id="solana-mainnet",
        chain_id=ChainId.SOLANA,
        contract_address=contract,
        event_type=kind,
        block_number=300_000_000 + sequence,
        block_hash=block_hash or f"block-{sequence}",
        parent_hash=f"block-{sequence - 1}" if sequence else None,
        transaction_id=f"tx-{contract}-{sequence}",
        transaction_index=(0 if contract == TRUMP else 1) if transaction_index is None else transaction_index,
        log_index=sequence if log_index is None else log_index,
        exchange_timestamp_ms=received - 250,
        received_at_ms=received,
        payload=payload,
        chain_verified=verified,
        confirmations=1,
    )


def healthy_events(contract: str = TRUMP, *, official: bool = True) -> list[OnChainEvent]:
    values = [
        event(OnChainEventType.TOKEN_CREATED, 1, {
            "symbol": "TRUMP", "name": "Official Trump", "deployer": "deployer-1",
            "funding_path": ["exchange-hot-wallet", "deployer-1"], "linked_wallets": ["deployer-2"],
        }, contract=contract),
        event(OnChainEventType.POOL_CREATED, 2, {
            "pool_id": f"pool-{contract}", "pool_provider": "direct-solana-dex-program",
            "liquidity_usd": 500_000,
        }, contract=contract),
        event(OnChainEventType.AUTHORITY_STATE, 3, {
            "mint_authority": None, "freeze_authority": None, "update_authority": None,
            "owner": "spl-token-program", "upgradeable_proxy": False,
        }, contract=contract),
        event(OnChainEventType.HOLDER_SNAPSHOT, 4, {
            "top_holder_percent": 8, "top10_holder_percent": 35, "holder_count": 1200,
            "organic_wallet_growth": 0.8, "bundled_wallet_percent": 5, "sniper_wallet_percent": 4,
        }, contract=contract),
        event(OnChainEventType.TRADE, 5, {
            "side": "buy", "wallet": "buyer-1", "counterparty": "pool", "volume_usd": 20_000,
        }, contract=contract),
        event(OnChainEventType.PAPER_EXIT_QUOTE, 6, {
            "notional_usd": 1000, "expected_output_usd": 970, "slippage_bps": 300,
            "price_impact_bps": 200, "fees_usd": 0, "provider": "direct-pool-state",
            "entry_price": 0.20, "latency_disadvantage_bps": 80, "mev_disadvantage_bps": 120,
        }, contract=contract),
        event(OnChainEventType.CONTRACT_RISK, 6, {
            "honeypot": False, "sell_tax_percent": 0, "blacklist": False,
            "transfer_restricted": False, "upgradeable_proxy": False,
        }, contract=contract, log_index=60),
        event(OnChainEventType.PAPER_EXIT_QUOTE, 7, {
            "notional_usd": 3000, "expected_output_usd": 2850, "slippage_bps": 500,
            "price_impact_bps": 400, "fees_usd": 0, "provider": "direct-pool-state",
        }, contract=contract, received_offset_ms=6_000),
    ]
    if official:
        values.append(event(OnChainEventType.OFFICIAL_IDENTITY, 8, {
            "exact_chain_contract": True, "credibility": 0.95,
            "source_policy_verified": True,
            "published_at_ms": START + 6_500, "source_event_id": "phase2-official-post",
        }, contract=contract, received_offset_ms=7_000, verified=False))
    return values


def replay(values: list[OnChainEvent], as_of: int | None = None):
    end = as_of if as_of is not None else max(item.received_at_ms for item in values)
    result = OnChainCandidatePipeline.replay(values, as_of_ms=end)
    return next(item for item in result if item.identity.contract_address == values[0].identity.contract_address)


def test_direct_solana_rpc_contract_is_read_only_and_parses_verified_mint():
    fixture = json.loads((FIXTURES / "solana_transaction.json").read_text(encoding="utf-8"))
    verified = VerifiedSolanaTransaction(
        fixture["signature"], fixture["slot"], fixture["blockhash"],
        fixture["previous_blockhash"], fixture["block_time_ms"], fixture["transaction"],
    )
    adapter = SolanaRPCAdapter("solana-mainnet", {
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "websocket_url": "wss://api.mainnet-beta.solana.com",
        "request_timeout_seconds": 8,
    })
    events = adapter.decode_token_creation(verified, fixture["block_time_ms"] + 500)
    assert len(events) == 1
    assert events[0].identity == TokenIdentity(ChainId.SOLANA, TRUMP)
    assert events[0].chain_verified
    assert events[0].payload["deployer"].startswith("Deployer")
    with pytest.raises(ValueError, match="not permitted"):
        import asyncio
        asyncio.run(adapter.rpc("sendTransaction", []))


def test_solana_log_notification_schema_and_failed_transaction_filter():
    value = {
        "jsonrpc": "2.0", "method": "logsNotification",
        "params": {"result": {"context": {"slot": 3557}, "value": {"signature": "sig", "err": None, "logs": []}}},
    }
    assert SolanaRPCAdapter.parse_log_notification(value) == ("sig", 3557)
    value["params"]["result"]["value"]["err"] = {"InstructionError": [0, "Custom"]}
    assert SolanaRPCAdapter.parse_log_notification(value) is None
    with pytest.raises(SolanaRPCError):
        SolanaRPCAdapter.parse_log_notification({"method": "logsNotification", "params": {}})


def test_solana_decoder_finds_inner_mint_and_rejects_unapproved_program():
    fixture = json.loads((FIXTURES / "solana_transaction.json").read_text(encoding="utf-8"))
    original = fixture["transaction"]["transaction"]["message"]["instructions"].pop()
    fixture["transaction"]["meta"]["innerInstructions"] = [
        {"index": 0, "instructions": [original]}
    ]
    verified = VerifiedSolanaTransaction(
        fixture["signature"], fixture["slot"], fixture["blockhash"],
        fixture["previous_blockhash"], fixture["block_time_ms"], fixture["transaction"],
    )
    adapter = SolanaRPCAdapter("solana-mainnet", {
        "rpc_url": "https://api.mainnet-beta.solana.com",
        "websocket_url": "wss://api.mainnet-beta.solana.com",
        "request_timeout_seconds": 8,
    })
    assert len(adapter.decode_token_creation(verified, fixture["block_time_ms"] + 100)) == 1
    tampered = copy.deepcopy(fixture)
    tampered["transaction"]["meta"]["innerInstructions"][0]["instructions"][0]["programId"] = "Untrusted111111111111111111111111111111111"
    untrusted = VerifiedSolanaTransaction(
        tampered["signature"], tampered["slot"], tampered["blockhash"],
        tampered["previous_blockhash"], tampered["block_time_ms"], tampered["transaction"],
    )
    assert adapter.decode_token_creation(untrusted, tampered["block_time_ms"] + 100) == []


def test_official_trump_replay_never_eligible_before_public_confirmation():
    fixture = json.loads((FIXTURES / "solana_cases.json").read_text(encoding="utf-8"))
    values = healthy_events()
    before = replay(values, START + 6_999)
    assert before.status is CandidateStatus.OBSERVE
    assert not before.official_identity_confirmed
    assert before.minimum_launch_price_known is False
    after = replay(values)
    assert after.status.value == fixture["official_launch"]["expected_after_confirmation"]
    assert after.identity.contract_address == fixture["official_launch"]["contract"]
    assert after.detection_latency_ms == 500
    assert after.feasible_entry_price == pytest.approx(0.204)
    assert after.paper_exit_quote.notional_usd == 1000
    assert after.paper_exit_quote.net_output_usd == 970
    assert after.proven_sellable_capacity_usd == 3000
    assert not after.auto_purchase


def test_exact_position_quote_and_capacity_quote_make_candidate_eligible():
    result = replay(healthy_events())
    assert result.status is CandidateStatus.ELIGIBLE
    assert result.paper_exit_quote.notional_usd == 1000
    assert result.paper_exit_quote.net_output_usd == 970
    assert result.proven_sellable_capacity_usd == 3000
    assert result.metrics["required_sellable_capacity_usd"] == 3000
    assert set(result.metrics["paper_exit_quotes"]) == {"1000.0", "3000.0"}
    assert result.metrics["verified_pool_evidence"] is True
    assert result.metrics["verified_liquidity_evidence"] is True
    assert result.metrics["verified_authority_evidence"] is True
    assert result.metrics["verified_holder_evidence"] is True
    assert result.metrics["verified_contract_risk_evidence"] is True
    assert result.metrics["required_unavailable_capabilities"] == ()
    assert "PAPER_NOTIONAL_NOT_EXITABLE" not in result.risk_flags


def test_missing_capacity_quote_fails_closed_and_net_output_is_not_capacity():
    values = [
        item for item in healthy_events()
        if not (
            item.event_type is OnChainEventType.PAPER_EXIT_QUOTE
            and float(item.payload["notional_usd"]) == 3000
        )
    ]
    result = replay(values)
    assert result.paper_exit_quote.net_output_usd == 970
    assert result.proven_sellable_capacity_usd == 1000
    assert result.status is CandidateStatus.REJECTED
    assert "PAPER_NOTIONAL_NOT_EXITABLE" in result.risk_flags
    assert "MISSING_CAPACITY_QUOTE" in result.risk_flags


def test_capacity_quote_with_excessive_slippage_fails_closed():
    values = healthy_events() + [event(OnChainEventType.PAPER_EXIT_QUOTE, 10, {
        "notional_usd": 3000, "expected_output_usd": 2800,
        "slippage_bps": 600, "fees_usd": 0, "provider": "direct-pool-state",
    })]
    result = replay(values)
    assert result.proven_sellable_capacity_usd == 1000
    assert result.status is CandidateStatus.REJECTED
    assert "PAPER_NOTIONAL_NOT_EXITABLE" in result.risk_flags
    assert "EXCESSIVE_CAPACITY_SLIPPAGE" in result.risk_flags


def test_stale_capacity_quote_fails_closed_while_exact_quote_is_fresh():
    values = healthy_events() + [event(
        OnChainEventType.PAPER_EXIT_QUOTE, 10, {
            "notional_usd": 1000, "expected_output_usd": 970,
            "slippage_bps": 300, "fees_usd": 0, "provider": "direct-pool-state",
        },
        received_offset_ms=1_900_000,
    )]
    result = replay(values)
    assert result.paper_exit_quote.quoted_at_ms == START + 1_900_000
    assert result.proven_sellable_capacity_usd == 1000
    assert result.status is CandidateStatus.REJECTED
    assert "PAPER_NOTIONAL_NOT_EXITABLE" in result.risk_flags
    assert "STALE_CAPACITY_QUOTE" in result.risk_flags


@pytest.mark.parametrize(
    ("case", "extra", "expected_flag"),
    [
        ("liquidity_removal", event(OnChainEventType.LIQUIDITY_REMOVED, 9, {"liquidity_usd": 475_000}), "MATERIAL_LIQUIDITY_REMOVAL"),
        ("honeypot_high_sell_tax", event(OnChainEventType.CONTRACT_RISK, 8, {"honeypot": True, "sell_tax_percent": 99}), "HONEYPOT_OR_UNSELLABLE"),
        ("concentrated_holders", event(OnChainEventType.HOLDER_SNAPSHOT, 8, {"top_holder_percent": 75, "top10_holder_percent": 95, "holder_count": 40}), "CONCENTRATED_HOLDERS"),
        ("wash_volume", event(OnChainEventType.TRADE, 8, {"side": "buy", "wallet": "wash", "counterparty": "other", "volume_usd": 100_000, "wash_suspected": True}), "MANIPULATION_RISK"),
        ("bundled_sniper_wallets", event(OnChainEventType.HOLDER_SNAPSHOT, 8, {"top_holder_percent": 10, "top10_holder_percent": 50, "holder_count": 300, "bundled_wallet_percent": 90, "sniper_wallet_percent": 90}), "MANIPULATION_RISK"),
    ],
)
def test_solana_risk_patterns_are_rejected(case: str, extra: OnChainEvent, expected_flag: str):
    fixture = json.loads((FIXTURES / "solana_cases.json").read_text(encoding="utf-8"))
    result = replay(healthy_events() + [extra])
    assert result.status is CandidateStatus.REJECTED
    assert expected_flag == fixture[case]["risk"]
    assert expected_flag in result.risk_flags


def test_high_market_cap_never_substitutes_for_sellable_capacity():
    values = healthy_events()
    values += [
        event(OnChainEventType.MARKET_SNAPSHOT, 9, {"market_cap_usd": 2_000_000_000}),
        event(OnChainEventType.PAPER_EXIT_QUOTE, 10, {
            "notional_usd": 3000, "expected_output_usd": 2700,
            "slippage_bps": 1000, "fees_usd": 0, "provider": "direct-pool-state",
        }),
    ]
    result = replay(values)
    assert result.market_cap_usd == 2_000_000_000
    assert result.proven_sellable_capacity_usd == 1000
    assert result.status is CandidateStatus.REJECTED
    assert "PAPER_NOTIONAL_NOT_EXITABLE" in result.risk_flags


def test_fake_same_ticker_contract_is_isolated_and_unconfirmed():
    pipeline = OnChainCandidatePipeline()
    for item in healthy_events():
        pipeline.ingest(item)
    for item in healthy_events(FAKE_TRUMP, official=False):
        pipeline.ingest(item)
    values = {item.identity.key: item for item in pipeline.latest()}
    real = values[f"solana:{TRUMP}"]
    fake = values[f"solana:{FAKE_TRUMP}"]
    assert real.status is CandidateStatus.ELIGIBLE
    assert fake.status is CandidateStatus.OBSERVE
    assert "UNCONFIRMED_OFFICIAL_IDENTITY" in fake.risk_flags


def test_duplicate_out_of_order_and_reorg_are_idempotent_and_causal():
    pipeline = OnChainCandidatePipeline()
    values = healthy_events()
    pool = values[1]
    assert pipeline.ingest(pool) is not None
    assert pipeline.ingest(pool).evidence_event_ids == (pool.event_id,)
    pipeline.ingest(values[0])
    before_reorg = pipeline.assess(TokenIdentity(ChainId.SOLANA, TRUMP), pool.received_at_ms)
    assert before_reorg.liquidity_usd == 500_000
    replacement = OnChainEvent.build(
        event_id="replacement-fork-pool", provider_id=pool.provider_id,
        chain_id=pool.identity.chain_id, contract_address=pool.identity.contract_address,
        event_type=OnChainEventType.POOL_CREATED, block_number=pool.block_number,
        block_hash="replacement-block", parent_hash=pool.parent_hash,
        transaction_id="replacement-tx", transaction_index=pool.transaction_index,
        log_index=pool.log_index, exchange_timestamp_ms=pool.exchange_timestamp_ms,
        received_at_ms=pool.received_at_ms + 500,
        payload={"pool_id": "canonical-pool", "pool_provider": "direct", "liquidity_usd": 50_000},
        chain_verified=True,
    )
    pipeline.ingest(replacement)
    after = pipeline.assess(TokenIdentity(ChainId.SOLANA, TRUMP), replacement.received_at_ms)
    assert after.liquidity_usd == 50_000
    assert pool.event_id not in after.evidence_event_ids
    historical = pipeline.assess(TokenIdentity(ChainId.SOLANA, TRUMP), pool.received_at_ms)
    assert historical.liquidity_usd == 500_000
    assert pool.event_id in historical.evidence_event_ids
    replay_before = OnChainCandidatePipeline.replay(values + [replacement], as_of_ms=pool.received_at_ms)
    assert replay_before[0].liquidity_usd == 500_000


def test_unverified_dex_enrichment_cannot_be_sole_source_of_truth():
    unverified = event(
        OnChainEventType.POOL_CREATED, 2,
        {"pool_id": "aggregator", "liquidity_usd": 1_000_000},
        verified=False,
    )
    pipeline = OnChainCandidatePipeline()
    assert pipeline.ingest(unverified) is None
    assert pipeline.latest() == ()
    pipeline.ingest(healthy_events(official=False)[0])
    result = pipeline.assess(TokenIdentity(ChainId.SOLANA, TRUMP), unverified.received_at_ms)
    assert result.status is CandidateStatus.REJECTED
    assert "NO_VERIFIED_POOL" in result.risk_flags
    spoofed_official = event(OnChainEventType.OFFICIAL_IDENTITY, 3, {
        "exact_chain_contract": True, "credibility": 1.0, "published_at_ms": START + 2_500,
    }, verified=False)
    pipeline.ingest(spoofed_official)
    assert not pipeline.latest(1)[0].official_identity_confirmed


def test_chain_clock_ahead_does_not_overwrite_local_receive_time():
    value = OnChainEvent.build(
        event_id="clock-skew", provider_id="solana-mainnet", chain_id=ChainId.SOLANA,
        contract_address=TRUMP, event_type=OnChainEventType.TOKEN_CREATED,
        block_number=1, block_hash="block", transaction_id="tx",
        transaction_index=0, log_index=0, exchange_timestamp_ms=START + 2000,
        received_at_ms=START + 1000, payload={"symbol": "TRUMP"}, chain_verified=True,
    )
    assert value.received_at_ms == START + 1000
    assert value.exchange_timestamp_ms == START + 2000
