from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
import time
from typing import Any

import aiohttp

from .types import OnChainEvent, OnChainEventType


class JupiterQuoteError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JupiterOrderQuote:
    input_mint: str
    output_mint: str
    input_amount: int
    output_amount: int
    price_impact_percent: float
    raw: dict[str, Any]


class JupiterQuoteAdapter:
    """Jupiter Swap V2 quote-only client. It never sends a taker or calls execute."""

    def __init__(self, provider_id: str, config: dict[str, Any]):
        self.provider_id = provider_id
        self.url = str(config["url"])
        self.timeout = float(config.get("request_timeout_seconds", 8))
        if not 0 < self.timeout <= 10:
            raise ValueError("Jupiter HTTP timeout must be in (0,10]")
        self.api_key_env = str(config.get("api_key_env", "JUPITER_API_KEY"))
        self.min_interval = max(0.0, float(config.get("min_interval_seconds", 2.1)))
        self._lock = asyncio.Lock()
        self._last_request_monotonic = 0.0

    async def order(self, input_mint: str, output_mint: str, amount: int) -> JupiterOrderQuote:
        if amount <= 0:
            raise ValueError("quote amount must be positive")
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last_request_monotonic)
            if wait > 0:
                await asyncio.sleep(wait)
            headers = {"User-Agent": "crypto-intelligence-onchain/1.0"}
            if api_key := os.getenv(self.api_key_env):
                headers["x-api-key"] = api_key
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
            }
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(self.url, params=params) as response:
                    response.raise_for_status()
                    body = await response.json()
            self._last_request_monotonic = time.monotonic()
        return self.parse_order(body, input_mint, output_mint, amount)

    @staticmethod
    def parse_order(
        body: dict[str, Any], input_mint: str, output_mint: str, amount: int
    ) -> JupiterOrderQuote:
        if not isinstance(body, dict) or body.get("error"):
            raise JupiterQuoteError(f"Jupiter quote failed: {body.get('error') if isinstance(body, dict) else 'invalid response'}")
        if body.get("transaction") not in (None, ""):
            raise JupiterQuoteError("quote-only response unexpectedly contains a transaction")
        actual_input = str(body.get("inputMint") or body.get("input_mint") or "")
        actual_output = str(body.get("outputMint") or body.get("output_mint") or "")
        input_amount = int(str(body.get("inAmount") or body.get("inputAmount") or "0"))
        output_amount = int(str(body.get("outAmount") or body.get("outputAmount") or "0"))
        if actual_input != input_mint or actual_output != output_mint:
            raise JupiterQuoteError("Jupiter quote mint mismatch")
        if input_amount != amount or output_amount <= 0:
            raise JupiterQuoteError("Jupiter quote amount mismatch or zero output")
        impact = float(body.get("priceImpactPct", body.get("priceImpact", 0)) or 0)
        if impact < 0:
            raise JupiterQuoteError("Jupiter quote price impact cannot be negative")
        return JupiterOrderQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            input_amount=input_amount,
            output_amount=output_amount,
            price_impact_percent=impact,
            raw=body,
        )

    async def paper_exit_events(
        self,
        origin: OnChainEvent,
        *,
        token_mint: str,
        stable_mint: str,
        stable_decimals: int,
        notionals_usd: list[float],
        received_at_ms: int | None = None,
    ) -> list[OnChainEvent]:
        """Round-trip stable→token→stable quotes model a feasible paper entry and exit."""
        events: list[OnChainEvent] = []
        quote_position = 50_000_000_000 + int.from_bytes(
            hashlib.sha256(origin.event_id.encode("utf-8")).digest()[:4], "big"
        ) * 100
        for ordinal, notional in enumerate(notionals_usd):
            input_raw = int(round(float(notional) * (10 ** stable_decimals)))
            buy = await self.order(stable_mint, token_mint, input_raw)
            sell = await self.order(token_mint, stable_mint, buy.output_amount)
            output_usd = sell.output_amount / (10 ** stable_decimals)
            slippage_bps = max(0.0, (1.0 - output_usd / float(notional)) * 10_000)
            price_impact_bps = (buy.price_impact_percent + sell.price_impact_percent) * 100
            received = received_at_ms or int(time.time() * 1000)
            events.append(OnChainEvent.build(
                event_id=f"{self.provider_id}:{origin.event_id}:{input_raw}:round-trip",
                provider_id=self.provider_id,
                chain_id=origin.identity.chain_id,
                contract_address=token_mint,
                event_type=OnChainEventType.PAPER_EXIT_QUOTE,
                block_number=origin.block_number,
                block_hash=origin.block_hash,
                parent_hash=origin.parent_hash,
                transaction_id=origin.transaction_id,
                transaction_index=origin.transaction_index,
                log_index=quote_position + ordinal,
                exchange_timestamp_ms=origin.exchange_timestamp_ms,
                received_at_ms=received,
                payload={
                    "notional_usd": float(notional),
                    "expected_output_usd": output_usd,
                    "slippage_bps": slippage_bps,
                    "price_impact_bps": price_impact_bps,
                    "fees_usd": 0.0,
                    "provider": self.provider_id,
                    "quote_only": True,
                    "transaction_present": False,
                    "buy_input_raw": str(buy.input_amount),
                    "buy_output_raw": str(buy.output_amount),
                    "sell_output_raw": str(sell.output_amount),
                },
                chain_verified=False,
                confirmations=origin.confirmations,
            ))
        return events
