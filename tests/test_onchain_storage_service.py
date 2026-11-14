from __future__ import annotations

import asyncio
import sqlite3
import copy
from pathlib import Path

import pytest

from src.alerts.formatter import format_onchain_preview
from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.onchain.repository import OnChainRepository
from src.onchain.config import required_quote_notionals, validate_onchain_config
from src.onchain.types import ChainId, OnChainEvent, OnChainEventType, TokenIdentity
from src.service.onchain_intelligence import OnChainIntelligenceService


MINT = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"


def event(event_id: str, block_hash: str, received: int) -> OnChainEvent:
    return OnChainEvent.build(
        event_id=event_id, provider_id="solana_mainnet", chain_id=ChainId.SOLANA,
        contract_address=MINT, event_type=OnChainEventType.TOKEN_CREATED,
        block_number=10, block_hash=block_hash, parent_hash="parent",
        transaction_id="signature", transaction_index=0, log_index=0,
        exchange_timestamp_ms=100, received_at_ms=received,
        payload={"symbol": "TRUMP"}, chain_verified=True,
    )


def service_config() -> dict:
    return {
        "onchain": {
            "enabled": True,
            "paper_notionals_usd": [100, 500, 1000],
            "risk_filter": {"paper_notional_usd": 1000},
            "providers": {
                "solana_mainnet": {
                    "chain_id": "solana", "adapter": "solana_rpc", "enabled": False,
                    "rpc_url": "https://api.mainnet-beta.solana.com",
                    "websocket_url": "wss://api.mainnet-beta.solana.com",
                    "request_timeout_seconds": 8,
                },
                "base_mainnet": {
                    "chain_id": "eip155:8453", "adapter": "base_rpc", "enabled": False,
                    "rpc_url": "https://mainnet.base.org", "websocket_url": "",
                    "request_timeout_seconds": 8,
                },
            },
        }
    }


def test_additive_v6_to_v8_migration_preserves_rows_and_integrity(tmp_path: Path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
    INSERT INTO schema_migrations VALUES (6, 1);
    CREATE TABLE signals(
      id INTEGER PRIMARY KEY AUTOINCREMENT, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
      primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
      payload_json TEXT NOT NULL, model_status TEXT NOT NULL
    );
    INSERT INTO signals(coin,timestamp,primary_horizon,direction,probability,payload_json,model_status)
    VALUES ('SOL',123,'1h','WAIT',50,'{}','MODEL_REJECTED_OOS');
    """)
    connection.commit()
    connection.close()

    storage = SQLiteStorage(path)
    try:
        assert SCHEMA_VERSION == 13
        assert storage.health()["integrity"] == "ok"
        assert storage.health()["schema_version"] == 13
        assert storage.query("SELECT coin,timestamp FROM signals") == [{"coin": "SOL", "timestamp": 123}]
        assert storage.query("SELECT version FROM schema_migrations ORDER BY version") == [
            {"version": 6}, {"version": 7}, {"version": 8}, {"version": 9},
            {"version": 10}, {"version": 11}, {"version": 12}, {"version": 13}
        ]
    finally:
        storage.close()


def test_immutable_point_in_time_reorg_is_safe_when_inserted_out_of_order(tmp_path: Path):
    storage = SQLiteStorage(tmp_path / "chain.db")
    repository = OnChainRepository(storage)
    old = event("old", "block-a", 200)
    replacement = event("replacement", "block-b", 300)
    try:
        repository.insert_event(replacement)
        repository.insert_event(old)
        assert [item.event_id for item in repository.canonical_events(old.identity, 250)] == ["old"]
        assert [item.event_id for item in repository.canonical_events(old.identity, 350)] == ["replacement"]
        assert len(storage.query("SELECT * FROM onchain_reorgs")) == 1
        try:
            storage.execute("UPDATE onchain_events SET payload_json='{}' WHERE event_id='old'")
        except sqlite3.IntegrityError as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError("immutable onchain event was updated")
    finally:
        storage.close()


def test_one_provider_failure_does_not_stop_another(tmp_path: Path):
    storage = SQLiteStorage(tmp_path / "isolation.db")
    service = OnChainIntelligenceService(service_config(), storage)
    completed: list[str] = []

    async def failed() -> None:
        raise ConnectionError("offline")

    async def healthy() -> None:
        completed.append("base")

    async def run() -> None:
        await asyncio.gather(
            service._run_isolated("solana_mainnet", failed),
            service._run_isolated("base_mainnet", healthy),
        )

    try:
        asyncio.run(run())
        assert completed == ["base"]
        assert service.runtime["solana_mainnet"].status == "degraded"
        assert service.runtime["base_mainnet"].status == "healthy"
        assert service.runtime["solana_mainnet"].last_received_at_ms is None
    finally:
        storage.close()


def test_phase2_bridge_requires_exact_chain_contract_and_is_not_candidate_origin(tmp_path: Path):
    class Events:
        def latest_events(self, _limit: int):
            return [{
                "event_id": "official-post", "credibility": 0.95,
                "official_source_confirmed": True,
                "first_received_at_ms": 500, "first_published_at_ms": 450,
                "source_ids": ["official-x"], "evidence_urls": ["https://example.invalid/post"],
                "impacted_assets": [
                    {"chain_id": "solana", "contract_address": MINT},
                    {"chain_id": None, "contract_address": None, "canonical_asset_id": "ticker:TRUMP"},
                ],
            }]

    storage = SQLiteStorage(tmp_path / "bridge.db")
    service = OnChainIntelligenceService(service_config(), storage, Events())
    try:
        assert service.bridge_official_events() == 1
        assert service.bridge_official_events() == 0
        assert service.latest() == []  # off-chain identity cannot originate a token candidate
        service.ingest(event("verified-mint", "block-a", 600))
        latest = service.latest(1)[0]
        assert latest["contract_address"] == MINT
        assert latest["official_identity_confirmed"] is True
        assert len(storage.query("SELECT * FROM onchain_events")) == 2
    finally:
        storage.close()


def test_telegram_preview_has_required_risk_and_no_execution_fields():
    preview = format_onchain_preview({
        "chain_id": "solana", "contract_address": MINT, "symbol": "TRUMP",
        "status": "OBSERVE", "token_age_seconds": 12, "pool_id": "pool",
        "pool_provider": "direct-rpc", "liquidity_usd": 50_000,
        "proven_sellable_capacity_usd": 3_000, "paper_exit_quote": {
            "notional_usd": 1000, "slippage_bps": 250, "net_output_usd": 970,
        },
        "top10_holder_percent": 55, "authorities": {"mint": None, "freeze": None},
        "scores": {}, "risk_flags": ["UNCONFIRMED_OFFICIAL_IDENTITY"],
        "evidence_event_ids": ["mint-1"], "ttl_minutes": 30,
        "invalidation": ["liquidity removal"],
    })
    for expected in (
        "Chain: solana", MINT, "proven exit capacity", "Paper exit $1,000", "Top-10 holders",
        "Authorities", "Evidence", "Risk flags", "TTL", "Invalidation",
        "no automatic purchase or order execution",
    ):
        assert expected in preview


def test_same_ticker_contracts_within_solana_are_isolated(tmp_path: Path):
    other = "FakeTrumpMint11111111111111111111111111111"
    storage = SQLiteStorage(tmp_path / "ticker.db")
    service = OnChainIntelligenceService(service_config(), storage)
    first = event("first", "block-a", 200)
    second = OnChainEvent.build(
        event_id="second", provider_id="solana_mainnet", chain_id="solana",
        contract_address=other, event_type=OnChainEventType.TOKEN_CREATED,
        block_number=11, block_hash="block-b", transaction_id="signature-2",
        transaction_index=1, log_index=0, exchange_timestamp_ms=100,
        received_at_ms=201, payload={"symbol": "TRUMP"}, chain_verified=True,
    )
    try:
        service.ingest(first)
        service.ingest(second)
        identities = {row["contract_address"] for row in service.latest(10)}
        assert identities == {MINT, other}
        assert TokenIdentity("solana", MINT) != TokenIdentity("solana", other)
    finally:
        storage.close()


def test_onchain_config_allows_only_bounded_official_read_endpoints():
    config = service_config()
    validate_onchain_config(config)
    assert required_quote_notionals(config["onchain"]) == [100.0, 500.0, 1000.0, 3000.0]
    invalid_capacity = copy.deepcopy(config)
    invalid_capacity["onchain"]["risk_filter"]["minimum_sellable_liquidity_multiple"] = 0.5
    with pytest.raises(ValueError, match="multiple"):
        validate_onchain_config(invalid_capacity)
    bad = copy.deepcopy(config)
    bad["onchain"]["providers"]["solana_mainnet"]["rpc_url"] = "https://attacker.invalid"
    try:
        validate_onchain_config(bad)
    except ValueError as exc:
        assert "approved official" in str(exc)
    else:
        raise AssertionError("unapproved RPC endpoint accepted")
    secret = copy.deepcopy(config)
    secret["onchain"]["providers"]["base_mainnet"]["private_key"] = "forbidden"
    try:
        validate_onchain_config(secret)
    except ValueError as exc:
        assert "signing/secret" in str(exc)
    else:
        raise AssertionError("private key field accepted")
