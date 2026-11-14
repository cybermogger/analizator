from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from .types import normalize_contract


APPROVED_ENDPOINTS: dict[str, dict[str, tuple[str, set[str]]]] = {
    "solana_rpc": {
        "rpc_url": ("https", {"api.mainnet-beta.solana.com"}),
        "websocket_url": ("wss", {"api.mainnet-beta.solana.com"}),
    },
    "base_rpc": {
        "rpc_url": ("https", {"mainnet.base.org"}),
        "websocket_url": ("wss", {"mainnet.base.org"}),
    },
}


def required_quote_notionals(onchain: Mapping[str, Any]) -> list[float]:
    """Configured paper sizes plus the automatically required capacity probe."""
    values = [float(value) for value in onchain.get("paper_notionals_usd", [])]
    risk = onchain.get("risk_filter", {})
    paper = float(risk.get("paper_notional_usd", 0))
    multiple = float(risk.get("minimum_sellable_liquidity_multiple", 3.0))
    if not values or any(value <= 0 for value in values) or paper <= 0 or multiple < 1:
        raise ValueError("positive paper notionals and capacity multiple >=1 are required")
    values.append(paper * multiple)
    return sorted({round(value, 8) for value in values})


def validate_onchain_config(config: Mapping[str, Any]) -> None:
    section = config.get("onchain", {})
    if not isinstance(section, Mapping):
        raise ValueError("onchain must be a mapping")
    providers = section.get("providers", {})
    if not isinstance(providers, Mapping) or not providers:
        raise ValueError("onchain.providers must be a non-empty mapping")
    for provider_id, raw in providers.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"onchain provider {provider_id} must be a mapping")
        adapter = str(raw.get("adapter", ""))
        expected = APPROVED_ENDPOINTS.get(adapter)
        if expected is None:
            raise ValueError(f"unsupported on-chain adapter: {adapter}")
        timeout = float(raw.get("request_timeout_seconds", 8))
        if not 0 < timeout <= 10:
            raise ValueError(f"onchain provider {provider_id} HTTP timeout must be in (0,10]")
        if float(raw.get("connect_timeout_seconds", 8)) > 10:
            raise ValueError(f"onchain provider {provider_id} connect timeout must be <=10 seconds")
        if float(raw.get("read_timeout_seconds", 30)) > 60:
            raise ValueError(f"onchain provider {provider_id} read timeout must be <=60 seconds")
        for key, (scheme, hosts) in expected.items():
            value = str(raw.get(key, ""))
            if not value and key == "websocket_url" and (
                adapter == "base_rpc" or not bool(raw.get("enabled", False))
            ):
                continue
            parsed = urlparse(value)
            if parsed.scheme != scheme or parsed.hostname not in hosts:
                raise ValueError(
                    f"onchain provider {provider_id}.{key} must use an approved official {scheme} endpoint"
                )
        lowered = {str(key).lower() for key in raw}
        forbidden = {"private_key", "secret", "seed", "mnemonic", "wallet", "signing_key"}
        if lowered & forbidden:
            raise ValueError(f"onchain provider {provider_id} contains signing/secret material")
        env_name = raw.get("authorization_env")
        if env_name is not None and (
            not isinstance(env_name, str)
            or not env_name
            or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in env_name)
        ):
            raise ValueError(f"onchain provider {provider_id}.authorization_env must be an env-var name")
        configured_pools = raw.get("configured_pools", [])
        if not isinstance(configured_pools, list):
            raise ValueError(f"onchain provider {provider_id}.configured_pools must be a list")
        if configured_pools and adapter != "solana_rpc":
            raise ValueError("configured pool decoder currently supports Solana only")
        seen_pools: set[str] = set()
        for pool in configured_pools:
            if not isinstance(pool, Mapping):
                raise ValueError("configured Solana pool must be a mapping")
            required_pool = (
                "pool_id", "program_id", "token_mint", "token_vault", "quote_mint",
                "quote_vault", "token_decimals", "quote_decimals",
            )
            if any(pool.get(name) in (None, "") for name in required_pool):
                raise ValueError("configured Solana pool requires exact pool/program/mint/vault identities")
            for name in ("pool_id", "program_id", "token_mint", "token_vault", "quote_mint", "quote_vault"):
                normalize_contract("solana", str(pool[name]))
            if str(pool["pool_id"]) in seen_pools:
                raise ValueError("configured Solana pool_id values must be unique")
            seen_pools.add(str(pool["pool_id"]))
            if not bool(pool.get("quote_is_usd", False)):
                raise ValueError("configured pool quote_is_usd must be explicit for USD liquidity claims")
            if any(not 0 <= int(pool[name]) <= 18 for name in ("token_decimals", "quote_decimals")):
                raise ValueError("configured pool decimals must be in [0,18]")

    quote = section.get("enrichment", {}).get("jupiter_quote", {})
    if quote:
        if not isinstance(quote, Mapping):
            raise ValueError("onchain.enrichment.jupiter_quote must be a mapping")
        if bool(quote.get("enabled", False)):
            parsed = urlparse(str(quote.get("url", "")))
            if parsed.scheme != "https" or parsed.hostname != "api.jup.ag" or parsed.path != "/swap/v2/order":
                raise ValueError("Jupiter quote enrichment must use official Swap V2 /order")
            if not 0 < float(quote.get("request_timeout_seconds", 8)) <= 10:
                raise ValueError("Jupiter HTTP timeout must be in (0,10]")
            normalize_contract("solana", str(quote.get("stable_mint", "")))
            if not 0 <= int(quote.get("stable_decimals", 6)) <= 18:
                raise ValueError("Jupiter stable_decimals must be in [0,18]")
            env_name = quote.get("api_key_env")
            if env_name is not None and (
                not isinstance(env_name, str) or not env_name
                or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in env_name)
            ):
                raise ValueError("Jupiter api_key_env must be an env-var name")

    notionals = section.get("paper_notionals_usd", [])
    if not isinstance(notionals, list) or not notionals or any(float(value) <= 0 for value in notionals):
        raise ValueError("onchain.paper_notionals_usd must contain positive values")
    if list(map(float, notionals)) != sorted(set(map(float, notionals))):
        raise ValueError("onchain.paper_notionals_usd must be unique and ascending")
    required = float(section.get("risk_filter", {}).get("paper_notional_usd", 0))
    if required not in set(map(float, notionals)):
        raise ValueError("onchain risk paper_notional_usd must be one configured paper notional")
    multiple = float(section.get("risk_filter", {}).get("minimum_sellable_liquidity_multiple", 3.0))
    if multiple < 1:
        raise ValueError("onchain minimum_sellable_liquidity_multiple must be >=1")
