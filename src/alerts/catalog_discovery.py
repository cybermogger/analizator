"""Reports a newly observed official spot market after a safe catalog baseline."""
from __future__ import annotations

import hashlib
from typing import Any

from src.providers.types import MarketCatalogEntry, MarketCatalogScope
from .multi_asset import SendOnce


class CatalogDiscoveryAlerts:
    def __init__(self, storage: Any, settings: dict[str, Any], send_once: SendOnce) -> None:
        self.storage = storage
        self.send_once = send_once
        self.enabled = bool(settings.get("enabled", False))
        self.max_per_day = max(0, min(30, int(settings.get("max_per_day", 6))))
        self.max_per_scope = max(0, min(3, int(settings.get("max_per_scope", 2))))
        self.min_turnover = max(0.0, float(settings.get("min_quote_turnover_24h", 1_000_000)))
        self.max_spread_bps = max(0.0, float(settings.get("max_spread_bps", 50)))
        storage.execute("""CREATE TABLE IF NOT EXISTS catalog_alert_scopes(
            scope_key TEXT PRIMARY KEY, bootstrapped_at_ms INTEGER NOT NULL)""")
        storage.execute("""CREATE TABLE IF NOT EXISTS catalog_alert_seen(
            scope_key TEXT NOT NULL, instrument_id TEXT NOT NULL,
            first_seen_at_ms INTEGER NOT NULL, PRIMARY KEY(scope_key,instrument_id))""")
        storage.execute("""CREATE TABLE IF NOT EXISTS catalog_alert_attempts(
            alert_key TEXT PRIMARY KEY, attempted_at_ms INTEGER NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS catalog_alert_attempts_time ON catalog_alert_attempts(attempted_at_ms)")

    def _eligible(self, entry: MarketCatalogEntry, now_ms: int) -> bool:
        instrument = entry.instrument
        if (not entry.active or not entry.admitted or not instrument.active
                or instrument.market_type.value != "spot" or instrument.quote not in ("USD", "USDT", "USDC")
                or entry.quote_turnover_24h is None or entry.quote_turnover_24h < self.min_turnover
                or not 0 <= now_ms - entry.received_at_ms <= 180_000
                or entry.bid is None or entry.ask is None or entry.bid <= 0 or entry.ask <= 0):
            return False
        return (entry.ask - entry.bid) / ((entry.ask + entry.bid) / 2) * 10_000 <= self.max_spread_bps

    @staticmethod
    def _format(entry: MarketCatalogEntry) -> str:
        instrument = entry.instrument
        return (f"🆕 Новая для нашего каталога спотовая пара: {instrument.instrument_id}\n"
            f"Площадка: {instrument.venue}; котировка: {instrument.quote}.\n"
            f"Почему в отчёте: официальная площадка впервые добавила эту точную пару после "
            f"первоначального снимка; 24ч оборот {entry.quote_turnover_24h:,.0f} "
            f"{instrument.quote}, спред {((entry.ask-entry.bid)/((entry.ask+entry.bid)/2)*10_000):.1f} б.п.\n"
            f"Источник каталога: {entry.source}; получено {entry.received_at_ms} UTC ms.\n"
            "Это обнаружение пары, а не доказательство запуска нового токена, роста цены или возможности "
            "купить и продать заявленный объём. Проверь контракт, историю листинга и ликвидность отдельно.")[:3900]

    async def observe(self, scope: MarketCatalogScope, now_ms: int) -> int:
        if not self.enabled or not self.max_per_day or not self.max_per_scope or not scope.complete or not scope.entries:
            return 0
        scope_key = f"{scope.provider}:{scope.scope}"
        initialized = self.storage.query("SELECT 1 FROM catalog_alert_scopes WHERE scope_key=?", (scope_key,))
        if not initialized:
            # An initial catalog cannot establish which instruments are genuinely new.
            self.storage.executemany("INSERT OR IGNORE INTO catalog_alert_seen VALUES(?,?,0)",
                ((scope_key, entry.instrument.instrument_id) for entry in scope.entries))
            self.storage.execute("INSERT OR IGNORE INTO catalog_alert_scopes VALUES(?,?)", (scope_key, now_ms))
            return 0
        sent = 0
        attempted = 0
        seen = {row["instrument_id"]: int(row["first_seen_at_ms"])
            for row in self.storage.query("SELECT instrument_id,first_seen_at_ms FROM catalog_alert_seen WHERE scope_key=?",
                (scope_key,))}
        for entry in scope.entries:
            iid = entry.instrument.instrument_id
            first_seen = seen.get(iid)
            if first_seen is None:
                first_seen = now_ms
                self.storage.execute("INSERT OR IGNORE INTO catalog_alert_seen VALUES(?,?,?)",
                    (scope_key, iid, first_seen))
            if not 0 < first_seen <= now_ms or now_ms - first_seen > 86_400_000 or not self._eligible(entry, now_ms):
                continue
            key = hashlib.sha256(f"catalog-new-v1|{scope_key}|{iid}".encode()).hexdigest()
            if self.storage.query("SELECT 1 FROM catalog_alert_attempts WHERE alert_key=?", (key,)):
                continue
            recent = self.storage.query("SELECT COUNT(*) AS n FROM catalog_alert_attempts WHERE attempted_at_ms>=?",
                (now_ms - 86_400_000,))[0]["n"]
            if int(recent) >= self.max_per_day:
                break
            self.storage.execute("INSERT OR IGNORE INTO catalog_alert_attempts VALUES(?,?)", (key, now_ms))
            attempted += 1
            delivered, _ = await self.send_once(key, self._format(entry), now_ms)
            sent += int(delivered)
            if attempted >= self.max_per_scope:
                break
        return sent
