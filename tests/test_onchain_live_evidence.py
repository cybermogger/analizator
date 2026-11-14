from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.data.storage import SQLiteStorage
from src.onchain.base import BaseRPCAdapter
from src.onchain.config import validate_onchain_config
from src.onchain.jupiter import JupiterOrderQuote, JupiterQuoteAdapter, JupiterQuoteError
from src.onchain.solana import SolanaRPCAdapter
from src.service.onchain_intelligence import OnChainIntelligenceService


FIXTURES = Path(__file__).parent / "fixtures/onchain"
MINT_TRANSACTION = json.loads((FIXTURES / "solana_transaction.json").read_text(encoding="utf-8"))
LIVE = json.loads((FIXTURES / "solana_live_evidence.json").read_text(encoding="utf-8"))
BASE = json.loads((FIXTURES / "base_receipt.json").read_text(encoding="utf-8"))
POOL_SIGNATURE = LIVE["pool_transaction"]["transaction"]["signatures"][0]


def live_config() -> dict[str, Any]:
    identity = LIVE["identity"]
    return {
        "onchain": {
            "enabled": True,
            "paper_notionals_usd": [1000],
            "risk_filter": {"paper_notional_usd": 1000},
            "enrichment": {
                "jupiter_quote": {
                    "enabled": True,
                    "url": "https://api.jup.ag/swap/v2/order",
                    "api_key_env": "JUPITER_API_KEY",
                    "request_timeout_seconds": 8,
                    "min_interval_seconds": 0,
                    "stable_mint": identity["quote_mint"],
                    "stable_decimals": 6,
                }
            },
            "providers": {
                "solana_mainnet": {
                    "chain_id": "solana", "adapter": "solana_rpc", "enabled": False,
                    "rpc_url": "https://api.mainnet-beta.solana.com",
                    "websocket_url": "wss://api.mainnet-beta.solana.com",
                    "request_timeout_seconds": 8,
                    "configured_pools": [{
                        **identity, "token_decimals": 6, "quote_decimals": 6,
                        "quote_is_usd": True,
                    }],
                },
                "base_mainnet": {
                    "chain_id": "eip155:8453", "adapter": "base_rpc", "enabled": False,
                    "rpc_url": "https://mainnet.base.org", "websocket_url": "",
                    "request_timeout_seconds": 8,
                },
            },
        }
    }


class FixtureSolanaAdapter(SolanaRPCAdapter):
    async def rpc(self, method: str, params: list[Any]) -> Any:
        identity = LIVE["identity"]
        if method == "getTransaction":
            if params[0] == MINT_TRANSACTION["signature"]:
                return MINT_TRANSACTION["transaction"]
            if params[0] == POOL_SIGNATURE:
                return LIVE["pool_transaction"]
        if method == "getBlock":
            return LIVE["blocks"][str(params[0])]
        if method == "getTokenSupply" and params[0] == identity["mint"]:
            return {"context": {"slot": LIVE["mint_snapshot"]["slot"]}, "value": LIVE["mint_snapshot"]["supply"]}
        if method == "getTokenLargestAccounts" and params[0] == identity["mint"]:
            return {"context": {"slot": LIVE["mint_snapshot"]["slot"]}, "value": LIVE["mint_snapshot"]["largest_accounts"]}
        if method == "getAccountInfo":
            values = {
                identity["mint"]: LIVE["mint_snapshot"]["account"],
                identity["pool_id"]: LIVE["pool_accounts"]["pool"],
                identity["token_vault"]: LIVE["pool_accounts"]["token_vault"],
                identity["quote_vault"]: LIVE["pool_accounts"]["quote_vault"],
            }
            if params[0] in values:
                return {"context": {"slot": LIVE["mint_snapshot"]["slot"]}, "value": values[params[0]]}
        raise AssertionError(f"unexpected Solana fixture RPC: {method} {params}")


class FixtureJupiterAdapter(JupiterQuoteAdapter):
    async def order(self, input_mint: str, output_mint: str, amount: int) -> JupiterOrderQuote:
        if input_mint == LIVE["identity"]["quote_mint"]:
            key = "buy_3000" if amount == 3_000_000_000 else "buy"
        else:
            key = "sell_3000" if amount == 15_000_000_000 else "sell"
        body = LIVE["jupiter"][key]
        return self.parse_order(body, input_mint, output_mint, amount)


class FixtureBaseAdapter(BaseRPCAdapter):
    async def rpc(self, method: str, params: list[Any]) -> Any:
        values = {
            "eth_chainId": "0x2105",
            "eth_getTransactionReceipt": BASE["receipt"],
            "eth_getBlockByHash": BASE["block"],
            "eth_getCode": "0x60006000",
            "eth_getStorageAt": "0x" + "0" * 64,
        }
        if method not in values:
            raise AssertionError(f"unexpected Base fixture RPC: {method} {params}")
        return values[method]


def test_realistic_solana_payloads_flow_adapter_service_storage_pipeline(tmp_path: Path):
    config = live_config()
    validate_onchain_config(config)
    storage = SQLiteStorage(tmp_path / "solana-live-evidence.db")
    service = OnChainIntelligenceService(config, storage)
    settings = config["onchain"]["providers"]["solana_mainnet"]
    service.providers["solana_mainnet"] = FixtureSolanaAdapter("solana_mainnet", settings)
    service.quote_adapter = FixtureJupiterAdapter(
        "jupiter_quote", config["onchain"]["enrichment"]["jupiter_quote"]
    )

    async def run() -> None:
        await service._solana_signature("solana_mainnet", MINT_TRANSACTION["signature"], MINT_TRANSACTION["slot"])
        await service._solana_signature("solana_mainnet", POOL_SIGNATURE, LIVE["pool_transaction"]["slot"])
        # Duplicate stream delivery must not duplicate immutable evidence.
        await service._solana_signature("solana_mainnet", POOL_SIGNATURE, LIVE["pool_transaction"]["slot"])

    try:
        asyncio.run(run())
        kinds = [row["event_type"] for row in storage.query(
            "SELECT event_type FROM onchain_events ORDER BY recorded_at_ms,event_id"
        )]
        for required in (
            "TOKEN_CREATED", "AUTHORITY_STATE", "HOLDER_SNAPSHOT", "CAPABILITY_STATE",
            "LIQUIDITY_SNAPSHOT", "TRADE", "PAPER_EXIT_QUOTE",
        ):
            assert required in kinds
        assert "CONTRACT_RISK" not in kinds  # unsupported safety values are never invented
        assert kinds.count("LIQUIDITY_SNAPSHOT") == 1
        assert kinds.count("PAPER_EXIT_QUOTE") == 2  # exact $1k plus auto-added $3k capacity

        assessment = service.latest(1)[0]
        assert assessment["pool_id"] == LIVE["identity"]["pool_id"]
        assert assessment["metrics"]["verified_pool_evidence"] is True
        assert assessment["metrics"]["verified_liquidity_evidence"] is True
        assert assessment["liquidity_usd"] == pytest.approx(51_000)
        assert assessment["proven_sellable_capacity_usd"] == pytest.approx(3000)
        assert assessment["metrics"]["required_sellable_capacity_usd"] == pytest.approx(3000)
        assert set(assessment["metrics"]["paper_exit_quotes"]) == {"1000.0", "3000.0"}
        assert assessment["metrics"]["top10_token_account_percent"] == pytest.approx(35)
        assert assessment["top10_holder_percent"] is None
        assert assessment["metrics"]["buy_count"] == 1
        assert assessment["metrics"]["realized_volume_usd"] == pytest.approx(1000)
        assert assessment["metrics"]["wash_volume_usd"] is None
        assert assessment["metrics"]["bundled_wallet_percent"] is None
        assert assessment["metrics"]["honeypot"] is None
        assert assessment["paper_exit_quote"]["net_output_usd"] == pytest.approx(970)
        assert assessment["paper_exit_quote"]["slippage_bps"] == pytest.approx(300)
        assert assessment["paper_exit_quote"]["notional_usd"] == pytest.approx(1000)
        assert assessment["status"] == "REJECTED"
        for gap in (
            "NOT_AVAILABLE_HOLDER_CONCENTRATION",
            "NOT_AVAILABLE_BUNDLE_SNIPER_ATTRIBUTION",
            "NOT_AVAILABLE_WASH_TRADE_ATTRIBUTION",
            "NOT_AVAILABLE_HONEYPOT_TAX_BLACKLIST",
        ):
            assert gap in assessment["risk_flags"]

        health = service.health()["solana_mainnet"]
        assert health["capabilities"]["configured_pool_liquidity"]["status"] == "AVAILABLE"
        assert health["capabilities"]["holder_concentration"]["status"] == "NOT_AVAILABLE"
        assert service.health()["jupiter_quote"]["status"] == "healthy"
    finally:
        storage.close()


def test_base_realistic_payload_exposes_unsupported_evidence_and_rejects(tmp_path: Path):
    config = live_config()
    storage = SQLiteStorage(tmp_path / "base-live-evidence.db")
    service = OnChainIntelligenceService(config, storage)
    settings = config["onchain"]["providers"]["base_mainnet"]
    service.providers["base_mainnet"] = FixtureBaseAdapter("base_mainnet", settings)
    try:
        asyncio.run(service._base_log("base_mainnet", {
            "transactionHash": BASE["receipt"]["transactionHash"]
        }))
        assessment = service.latest(1)[0]
        assert assessment["chain_id"] == "eip155:8453"
        assert assessment["status"] == "REJECTED"
        assert "NOT_AVAILABLE_HOLDER_CONCENTRATION" in assessment["risk_flags"]
        assert "NOT_AVAILABLE_PAPER_EXIT_CAPACITY" in assessment["risk_flags"]
        assert "NOT_AVAILABLE_PAPER_EXIT_QUOTE" in assessment["risk_flags"]
        health = service.health()["base_mainnet"]
        assert health["capabilities"]["trade_flow"]["status"] == "NOT_AVAILABLE"
        assert health["capabilities"]["proxy_upgrade_risk"]["status"] == "AVAILABLE"
    finally:
        storage.close()


def test_jupiter_quote_only_parser_rejects_transaction_material():
    body = dict(LIVE["jupiter"]["buy"])
    body["transaction"] = "unsigned-transaction-is-still-not-accepted"
    with pytest.raises(JupiterQuoteError, match="transaction"):
        JupiterQuoteAdapter.parse_order(
            body, LIVE["identity"]["quote_mint"], LIVE["identity"]["mint"], 1_000_000_000
        )
