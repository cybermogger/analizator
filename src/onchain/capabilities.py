from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class CapabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    status: CapabilityStatus
    reason: str
    condition: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {"status": self.status.value, "reason": self.reason, "condition": self.condition}


def _available(reason: str, condition: str | None = None) -> ProviderCapability:
    return ProviderCapability(CapabilityStatus.AVAILABLE, reason, condition)


def _missing(reason: str) -> ProviderCapability:
    return ProviderCapability(CapabilityStatus.NOT_AVAILABLE, reason)


def provider_capabilities(adapter: str, settings: dict[str, Any], onchain: dict[str, Any]) -> dict[str, ProviderCapability]:
    if adapter == "solana_rpc":
        configured_pools = bool(settings.get("configured_pools"))
        quote_enabled = bool(onchain.get("enrichment", {}).get("jupiter_quote", {}).get("enabled", False))
        return {
            "token_creation": _available("verified getTransaction + canonical getBlock"),
            "mint_authorities": _available("jsonParsed SPL mint account"),
            "token_account_concentration_top20": _available(
                "getTokenSupply + getTokenLargestAccounts; token accounts are not unique owners"
            ),
            "holder_concentration": _missing(
                "standard RPC does not aggregate token accounts by beneficial owner"
            ),
            "holder_count": _missing("standard RPC returns only the 20 largest token accounts"),
            "pool_discovery": _missing("generic RPC has no protocol-independent DEX pool schema"),
            "configured_pool_liquidity": (
                _available("verified token-vault balance deltas", "configured pool and vault identities")
                if configured_pools else _missing("no verified pool/vault specification configured")
            ),
            "configured_pool_trades": (
                _available("verified pool vault deltas and transaction signer", "configured pool and vault identities")
                if configured_pools else _missing("no verified pool/vault specification configured")
            ),
            "paper_exit_capacity": (
                _available("maximum fresh input notional with acceptable round-trip slippage")
                if quote_enabled and configured_pools
                else _missing("requires verified pool evidence and quote-only round trips")
            ),
            "paper_exit_quote": (
                _available("Jupiter Swap V2 quote-only round trip", "verified pool evidence; no taker")
                if quote_enabled and configured_pools
                else _missing("requires enabled quote-only enrichment and a verified configured pool")
            ),
            "honeypot_tax_blacklist": _missing("not standardized by SPL Token or generic RPC"),
            "bundle_sniper_attribution": _missing("requires a separately approved indexed/attribution source"),
            "wash_trade_attribution": _missing("generic RPC cannot prove common control between wallets"),
            "official_identity": _available("Phase-2 exact chain+contract official-source bridge"),
        }
    if adapter == "base_rpc":
        return {
            "token_creation": _available("canonical transaction receipt and contract address"),
            "proxy_upgrade_risk": _available("EIP-1967 implementation slot and bytecode"),
            "erc20_mint_authority": _missing("ERC-20 has no standard mint-authority interface"),
            "holder_concentration": _missing("standard JSON-RPC has no holder index"),
            "pool_discovery": _missing("no protocol-specific Base DEX decoder configured"),
            "paper_exit_capacity": _missing("no verified pool reserves or quoter configured"),
            "trade_flow": _missing("no protocol-specific Swap event decoder configured"),
            "paper_exit_quote": _missing("no verified Base DEX quoter configured"),
            "honeypot_tax_blacklist": _missing("not standardized by ERC-20 or generic JSON-RPC"),
            "bundle_sniper_attribution": _missing("requires a separately approved indexed/attribution source"),
            "wash_trade_attribution": _missing("generic RPC cannot prove common control between wallets"),
            "official_identity": _available("Phase-2 exact chain+contract official-source bridge"),
        }
    raise ValueError(f"unsupported on-chain adapter: {adapter}")


def unavailable_names(capabilities: dict[str, ProviderCapability]) -> tuple[str, ...]:
    return tuple(sorted(name for name, value in capabilities.items() if value.status is CapabilityStatus.NOT_AVAILABLE))
