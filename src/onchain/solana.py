from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import os
import time
from typing import Any

import aiohttp
import websockets

from .types import ChainId, OnChainEvent, OnChainEventType


READ_ONLY_METHODS = {
    "getAccountInfo",
    "getBlock",
    "getTokenLargestAccounts",
    "getTokenSupply",
    "getTransaction",
}
TOKEN_PROGRAM_IDS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


class SolanaRPCError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VerifiedSolanaTransaction:
    signature: str
    slot: int
    blockhash: str
    previous_blockhash: str | None
    block_time_ms: int
    transaction: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SolanaPoolSpec:
    pool_id: str
    program_id: str
    token_mint: str
    token_vault: str
    quote_mint: str
    quote_vault: str
    token_decimals: int
    quote_decimals: int
    quote_is_usd: bool

    @classmethod
    def from_config(cls, value: dict[str, Any]) -> "SolanaPoolSpec":
        required = ("pool_id", "program_id", "token_mint", "token_vault", "quote_mint", "quote_vault")
        if any(not value.get(name) for name in required):
            raise ValueError("configured Solana pool requires exact pool/program/mint/vault identities")
        return cls(
            pool_id=str(value["pool_id"]), program_id=str(value["program_id"]),
            token_mint=str(value["token_mint"]), token_vault=str(value["token_vault"]),
            quote_mint=str(value["quote_mint"]), quote_vault=str(value["quote_vault"]),
            token_decimals=int(value["token_decimals"]), quote_decimals=int(value["quote_decimals"]),
            quote_is_usd=bool(value.get("quote_is_usd", False)),
        )


@dataclass(frozen=True, slots=True)
class VerifiedSolanaPool:
    spec: SolanaPoolSpec
    observed_slot: int


class SolanaRPCAdapter:
    """Direct read-only Solana JSON-RPC and logsSubscribe adapter."""

    def __init__(self, provider_id: str, config: dict[str, Any]):
        self.provider_id = provider_id
        self.config = config
        self.rpc_url = str(config["rpc_url"])
        self.websocket_url = str(config["websocket_url"])
        self.timeout = float(config.get("request_timeout_seconds", 8))
        if not 0 < self.timeout <= 10:
            raise ValueError("Solana HTTP timeout must be in (0,10]")
        self.connect_timeout = min(10.0, max(0.5, float(config.get("connect_timeout_seconds", 8))))
        self.read_timeout = min(60.0, max(5.0, float(config.get("read_timeout_seconds", 30))))
        self._request_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": "crypto-intelligence-onchain/1.0"}
        environment_name = self.config.get("authorization_env")
        if environment_name and (token := os.getenv(str(environment_name))):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def rpc(self, method: str, params: list[Any]) -> Any:
        if method not in READ_ONLY_METHODS:
            raise ValueError(f"RPC method {method!r} is not permitted in read-only mode")
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout, headers=self._headers()) as session:
            async with session.post(self.rpc_url, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        if body.get("error"):
            raise SolanaRPCError(f"{method}: {body['error']}")
        if "result" not in body:
            raise SolanaRPCError(f"{method}: missing result")
        return body["result"]

    async def get_account_info(self, address: str) -> dict[str, Any] | None:
        _, value = await self.get_account_info_snapshot(address)
        return value

    async def get_account_info_snapshot(self, address: str) -> tuple[int, dict[str, Any] | None]:
        result = await self.rpc("getAccountInfo", [address, {"commitment": "confirmed", "encoding": "jsonParsed"}])
        if not isinstance(result, dict) or "value" not in result:
            raise SolanaRPCError("getAccountInfo: invalid response")
        context = result.get("context", {})
        if not isinstance(context.get("slot"), int):
            raise SolanaRPCError("getAccountInfo: context slot missing")
        return int(context["slot"]), result["value"]

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        _, values = await self.get_token_largest_accounts_snapshot(mint)
        return values

    async def get_token_largest_accounts_snapshot(self, mint: str) -> tuple[int, list[dict[str, Any]]]:
        result = await self.rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        if not isinstance(result, dict) or not isinstance(result.get("value"), list):
            raise SolanaRPCError("getTokenLargestAccounts: invalid response")
        context = result.get("context", {})
        if not isinstance(context.get("slot"), int):
            raise SolanaRPCError("getTokenLargestAccounts: context slot missing")
        return int(context["slot"]), result["value"]

    async def get_token_supply_snapshot(self, mint: str) -> tuple[int, dict[str, Any]]:
        result = await self.rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
        if not isinstance(result, dict) or not isinstance(result.get("value"), dict):
            raise SolanaRPCError("getTokenSupply: invalid response")
        context = result.get("context", {})
        if not isinstance(context.get("slot"), int):
            raise SolanaRPCError("getTokenSupply: context slot missing")
        return int(context["slot"]), result["value"]

    async def get_block_context(self, slot: int) -> tuple[str, str | None, int]:
        block = await self.rpc("getBlock", [slot, {
            "commitment": "confirmed", "transactionDetails": "none", "rewards": False,
        }])
        if not isinstance(block, dict) or not block.get("blockhash") or not isinstance(block.get("blockTime"), int):
            raise SolanaRPCError("getBlock: invalid snapshot block")
        return str(block["blockhash"]), (
            str(block["previousBlockhash"]) if block.get("previousBlockhash") else None
        ), int(block["blockTime"]) * 1000

    async def fetch_verified_transaction(self, signature: str) -> VerifiedSolanaTransaction:
        result = await self.rpc("getTransaction", [signature, {
            "commitment": "confirmed", "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
        }])
        if result is None:
            raise SolanaRPCError("getTransaction: transaction not found at confirmed commitment")
        if not isinstance(result, dict) or not isinstance(result.get("slot"), int):
            raise SolanaRPCError("getTransaction: invalid response")
        if not isinstance(result.get("meta"), dict) or result["meta"].get("err") is not None:
            raise SolanaRPCError("getTransaction: failed or missing transaction metadata")
        transaction = result.get("transaction")
        signatures = transaction.get("signatures", []) if isinstance(transaction, dict) else []
        if signature not in signatures:
            raise SolanaRPCError("getTransaction: signature mismatch")
        slot = int(result["slot"])
        block = await self.rpc("getBlock", [slot, {
            "commitment": "confirmed", "transactionDetails": "none", "rewards": False,
        }])
        if not isinstance(block, dict) or not block.get("blockhash"):
            raise SolanaRPCError("getBlock: missing canonical block hash")
        block_time = result.get("blockTime") if result.get("blockTime") is not None else block.get("blockTime")
        if not isinstance(block_time, int):
            raise SolanaRPCError("verified transaction has no blockTime")
        return VerifiedSolanaTransaction(
            signature=signature,
            slot=slot,
            blockhash=str(block["blockhash"]),
            previous_blockhash=str(block["previousBlockhash"]) if block.get("previousBlockhash") else None,
            block_time_ms=block_time * 1000,
            transaction=result,
        )

    async def stream_logs(
        self,
        mentioned_address: str,
        emit: Callable[[str, int], Awaitable[None]],
        stop: asyncio.Event,
        status: Callable[[str, Exception | None], Awaitable[None]] | None = None,
    ) -> None:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
            "params": [{"mentions": [mentioned_address]}, {"commitment": "confirmed"}],
        }
        while not stop.is_set():
            try:
                async with websockets.connect(
                    self.websocket_url,
                    open_timeout=self.connect_timeout,
                    close_timeout=self.connect_timeout,
                    ping_interval=20,
                    ping_timeout=10,
                    additional_headers=self._headers(),
                ) as socket:
                    await socket.send(json.dumps(request, separators=(",", ":")))
                    acknowledgement = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.read_timeout))
                    if "result" not in acknowledgement:
                        raise SolanaRPCError(f"logsSubscribe rejected: {acknowledgement.get('error')}")
                    if status is not None:
                        await status("healthy", None)
                    while not stop.is_set():
                        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.read_timeout))
                        notification = self.parse_log_notification(message)
                        if notification is not None:
                            signature, slot = notification
                            await emit(signature, slot)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if status is not None:
                    await status("degraded", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except TimeoutError:
                    pass

    @staticmethod
    def parse_log_notification(value: dict[str, Any]) -> tuple[str, int] | None:
        if value.get("method") != "logsNotification":
            return None
        try:
            result = value["params"]["result"]
            if result["value"].get("err") is not None:
                return None
            return str(result["value"]["signature"]), int(result["context"]["slot"])
        except (KeyError, TypeError, ValueError):
            raise SolanaRPCError("invalid logsNotification payload")

    def decode_token_creation(self, verified: VerifiedSolanaTransaction, received_at_ms: int | None = None) -> list[OnChainEvent]:
        received = received_at_ms or int(time.time() * 1000)
        transaction = verified.transaction.get("transaction", {})
        message = transaction.get("message", {}) if isinstance(transaction, dict) else {}
        instructions = self._ordered_instructions(verified.transaction, message)
        events: list[OnChainEvent] = []
        for index, instruction in instructions:
            parsed = instruction.get("parsed") if isinstance(instruction, dict) else None
            if not isinstance(parsed, dict) or parsed.get("type") not in {"initializeMint", "initializeMint2"}:
                continue
            if str(instruction.get("programId") or "") not in TOKEN_PROGRAM_IDS:
                continue
            info = parsed.get("info", {})
            mint = info.get("mint")
            if not mint:
                continue
            events.append(OnChainEvent.build(
                event_id=f"{self.provider_id}:{verified.signature}:{index}",
                provider_id=self.provider_id,
                chain_id=ChainId.SOLANA,
                contract_address=str(mint),
                event_type=OnChainEventType.TOKEN_CREATED,
                block_number=verified.slot,
                block_hash=verified.blockhash,
                parent_hash=verified.previous_blockhash,
                transaction_id=verified.signature,
                transaction_index=int(verified.transaction.get("meta", {}).get("transactionIndex", 0) or 0),
                log_index=index,
                exchange_timestamp_ms=verified.block_time_ms,
                received_at_ms=received,
                payload={
                    "decimals": int(info.get("decimals", 0)),
                    "mint_authority": info.get("mintAuthority"),
                    "freeze_authority": info.get("freezeAuthority"),
                    "deployer": self._first_signer(message.get("accountKeys", [])),
                },
                chain_verified=True,
                confirmations=1,
            ))
        return events

    async def produce_mint_evidence(
        self, origin: OnChainEvent, received_at_ms: int | None = None
    ) -> list[OnChainEvent]:
        """Read verified SPL account snapshots without pretending token accounts are holders."""
        mint = origin.identity.contract_address
        (mint_slot, account), (supply_slot, supply), (largest_slot, largest) = await asyncio.gather(
            self.get_account_info_snapshot(mint),
            self.get_token_supply_snapshot(mint),
            self.get_token_largest_accounts_snapshot(mint),
        )
        if not isinstance(account, dict) or str(account.get("owner")) not in TOKEN_PROGRAM_IDS:
            raise SolanaRPCError("mint account is not owned by an approved SPL Token program")
        parsed = account.get("data", {}).get("parsed", {})
        if parsed.get("type") != "mint" or not isinstance(parsed.get("info"), dict):
            raise SolanaRPCError("mint account is not a jsonParsed SPL mint")
        info = parsed["info"]
        decimals = int(supply.get("decimals", info.get("decimals", -1)))
        raw_supply = int(str(supply.get("amount", "0")))
        if decimals < 0 or raw_supply <= 0 or int(info.get("decimals", decimals)) != decimals:
            raise SolanaRPCError("inconsistent or zero SPL token supply")
        balances = [max(0, int(str(value.get("amount", "0")))) for value in largest]
        top_one = balances[0] / raw_supply * 100 if balances else 0.0
        top_ten = sum(balances[:10]) / raw_supply * 100
        snapshot_slot = max(mint_slot, supply_slot, largest_slot)
        blockhash, parent_hash, block_time_ms = await self.get_block_context(snapshot_slot)
        received = received_at_ms or int(time.time() * 1000)
        common = {
            "provider_id": self.provider_id,
            "chain_id": ChainId.SOLANA,
            "contract_address": mint,
            "block_number": snapshot_slot,
            "block_hash": blockhash,
            "parent_hash": parent_hash,
            "transaction_id": f"account-snapshot:{snapshot_slot}:{mint}",
            # Account snapshots have a slot but no transaction position. Reserve a
            # synthetic index outside Solana's possible per-slot transaction range.
            "transaction_index": 2_147_000_000,
            "exchange_timestamp_ms": block_time_ms,
            "received_at_ms": received,
            "chain_verified": True,
            "confirmations": 1,
        }
        return [
            OnChainEvent.build(
                event_id=f"{self.provider_id}:mint-state:{mint}:{snapshot_slot}",
                event_type=OnChainEventType.AUTHORITY_STATE,
                log_index=0,
                payload={
                    "mint_authority": info.get("mintAuthority"),
                    "freeze_authority": info.get("freezeAuthority"),
                    "update_authority": None,
                    "update_authority_applicable": False,
                    "owner": str(account["owner"]),
                    "decimals": decimals,
                    "supply_raw": str(raw_supply),
                    "snapshot_slots": {
                        "mint": mint_slot, "supply": supply_slot, "largest_accounts": largest_slot,
                    },
                },
                **common,
            ),
            OnChainEvent.build(
                event_id=f"{self.provider_id}:token-account-concentration:{mint}:{snapshot_slot}",
                event_type=OnChainEventType.HOLDER_SNAPSHOT,
                log_index=1,
                payload={
                    "top_token_account_percent": top_one,
                    "top10_token_account_percent": top_ten,
                    "observed_token_accounts": len(balances),
                    "holder_aggregation_available": False,
                    "holder_count_available": False,
                    "scope": "largest_token_accounts_not_unique_owners",
                    "supply_raw": str(raw_supply),
                    "decimals": decimals,
                    "snapshot_slots": {
                        "mint": mint_slot, "supply": supply_slot, "largest_accounts": largest_slot,
                    },
                },
                **common,
            ),
        ]

    async def verify_pool_spec(self, spec: SolanaPoolSpec) -> VerifiedSolanaPool:
        pool, token_vault, quote_vault = await asyncio.gather(
            self.get_account_info_snapshot(spec.pool_id),
            self.get_account_info_snapshot(spec.token_vault),
            self.get_account_info_snapshot(spec.quote_vault),
        )
        pool_slot, pool_account = pool
        if not isinstance(pool_account, dict) or str(pool_account.get("owner")) != spec.program_id:
            raise SolanaRPCError("configured pool is not owned by its configured DEX program")
        for label, (slot, account), expected_mint, decimals in (
            ("token", token_vault, spec.token_mint, spec.token_decimals),
            ("quote", quote_vault, spec.quote_mint, spec.quote_decimals),
        ):
            if not isinstance(account, dict) or str(account.get("owner")) not in TOKEN_PROGRAM_IDS:
                raise SolanaRPCError(f"configured {label} vault is not an SPL token account")
            parsed = account.get("data", {}).get("parsed", {})
            info = parsed.get("info", {}) if parsed.get("type") == "account" else {}
            token_amount = info.get("tokenAmount", {}) if isinstance(info, dict) else {}
            if str(info.get("mint")) != expected_mint or int(token_amount.get("decimals", -1)) != decimals:
                raise SolanaRPCError(f"configured {label} vault mint/decimals mismatch")
            if slot <= 0:
                raise SolanaRPCError(f"configured {label} vault has no verified context slot")
        return VerifiedSolanaPool(spec=spec, observed_slot=max(pool_slot, token_vault[0], quote_vault[0]))

    @staticmethod
    def transaction_mentions_pool(verified: VerifiedSolanaTransaction, spec: SolanaPoolSpec) -> bool:
        message = verified.transaction.get("transaction", {}).get("message", {})
        keys = SolanaRPCAdapter._account_keys(message, verified.transaction.get("meta", {}))
        programs = {
            str(item.get("programId")) for _, item in SolanaRPCAdapter._ordered_instructions(
                verified.transaction, message
            ) if item.get("programId")
        }
        return (
            spec.program_id in programs
            and {spec.pool_id, spec.token_vault, spec.quote_vault}.issubset(set(keys))
        )

    def decode_configured_pool_events(
        self,
        verified: VerifiedSolanaTransaction,
        pool: VerifiedSolanaPool,
        received_at_ms: int | None = None,
    ) -> list[OnChainEvent]:
        """Decode exact configured vault deltas; no protocol-independent claims are inferred."""
        spec = pool.spec
        if not spec.quote_is_usd:
            raise SolanaRPCError("configured pool quote reserve cannot be labelled USD")
        if not self.transaction_mentions_pool(verified, spec):
            return []
        result = verified.transaction
        message = result.get("transaction", {}).get("message", {})
        meta = result.get("meta", {})
        keys = self._account_keys(message, meta)
        token_index = keys.index(spec.token_vault)
        quote_index = keys.index(spec.quote_vault)
        token_pre, token_post = self._token_balance_pair(meta, token_index, spec.token_mint, spec.token_decimals)
        quote_pre, quote_post = self._token_balance_pair(meta, quote_index, spec.quote_mint, spec.quote_decimals)
        received = received_at_ms or int(time.time() * 1000)
        transaction_index = int(meta.get("transactionIndex", 0) or 0)
        quote_post_usd = quote_post / (10 ** spec.quote_decimals)
        pool_position = 1_000_000_000 + int.from_bytes(
            hashlib.sha256(spec.pool_id.encode("utf-8")).digest()[:4], "big"
        ) * 10
        common = {
            "provider_id": self.provider_id,
            "chain_id": ChainId.SOLANA,
            "contract_address": spec.token_mint,
            "block_number": verified.slot,
            "block_hash": verified.blockhash,
            "parent_hash": verified.previous_blockhash,
            "transaction_id": verified.signature,
            "transaction_index": transaction_index,
            "exchange_timestamp_ms": verified.block_time_ms,
            "received_at_ms": received,
            "chain_verified": True,
            "confirmations": 1,
        }
        events = [OnChainEvent.build(
            event_id=f"{self.provider_id}:{verified.signature}:{spec.pool_id}:liquidity-snapshot",
            event_type=OnChainEventType.LIQUIDITY_SNAPSHOT,
            log_index=pool_position,
            payload={
                "pool_id": spec.pool_id,
                "pool_provider": self.provider_id,
                "pool_program_id": spec.program_id,
                "pool_verified": True,
                # A vault reserve is observable liquidity, but not fully realizable exit value.
                "liquidity_usd": quote_post_usd,
                "quote_reserve_raw": str(quote_post),
                "quote_reserve_usd": quote_post_usd,
                "quote_mint": spec.quote_mint,
                "valuation_semantics": "verified_usd_quote_vault_reserve_not_tvl_or_exit_value",
                "sellable_capacity_source": "separate_fresh_round_trip_quotes",
            },
            **common,
        )]
        token_delta = token_post - token_pre
        quote_delta = quote_post - quote_pre
        if token_delta and quote_delta and (token_delta > 0) != (quote_delta > 0):
            side = "sell" if token_delta > 0 else "buy"
            events.append(OnChainEvent.build(
                event_id=f"{self.provider_id}:{verified.signature}:{spec.pool_id}:trade",
                event_type=OnChainEventType.TRADE,
                log_index=pool_position + 1,
                payload={
                    "side": side,
                    "wallet": self._first_signer(message.get("accountKeys", [])),
                    "counterparty": spec.pool_id,
                    "volume_usd": abs(quote_delta) / (10 ** spec.quote_decimals),
                    "token_amount_raw": str(abs(token_delta)),
                    "quote_amount_raw": str(abs(quote_delta)),
                    "pool_id": spec.pool_id,
                    "wash_attribution_available": False,
                },
                **common,
            ))
        return events

    @staticmethod
    def _account_keys(message: dict[str, Any], meta: dict[str, Any]) -> list[str]:
        keys = [
            str(value.get("pubkey")) if isinstance(value, dict) else str(value)
            for value in message.get("accountKeys", [])
        ]
        loaded = meta.get("loadedAddresses", {}) if isinstance(meta, dict) else {}
        keys.extend(str(value) for value in loaded.get("writable", []))
        keys.extend(str(value) for value in loaded.get("readonly", []))
        return keys

    @staticmethod
    def _token_balance_pair(
        meta: dict[str, Any], account_index: int, mint: str, decimals: int
    ) -> tuple[int, int]:
        def amount(values: list[Any]) -> int:
            matches = [
                value for value in values if isinstance(value, dict)
                and int(value.get("accountIndex", -1)) == account_index
                and str(value.get("mint")) == mint
            ]
            if len(matches) != 1:
                raise SolanaRPCError("configured vault balance is missing or ambiguous")
            token_amount = matches[0].get("uiTokenAmount", {})
            if int(token_amount.get("decimals", -1)) != decimals:
                raise SolanaRPCError("configured vault balance decimals mismatch")
            return int(str(token_amount.get("amount", "0")))

        return amount(meta.get("preTokenBalances", [])), amount(meta.get("postTokenBalances", []))

    @staticmethod
    def _ordered_instructions(
        result: dict[str, Any], message: dict[str, Any]
    ) -> list[tuple[int, dict[str, Any]]]:
        top = [value for value in message.get("instructions", []) if isinstance(value, dict)]
        ordered: list[tuple[int, dict[str, Any]]] = list(enumerate(top))
        ordinal = len(ordered)
        meta = result.get("meta", {}) if isinstance(result, dict) else {}
        inner_groups = meta.get("innerInstructions", []) if isinstance(meta, dict) else []
        for group in sorted(
            (value for value in inner_groups if isinstance(value, dict)),
            key=lambda value: int(value.get("index", 0)),
        ):
            for instruction in group.get("instructions", []):
                if isinstance(instruction, dict):
                    ordered.append((ordinal, instruction))
                    ordinal += 1
        return ordered

    @staticmethod
    def _first_signer(account_keys: list[Any]) -> str | None:
        for item in account_keys:
            if isinstance(item, dict) and item.get("signer"):
                return str(item.get("pubkey"))
        return None
