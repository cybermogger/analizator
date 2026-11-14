from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
import time
from typing import Any, Awaitable, Callable

import structlog
from src.util.redaction import safe_exception

from src.onchain.base import BaseRPCAdapter
from src.onchain.capabilities import provider_capabilities, unavailable_names
from src.onchain.config import required_quote_notionals
from src.onchain.jupiter import JupiterQuoteAdapter
from src.onchain.pipeline import OnChainCandidatePipeline
from src.onchain.repository import OnChainRepository
from src.onchain.solana import SolanaPoolSpec, SolanaRPCAdapter, VerifiedSolanaPool
from src.onchain.types import OnChainEvent, OnChainEventType


@dataclass(slots=True)
class ProviderRuntime:
    chain_id: str
    enabled: bool
    status: str = "disabled"
    last_received_at_ms: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)


class OnChainIntelligenceService:
    """Failure-isolated, read-only on-chain evidence ingestion and candidate ranking."""

    def __init__(self, config: dict[str, Any], storage: Any, event_repository: Any | None = None):
        self.config = dict(config.get("onchain", {}))
        self.enabled = bool(self.config.get("enabled", False))
        self.log = structlog.get_logger("onchain_intelligence")
        self.repository = OnChainRepository(storage)
        provider_settings = dict(self.config.get("providers", {}))
        self.repository.sync_providers(provider_settings)
        self.pipeline = OnChainCandidatePipeline(
            dict(self.config.get("risk_filter", {})), repository=self.repository
        )
        self.event_repository = event_repository
        self.providers: dict[str, Any] = {}
        self.runtime: dict[str, ProviderRuntime] = {}
        for provider_id, settings in provider_settings.items():
            enabled = bool(settings.get("enabled", False))
            capabilities = provider_capabilities(
                str(settings.get("adapter", "")), dict(settings), self.config
            )
            self.runtime[provider_id] = ProviderRuntime(
                chain_id=str(settings["chain_id"]), enabled=enabled,
                status="starting" if enabled else "disabled",
                capabilities={name: value.as_dict() for name, value in capabilities.items()},
            )
            adapter = str(settings.get("adapter", ""))
            if adapter == "solana_rpc":
                self.providers[provider_id] = SolanaRPCAdapter(provider_id, settings)
            elif adapter == "base_rpc":
                self.providers[provider_id] = BaseRPCAdapter(provider_id, settings)
            else:
                raise ValueError(f"unsupported on-chain adapter: {adapter}")
        self._settings = provider_settings
        self._pool_specs: dict[str, tuple[SolanaPoolSpec, ...]] = {
            provider_id: tuple(
                SolanaPoolSpec.from_config(dict(value))
                for value in settings.get("configured_pools", [])
            )
            for provider_id, settings in provider_settings.items()
            if settings.get("adapter") == "solana_rpc"
        }
        self._verified_pools: dict[tuple[str, str], VerifiedSolanaPool] = {}
        self._solana_processed: dict[str, set[str]] = {
            provider_id: set() for provider_id in self._pool_specs
        }
        self._solana_inflight: set[tuple[str, str]] = set()
        self._solana_lock = asyncio.Lock()
        quote_config = dict(self.config.get("enrichment", {}).get("jupiter_quote", {}))
        self.quote_adapter = (
            JupiterQuoteAdapter("jupiter_quote", quote_config)
            if quote_config.get("enabled", False) else None
        )
        self._quote_config = quote_config
        self._quote_health: dict[str, Any] = {
            "chain_id": "solana", "enabled": self.quote_adapter is not None,
            "status": "starting" if self.quote_adapter is not None else "disabled",
            "last_received_at_ms": None, "last_error": None,
            "capabilities": {
                "paper_exit_quote": {
                    "status": "AVAILABLE" if self.quote_adapter is not None else "NOT_AVAILABLE",
                    "reason": "Jupiter Swap V2 quote-only; no taker and no execute"
                    if self.quote_adapter is not None else "quote-only enrichment is disabled",
                    "condition": "verified configured Solana pool",
                }
            },
        }
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        for provider_id in self.runtime:
            self._persist_health(provider_id)

    def ingest(self, event: OnChainEvent) -> Any:
        assessment = self.pipeline.ingest(event)
        runtime = self.runtime.get(event.provider_id)
        if runtime is not None:
            runtime.status = "healthy"
            runtime.last_received_at_ms = max(runtime.last_received_at_ms or 0, event.received_at_ms)
            runtime.consecutive_failures = 0
            runtime.last_error = None
            self._persist_health(event.provider_id)
        return assessment

    async def _solana_signature(self, provider_id: str, signature: str, _slot: int) -> None:
        key = (provider_id, signature)
        async with self._solana_lock:
            if signature in self._solana_processed.setdefault(provider_id, set()) or key in self._solana_inflight:
                return
            self._solana_inflight.add(key)
        try:
            adapter: SolanaRPCAdapter = self.providers[provider_id]
            verified = await adapter.fetch_verified_transaction(signature)
            received_at_ms = int(time.time() * 1000)
            for event in adapter.decode_token_creation(verified, received_at_ms):
                self.ingest(event)
                self.ingest(self._capability_event(event, provider_id))
                for evidence in await adapter.produce_mint_evidence(event, received_at_ms):
                    self.ingest(evidence)

            for spec in self._pool_specs.get(provider_id, ()):
                if not adapter.transaction_mentions_pool(verified, spec):
                    continue
                pool_key = (provider_id, spec.pool_id)
                pool = self._verified_pools.get(pool_key)
                if pool is None:
                    pool = await adapter.verify_pool_spec(spec)
                    self._verified_pools[pool_key] = pool
                pool_events = adapter.decode_configured_pool_events(verified, pool, received_at_ms)
                for pool_event in pool_events:
                    self.ingest(pool_event)
                snapshot = next(
                    (item for item in pool_events if item.event_type is OnChainEventType.LIQUIDITY_SNAPSHOT),
                    None,
                )
                if snapshot is not None:
                    await self._produce_paper_quotes(snapshot, spec)
            async with self._solana_lock:
                processed = self._solana_processed[provider_id]
                processed.add(signature)
                if len(processed) > 10_000:
                    processed.clear()
                    processed.add(signature)
        finally:
            async with self._solana_lock:
                self._solana_inflight.discard(key)

    def _capability_event(self, origin: OnChainEvent, provider_id: str) -> OnChainEvent:
        settings = dict(self._settings[provider_id])
        capabilities = provider_capabilities(
            str(settings.get("adapter", "")), settings, self.config
        )
        unavailable = unavailable_names(capabilities)
        return OnChainEvent.build(
            event_id=f"{origin.event_id}:provider-capabilities",
            provider_id=provider_id,
            chain_id=origin.identity.chain_id,
            contract_address=origin.identity.contract_address,
            event_type=OnChainEventType.CAPABILITY_STATE,
            block_number=origin.block_number,
            block_hash=origin.block_hash,
            parent_hash=origin.parent_hash,
            transaction_id=origin.transaction_id,
            transaction_index=origin.transaction_index,
            log_index=950_000,
            exchange_timestamp_ms=origin.exchange_timestamp_ms,
            received_at_ms=origin.received_at_ms,
            payload={
                "provider_id": provider_id,
                "capabilities": {name: value.as_dict() for name, value in capabilities.items()},
                "not_available": list(unavailable),
                # Unsupported evidence must never silently pass a strict candidate.
                "eligibility_required_not_available": list(unavailable),
            },
            chain_verified=False,
            confirmations=origin.confirmations,
        )

    async def _produce_paper_quotes(
        self, snapshot: OnChainEvent, spec: SolanaPoolSpec
    ) -> None:
        if self.quote_adapter is None:
            return
        try:
            events = await self.quote_adapter.paper_exit_events(
                snapshot,
                token_mint=spec.token_mint,
                stable_mint=str(self._quote_config["stable_mint"]),
                stable_decimals=int(self._quote_config.get("stable_decimals", 6)),
                notionals_usd=required_quote_notionals(self.config),
            )
            for event in events:
                self.ingest(event)
            self._quote_health.update({
                "status": "healthy", "last_received_at_ms": int(time.time() * 1000),
                "last_error": None,
            })
        except Exception as exc:
            self._quote_health.update({
                "status": "degraded", "last_error": safe_exception(exc),
            })
            self.log.warning("jupiter_quote_failed", contract=spec.token_mint, error=safe_exception(exc))

    async def _base_log(self, provider_id: str, log: dict[str, Any]) -> None:
        transaction_hash = str(log.get("transactionHash") or "")
        if not transaction_hash:
            return
        adapter = self.providers[provider_id]
        verified = await adapter.fetch_verified_receipt(transaction_hash)
        event = adapter.decode_contract_creation(verified, received_at_ms=int(time.time() * 1000))
        if event is not None:
            self.ingest(event)
            self.ingest(self._capability_event(event, provider_id))
            try:
                is_proxy, implementation = await adapter.inspect_proxy(event.identity.contract_address)
            except Exception as exc:
                self.log.warning(
                    "base_proxy_inspection_failed", contract=event.identity.contract_address,
                    error=safe_exception(exc),
                )
            else:
                self.ingest(OnChainEvent.build(
                    event_id=f"{event.event_id}:proxy-state",
                    provider_id=provider_id,
                    chain_id=event.identity.chain_id,
                    contract_address=event.identity.contract_address,
                    event_type=OnChainEventType.AUTHORITY_STATE,
                    block_number=event.block_number,
                    block_hash=event.block_hash,
                    parent_hash=event.parent_hash,
                    transaction_id=event.transaction_id,
                    transaction_index=event.transaction_index,
                    log_index=event.log_index + 1,
                    exchange_timestamp_ms=event.exchange_timestamp_ms,
                    received_at_ms=int(time.time() * 1000),
                    payload={
                        "upgradeable_proxy": is_proxy,
                        "proxy_implementation": implementation,
                        "owner": None,
                        "update_authority": "unknown" if is_proxy else None,
                    },
                    chain_verified=True,
                    confirmations=event.confirmations,
                ))

    async def _solana_worker(self, provider_id: str, program_id: str) -> None:
        adapter: SolanaRPCAdapter = self.providers[provider_id]
        await adapter.stream_logs(
            program_id,
            lambda signature, slot: self._solana_signature(provider_id, signature, slot),
            self._stop,
            lambda status, error: self._provider_status(provider_id, status, error),
        )

    async def _base_worker(self, provider_id: str) -> None:
        adapter: BaseRPCAdapter = self.providers[provider_id]
        settings = self._settings[provider_id]
        addresses = [str(value).lower() for value in settings.get("watched_addresses", [])]
        topics = [str(value) for value in settings.get("topics", [])]
        poll_seconds = max(1.0, float(settings.get("poll_interval_seconds", 4)))
        receipt_limit = max(1, min(int(settings.get("max_receipts_per_block", 20)), 100))
        last_block_hash: str | None = None
        while not self._stop.is_set():
            try:
                chain_id = await adapter.rpc("eth_chainId", [])
                if int(str(chain_id), 16) != 8453:
                    raise ValueError("RPC endpoint is not Base mainnet")
                block = await adapter.rpc("eth_getBlockByNumber", ["latest", True])
                if not isinstance(block, dict) or not block.get("hash"):
                    raise ValueError("Base latest block response is invalid")
                block_hash = str(block["hash"])
                if block_hash != last_block_hash:
                    creations = [
                        value for value in block.get("transactions", [])
                        if isinstance(value, dict) and value.get("to") is None and value.get("hash")
                    ][:receipt_limit]
                    for transaction in creations:
                        await self._base_log(provider_id, {
                            "transactionHash": transaction["hash"], "blockHash": block_hash,
                        })
                    if addresses:
                        values = await adapter.rpc("eth_getLogs", [{
                            "fromBlock": str(block.get("number") or "latest"),
                            "toBlock": str(block.get("number") or "latest"),
                            "address": addresses, "topics": topics,
                        }])
                        for value in values if isinstance(values, list) else []:
                            if isinstance(value, dict) and not value.get("removed", False):
                                await self._base_log(provider_id, value)
                    last_block_hash = block_hash
                await self._provider_status(provider_id, "healthy", None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._provider_status(provider_id, "degraded", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass

    async def _provider_status(
        self, provider_id: str, status: str, error: Exception | None
    ) -> None:
        runtime = self.runtime[provider_id]
        runtime.status = status
        if error is None:
            runtime.consecutive_failures = 0
            runtime.last_error = None
        else:
            runtime.consecutive_failures += 1
            runtime.last_error = safe_exception(error)
        self._persist_health(provider_id)

    async def _run_isolated(
        self, provider_id: str, worker: Callable[[], Awaitable[None]]
    ) -> None:
        runtime = self.runtime[provider_id]
        try:
            self._persist_health(provider_id)
            await worker()
            if runtime.status != "degraded":
                runtime.status = "healthy"
                runtime.consecutive_failures = 0
                runtime.last_error = None
                self._persist_health(provider_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.status = "degraded"
            runtime.consecutive_failures += 1
            runtime.last_error = safe_exception(exc)
            self._persist_health(provider_id)
            self.log.warning("onchain_provider_failed", provider=provider_id, error=safe_exception(exc))

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        self.bridge_official_events()
        for provider_id, adapter in self.providers.items():
            if not self.runtime[provider_id].enabled:
                continue
            if isinstance(adapter, SolanaRPCAdapter):
                program_ids = {
                    str(value) for value in self._settings[provider_id].get("program_ids", [])
                }
                program_ids.update(
                    spec.program_id for spec in self._pool_specs.get(provider_id, ())
                )
                for program_id in sorted(program_ids):
                    self._tasks.append(asyncio.create_task(
                        self._run_isolated(
                            provider_id,
                            lambda pid=provider_id, program=str(program_id): self._solana_worker(pid, program),
                        ),
                        name=f"onchain:{provider_id}:{program_id}",
                    ))
            elif isinstance(adapter, BaseRPCAdapter):
                self._tasks.append(asyncio.create_task(
                    self._run_isolated(provider_id, lambda pid=provider_id: self._base_worker(pid)),
                    name=f"onchain:{provider_id}",
                ))
        try:
            while not self._stop.is_set():
                self.bridge_official_events()
                for provider_id in self.runtime:
                    self._persist_health(provider_id)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                except TimeoutError:
                    pass
        finally:
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._tasks.clear()

    def bridge_official_events(self) -> int:
        """Import only exact chain+contract Phase-2 links; ticker-only links are ignored."""
        if self.event_repository is None:
            return 0
        created = 0
        for normalized in self.event_repository.latest_events(100):
            if bool(normalized.get("authenticity_risk", False)):
                continue
            if str(normalized.get("contradiction_state", "none")).lower() not in {"none", "resolved"}:
                continue
            if not self._event_has_official_source(normalized):
                continue
            for link in normalized.get("impacted_assets", []):
                chain_id = link.get("chain_id")
                contract = link.get("contract_address")
                if not chain_id or not contract:
                    continue
                if str(chain_id) not in {runtime.chain_id for runtime in self.runtime.values()}:
                    continue
                received_at = int(normalized["first_received_at_ms"])
                published_at = int(normalized["first_published_at_ms"])
                event_id = f"phase2:{normalized['event_id']}:{chain_id}:{contract}"
                digest = hashlib.sha256(event_id.encode("utf-8")).digest()
                event = OnChainEvent.build(
                    event_id=event_id,
                    provider_id="phase2-event-intelligence",
                    chain_id=str(chain_id),
                    contract_address=str(contract),
                    event_type=OnChainEventType.OFFICIAL_IDENTITY,
                    block_number=0,
                    block_hash=f"offchain:{normalized['event_id']}",
                    transaction_id=str(normalized["event_id"]),
                    transaction_index=int.from_bytes(digest[:2], "big"),
                    log_index=int.from_bytes(digest[2:4], "big"),
                    exchange_timestamp_ms=published_at,
                    received_at_ms=received_at,
                    payload={
                        "exact_chain_contract": True,
                        "credibility": float(normalized["credibility"]),
                        "source_policy_verified": True,
                        "published_at_ms": published_at,
                        "source_ids": normalized.get("source_ids", []),
                        "evidence_urls": normalized.get("evidence_urls", []),
                    },
                    chain_verified=False,
                )
                before = self.repository.storage.query(
                    "SELECT 1 FROM onchain_events WHERE event_id=?", (event_id,)
                )
                self.pipeline.ingest(event)
                created += int(not before)
        return created

    def _event_has_official_source(self, normalized: dict[str, Any]) -> bool:
        if bool(normalized.get("official_source_confirmed", False)):
            return True
        source_ids = [str(value) for value in normalized.get("source_ids", []) if value]
        storage = getattr(self.event_repository, "storage", None)
        if storage is None or not source_ids:
            return False
        marks = ",".join("?" for _ in source_ids)
        rows = storage.query(
            f"SELECT 1 FROM event_sources WHERE official=1 AND source_id IN ({marks}) LIMIT 1",
            source_ids,
        )
        return bool(rows)

    async def stop(self) -> None:
        self._stop.set()

    def _persist_health(self, provider_id: str) -> None:
        runtime = self.runtime[provider_id]
        self.repository.save_provider_health(
            provider_id, runtime.chain_id, runtime.status, int(time.time() * 1000),
            runtime.last_received_at_ms, runtime.consecutive_failures, runtime.last_error,
        )

    def health(self) -> dict[str, Any]:
        now = int(time.time() * 1000)
        result: dict[str, Any] = {}
        for provider_id, runtime in self.runtime.items():
            stale_ms = int(float(self._settings[provider_id].get("stale_after_seconds", 120)) * 1000)
            lag = max(0, now - runtime.last_received_at_ms) if runtime.last_received_at_ms else None
            status = runtime.status
            if status == "healthy" and lag is not None and lag > stale_ms:
                status = "stale"
            result[provider_id] = {
                "chain_id": runtime.chain_id,
                "enabled": runtime.enabled,
                "status": status,
                "last_received_at_ms": runtime.last_received_at_ms,
                "lag_ms": lag,
                "consecutive_failures": runtime.consecutive_failures,
                "last_error": runtime.last_error,
                "capabilities": runtime.capabilities,
                "not_available": sorted(
                    name for name, value in runtime.capabilities.items()
                    if value.get("status") == "NOT_AVAILABLE"
                ),
            }
        if self.quote_adapter is not None:
            result["jupiter_quote"] = dict(self._quote_health)
        return result

    def latest(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.repository.latest_assessments(limit)
