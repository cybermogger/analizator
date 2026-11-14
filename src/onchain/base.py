from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import os
import time
from typing import Any

import aiohttp
import websockets

from .types import ChainId, OnChainEvent, OnChainEventType


BASE_CHAIN_ID = 8453
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
READ_ONLY_METHODS = {
    "eth_call",
    "eth_chainId",
    "eth_getBlockByHash",
    "eth_getBlockByNumber",
    "eth_getCode",
    "eth_getLogs",
    "eth_getStorageAt",
    "eth_getTransactionReceipt",
}


class BaseRPCError(RuntimeError):
    pass


def _hex_int(value: Any, field: str) -> int:
    try:
        return int(str(value), 16)
    except (TypeError, ValueError) as exc:
        raise BaseRPCError(f"invalid {field}") from exc


@dataclass(frozen=True, slots=True)
class VerifiedBaseReceipt:
    transaction_hash: str
    block_number: int
    block_hash: str
    parent_hash: str
    block_time_ms: int
    transaction_index: int
    contract_address: str | None
    sender: str
    receipt: dict[str, Any]


class BaseRPCAdapter:
    """Direct read-only Base JSON-RPC adapter; never uses the sequencer submit endpoint."""

    def __init__(self, provider_id: str, config: dict[str, Any]):
        self.provider_id = provider_id
        self.config = config
        self.rpc_url = str(config["rpc_url"])
        self.websocket_url = str(config.get("websocket_url", ""))
        self.timeout = float(config.get("request_timeout_seconds", 8))
        if not 0 < self.timeout <= 10:
            raise ValueError("Base HTTP timeout must be in (0,10]")
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
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout), headers=self._headers()
        ) as session:
            async with session.post(self.rpc_url, json=payload) as response:
                response.raise_for_status()
                body = await response.json()
        if body.get("error"):
            raise BaseRPCError(f"{method}: {body['error']}")
        if "result" not in body:
            raise BaseRPCError(f"{method}: missing result")
        return body["result"]

    async def fetch_verified_receipt(self, transaction_hash: str) -> VerifiedBaseReceipt:
        chain_id, receipt = await asyncio.gather(
            self.rpc("eth_chainId", []),
            self.rpc("eth_getTransactionReceipt", [transaction_hash]),
        )
        if _hex_int(chain_id, "chainId") != BASE_CHAIN_ID:
            raise BaseRPCError("RPC endpoint is not Base mainnet")
        if not isinstance(receipt, dict):
            raise BaseRPCError("transaction receipt is missing")
        if str(receipt.get("transactionHash", "")).lower() != transaction_hash.lower():
            raise BaseRPCError("transaction receipt hash mismatch")
        if receipt.get("status") != "0x1":
            raise BaseRPCError("transaction reverted or status missing")
        block_hash = str(receipt.get("blockHash") or "")
        block = await self.rpc("eth_getBlockByHash", [block_hash, False])
        if not isinstance(block, dict) or str(block.get("hash", "")).lower() != block_hash.lower():
            raise BaseRPCError("canonical block mismatch")
        return self.parse_verified_receipt(receipt, block)

    @staticmethod
    def parse_verified_receipt(receipt: dict[str, Any], block: dict[str, Any]) -> VerifiedBaseReceipt:
        transaction_hash = str(receipt.get("transactionHash") or "")
        block_hash = str(receipt.get("blockHash") or "")
        if not transaction_hash or not block_hash or receipt.get("status") != "0x1":
            raise BaseRPCError("invalid successful receipt")
        if str(block.get("hash") or "").lower() != block_hash.lower():
            raise BaseRPCError("receipt/block hash mismatch")
        sender = str(receipt.get("from") or "").lower()
        if not sender:
            raise BaseRPCError("receipt sender missing")
        contract = receipt.get("contractAddress")
        return VerifiedBaseReceipt(
            transaction_hash=transaction_hash,
            block_number=_hex_int(receipt.get("blockNumber"), "blockNumber"),
            block_hash=block_hash,
            parent_hash=str(block.get("parentHash") or ""),
            block_time_ms=_hex_int(block.get("timestamp"), "timestamp") * 1000,
            transaction_index=_hex_int(receipt.get("transactionIndex"), "transactionIndex"),
            contract_address=str(contract).lower() if contract else None,
            sender=sender,
            receipt=receipt,
        )

    async def inspect_proxy(self, contract_address: str) -> tuple[bool, str | None]:
        code, storage = await asyncio.gather(
            self.rpc("eth_getCode", [contract_address, "latest"]),
            self.rpc("eth_getStorageAt", [contract_address, EIP1967_IMPLEMENTATION_SLOT, "latest"]),
        )
        if not isinstance(code, str) or code in {"0x", "0x0"}:
            raise BaseRPCError("contract has no bytecode")
        implementation = self.parse_implementation_slot(storage)
        return implementation is not None, implementation

    @staticmethod
    def parse_implementation_slot(value: Any) -> str | None:
        text = str(value or "").lower()
        if not text.startswith("0x") or len(text) != 66:
            raise BaseRPCError("invalid EIP-1967 storage response")
        address = "0x" + text[-40:]
        return None if int(address, 16) == 0 else address

    def decode_contract_creation(
        self,
        verified: VerifiedBaseReceipt,
        *,
        symbol: str | None = None,
        name: str | None = None,
        received_at_ms: int | None = None,
    ) -> OnChainEvent | None:
        if verified.contract_address is None:
            return None
        received = received_at_ms or int(time.time() * 1000)
        return OnChainEvent.build(
            event_id=f"{self.provider_id}:{verified.transaction_hash}:contract",
            provider_id=self.provider_id,
            chain_id=ChainId.BASE,
            contract_address=verified.contract_address,
            event_type=OnChainEventType.TOKEN_CREATED,
            block_number=verified.block_number,
            block_hash=verified.block_hash,
            parent_hash=verified.parent_hash,
            transaction_id=verified.transaction_hash,
            transaction_index=verified.transaction_index,
            log_index=0,
            exchange_timestamp_ms=verified.block_time_ms,
            received_at_ms=received,
            payload={"symbol": symbol, "name": name, "deployer": verified.sender},
            chain_verified=True,
            confirmations=1,
        )

    async def stream_logs(
        self,
        addresses: list[str],
        topics: list[str],
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        stop: asyncio.Event,
        status: Callable[[str, Exception | None], Awaitable[None]] | None = None,
    ) -> None:
        if not self.websocket_url:
            raise BaseRPCError("Base WebSocket URL is not configured")
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
            "params": ["logs", {"address": addresses, "topics": topics}],
        }
        while not stop.is_set():
            try:
                async with websockets.connect(
                    self.websocket_url, open_timeout=self.connect_timeout,
                    close_timeout=self.connect_timeout, ping_interval=20, ping_timeout=10,
                    additional_headers=self._headers(),
                ) as socket:
                    await socket.send(json.dumps(request, separators=(",", ":")))
                    acknowledgement = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.read_timeout))
                    if "result" not in acknowledgement:
                        raise BaseRPCError(f"eth_subscribe rejected: {acknowledgement.get('error')}")
                    if status is not None:
                        await status("healthy", None)
                    while not stop.is_set():
                        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=self.read_timeout))
                        if message.get("method") != "eth_subscription":
                            continue
                        value = message.get("params", {}).get("result")
                        if isinstance(value, dict) and not value.get("removed", False):
                            await emit(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if status is not None:
                    await status("degraded", exc)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2.0)
                except TimeoutError:
                    pass
