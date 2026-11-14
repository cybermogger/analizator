"""Exact-contract reports for strictly eligible new on-chain candidates."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from .multi_asset import SendOnce


class NewTokenAlertService:
    def __init__(self, storage: Any, settings: dict[str, Any], send_once: SendOnce) -> None:
        self.storage = storage
        self.send_once = send_once
        self.enabled = bool(settings.get("enabled", False))
        self.max_per_day = max(0, min(30, int(settings.get("max_per_day", 6))))
        self._stop = asyncio.Event()
        storage.execute("""CREATE TABLE IF NOT EXISTS new_token_alert_attempts(
            identity_key TEXT PRIMARY KEY, attempted_at_ms INTEGER NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS new_token_alert_attempts_time ON new_token_alert_attempts(attempted_at_ms)")

    @staticmethod
    def _eligible(candidate: dict[str, Any], now_ms: int) -> bool:
        quote = candidate.get("paper_exit_quote") or {}
        as_of = int(candidate.get("as_of_ms") or 0)
        first_seen = int(candidate.get("first_seen_at_ms") or 0)
        quoted_at = int(quote.get("quoted_at_ms") or 0)
        notional = float(quote.get("notional_usd") or 0)
        return bool(candidate.get("status") == "ELIGIBLE"
            and candidate.get("official_identity_confirmed") is True
            and candidate.get("chain_id") and candidate.get("contract_address")
            and candidate.get("pool_id") and candidate.get("pool_provider")
            and candidate.get("evidence_event_ids") and not candidate.get("risk_flags")
            and 0 < first_seen <= as_of <= now_ms
            and now_ms - as_of <= 120_000 and as_of - first_seen <= 86_400_000
            and 0 < quoted_at <= as_of and now_ms - quoted_at <= 120_000
            and notional > 0 and float(quote.get("net_output_usd") or 0) > 0
            and float(candidate.get("proven_sellable_capacity_usd") or 0) >= notional)

    @staticmethod
    def _format(candidate: dict[str, Any]) -> str:
        quote = candidate["paper_exit_quote"]
        return (f"🔎 Новый для мониторинга токен: {candidate.get('symbol') or 'без тикера'}\n"
            f"Точная идентичность: {candidate['chain_id']} / {candidate['contract_address']}\n"
            f"Пул: {candidate['pool_provider']} / {candidate['pool_id']}\n"
            f"Почему в отчёте: подтверждены идентичность и данные пула; пройдены строгие фильтры риска "
            f"на момент снимка. Ликвидность: ${float(candidate.get('liquidity_usd') or 0):,.0f}; "
            f"доказанная ёмкость выхода: ${float(candidate['proven_sellable_capacity_usd']):,.0f}.\n"
            f"Модельный выход для ${float(quote['notional_usd']):,.0f}: "
            f"${float(quote['net_output_usd']):,.0f} после указанных издержек. "
            "Котировка не гарантирует исполнение.\n"
            f"Время оценки: {candidate['as_of_ms']} UTC ms. Доказательства: "
            f"{', '.join(str(item) for item in candidate['evidence_event_ids'][:5])}.\n"
            "Это отчёт о проверенном кандидате, не прогноз роста и не совет купить. ")[:3900]

    async def consider(self, candidate: dict[str, Any], now_ms: int) -> bool:
        if not self.enabled or not self.max_per_day or not self._eligible(candidate, now_ms):
            return False
        identity = f"{candidate['chain_id']}|{candidate['contract_address']}"
        key = hashlib.sha256(f"new-token-v1|{identity}".encode()).hexdigest()
        if self.storage.query("SELECT 1 FROM new_token_alert_attempts WHERE identity_key=?", (key,)):
            return False
        count = self.storage.query("SELECT COUNT(*) AS n FROM new_token_alert_attempts WHERE attempted_at_ms>=?",
            (now_ms - 86_400_000,))[0]["n"]
        if int(count) >= self.max_per_day:
            return False
        self.storage.execute("INSERT OR IGNORE INTO new_token_alert_attempts VALUES(?,?)", (key, now_ms))
        delivered, _ = await self.send_once(key, self._format(candidate), now_ms)
        return delivered

    async def run_forever(self, onchain: Any) -> None:
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                now = int(time.time() * 1000)
                for candidate in onchain.latest(100):
                    await self.consider(candidate, now)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Candidate alert delivery cannot stop market-data workers.
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), 30)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
