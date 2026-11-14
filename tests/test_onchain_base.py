from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.onchain.base import BaseRPCAdapter, BaseRPCError, VerifiedBaseReceipt
from src.onchain.pipeline import OnChainCandidatePipeline
from src.onchain.types import CandidateStatus, ChainId, OnChainEvent, OnChainEventType, TokenIdentity


FIXTURE = Path(__file__).parent / "fixtures/onchain/base_receipt.json"
BASE_TOKEN = "0x2222222222222222222222222222222222222222"
TIME = 1_737_165_000_000


def base_event(kind: OnChainEventType, sequence: int, payload: dict) -> OnChainEvent:
    return OnChainEvent.build(
        event_id=f"base:{kind.value}:{sequence}", provider_id="base-mainnet",
        chain_id=ChainId.BASE, contract_address=BASE_TOKEN, event_type=kind,
        block_number=10_000 + sequence, block_hash=f"0x{sequence:064x}",
        parent_hash=f"0x{max(0, sequence - 1):064x}", transaction_id=f"0x{sequence + 100:064x}",
        transaction_index=0, log_index=sequence, exchange_timestamp_ms=TIME + sequence * 1000,
        received_at_ms=TIME + sequence * 1000 + 100, payload=payload, chain_verified=True,
    )


def test_base_official_rpc_contract_and_receipt_parser_are_read_only():
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    verified = BaseRPCAdapter.parse_verified_receipt(value["receipt"], value["block"])
    assert isinstance(verified, VerifiedBaseReceipt)
    assert verified.contract_address == BASE_TOKEN
    assert verified.transaction_index == 2
    adapter = BaseRPCAdapter("base-mainnet", {
        "rpc_url": "https://mainnet.base.org", "request_timeout_seconds": 8,
    })
    created = adapter.decode_contract_creation(verified, symbol="SAME", name="Same Ticker", received_at_ms=verified.block_time_ms + 100)
    assert created.identity == TokenIdentity(ChainId.BASE, BASE_TOKEN)
    assert created.payload["deployer"] == "0x1111111111111111111111111111111111111111"
    with pytest.raises(ValueError, match="not permitted"):
        asyncio.run(adapter.rpc("eth_sendRawTransaction", ["0xdeadbeef"]))


def test_base_receipt_rejects_revert_and_block_mismatch():
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["receipt"]["status"] = "0x0"
    with pytest.raises(BaseRPCError):
        BaseRPCAdapter.parse_verified_receipt(value["receipt"], value["block"])
    value["receipt"]["status"] = "0x1"
    value["block"]["hash"] = "0x" + "dd" * 32
    with pytest.raises(BaseRPCError, match="mismatch"):
        BaseRPCAdapter.parse_verified_receipt(value["receipt"], value["block"])


def test_eip1967_proxy_detection_and_zero_slot():
    proxy = "0x" + "00" * 12 + "33" * 20
    assert BaseRPCAdapter.parse_implementation_slot(proxy) == "0x" + "33" * 20
    assert BaseRPCAdapter.parse_implementation_slot("0x" + "00" * 32) is None
    with pytest.raises(BaseRPCError):
        BaseRPCAdapter.parse_implementation_slot("0x1234")


def test_base_proxy_authority_risk_is_rejected():
    values = [
        base_event(OnChainEventType.TOKEN_CREATED, 1, {"symbol": "MEME", "deployer": "0x1"}),
        base_event(OnChainEventType.POOL_CREATED, 2, {"pool_id": "base-pool", "pool_provider": "direct-base-dex", "liquidity_usd": 100_000}),
        base_event(OnChainEventType.AUTHORITY_STATE, 3, {"mint_authority": None, "freeze_authority": None, "update_authority": "0xadmin", "owner": "0xadmin", "upgradeable_proxy": True, "proxy_implementation": "0x3333333333333333333333333333333333333333"}),
        base_event(OnChainEventType.HOLDER_SNAPSHOT, 4, {"top_holder_percent": 10, "top10_holder_percent": 40, "holder_count": 500}),
        base_event(OnChainEventType.PAPER_EXIT_QUOTE, 5, {"notional_usd": 1000, "expected_output_usd": 970, "slippage_bps": 300, "fees_usd": 2, "provider": "direct-pool"}),
        base_event(OnChainEventType.OFFICIAL_IDENTITY, 6, {"exact_chain_contract": True, "credibility": 0.95, "published_at_ms": TIME + 5_000}),
    ]
    result = OnChainCandidatePipeline.replay(values, as_of_ms=values[-1].received_at_ms)[0]
    assert result.status is CandidateStatus.REJECTED
    assert result.proxy_implementation == "0x3333333333333333333333333333333333333333"
    assert "UPGRADEABLE_PROXY_RISK" in result.risk_flags


def test_same_ticker_cross_chain_contracts_never_merge():
    solana = OnChainEvent.build(
        event_id="solana-same", provider_id="solana", chain_id=ChainId.SOLANA,
        contract_address="SameTickerSonaMint111111111111111111",
        event_type=OnChainEventType.TOKEN_CREATED, block_number=1, block_hash="s1", parent_hash=None,
        transaction_id="stx", transaction_index=0, log_index=0,
        exchange_timestamp_ms=TIME, received_at_ms=TIME + 1,
        payload={"symbol": "SAME"}, chain_verified=True,
    )
    base = base_event(OnChainEventType.TOKEN_CREATED, 1, {"symbol": "SAME"})
    pipeline = OnChainCandidatePipeline()
    pipeline.ingest(solana)
    pipeline.ingest(base)
    identities = {item.identity.key for item in pipeline.latest()}
    assert identities == {solana.identity.key, base.identity.key}
