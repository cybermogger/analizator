"""Bounded experimental spot alerts from exact-instrument shadow plans."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Awaitable, Callable, Sequence

from src.service.strategy_shadow import ShadowPlan


SendOnce = Callable[[str, str, int], Awaitable[tuple[bool, str | None]]]


class MultiAssetAlertService:
    def __init__(self, storage: Any, settings: dict[str, Any], send_once: SendOnce,
                 excluded_bases: Sequence[str] = ()) -> None:
        self.storage = storage
        self.send_once = send_once
        self.enabled = bool(settings.get("enabled", False))
        self.max_per_hour = max(0, min(20, int(settings.get("max_per_hour", 4))))
        self.max_per_day = max(0, min(100, int(settings.get("max_per_day", 20))))
        self.excluded_bases = frozenset(str(base).upper() for base in excluded_bases)
        storage.execute("""CREATE TABLE IF NOT EXISTS multi_asset_alert_attempts(
            plan_id TEXT PRIMARY KEY, attempted_at_ms INTEGER NOT NULL)""")
        storage.execute("CREATE INDEX IF NOT EXISTS multi_asset_alert_attempts_time ON multi_asset_alert_attempts(attempted_at_ms)")

    @staticmethod
    def _eligible(plan: ShadowPlan, card: dict[str, Any], now_ms: int) -> bool:
        if (plan.variant != "disciplined" or plan.status != "PROPOSED" or plan.direction != "LONG"
                or card.get("instrument_id") != plan.instrument_id
                or card.get("market_type") != "spot" or card.get("observation_status") != "OBSERVED"
                or not plan.provider or plan.provider != card.get("provider")
                or plan.venue != card.get("venue") or plan.quote != card.get("quote")
                or not 0 < plan.observed_at_ms <= now_ms < plan.expires_at_ms
                or now_ms - plan.observed_at_ms > 60_000
                or plan.entry_reference is None or plan.stop is None or plan.target is None
                or plan.notional <= 0):
            return False
        book = card.get("book") or {}
        if book.get("status") != "FRESH":
            return False
        quality = card.get("candle_quality") or {}
        candles = card.get("candles") or {}
        return all(quality.get(tf, {}).get("status") == "FRESH"
            and not quality.get(tf, {}).get("gaps")
            and candles.get(tf, {}).get("trend") in ("UP", "DOWN")
            for tf in ("15m", "1h", "4h"))

    @staticmethod
    def _format(plan: ShadowPlan, card: dict[str, Any]) -> str:
        observed = datetime.fromtimestamp(plan.observed_at_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        trends = ", ".join(f"{tf} {card['candles'][tf]['trend']}" for tf in ("15m", "1h", "4h"))
        return (f"🧪 Экспериментальный спотовый сетап · {plan.instrument_id}\n"
            f"Направление: LONG · площадка {plan.venue} · котировка {plan.quote}\n"
            f"Почему: согласованный тренд закрытых свечей ({trends}); свежие котировка и стакан.\n"
            f"Наблюдение: {observed}. Вход только по следующей свежей ask до истечения TTL.\n"
            f"Ориентир {plan.entry_reference:.8g}; стоп {plan.stop:.8g}; цель {plan.target:.8g}; "
            f"виртуальный номинал до {plan.notional:.2f} {plan.quote}.\n"
            f"Издержки модели: комиссия {plan.fee_bps_per_side:g} б.п./сторона, "
            f"проскальзывание {plan.slippage_bps_per_side:g} б.п./сторона.\n"
            "Инвалидация: просроченная котировка, исчезновение ликвидности, закрытие по стопу/времени. "
            "Это проверяемая гипотеза, не подтверждённый прогноз прибыли; ордер не отправлен.")

    async def consider(self, plans: Sequence[ShadowPlan], card: dict[str, Any], now_ms: int) -> int:
        if not self.enabled or not self.max_per_hour or not self.max_per_day:
            return 0
        if str(card.get("base", "")).upper() in self.excluded_bases:
            return 0
        sent = 0
        for plan in plans:
            if not self._eligible(plan, card, now_ms):
                continue
            if self.storage.query("SELECT 1 FROM multi_asset_alert_attempts WHERE plan_id=?", (plan.plan_id,)):
                continue
            recent = self.storage.query("""SELECT
                SUM(CASE WHEN attempted_at_ms>=? THEN 1 ELSE 0 END) AS hourly,
                COUNT(*) AS daily FROM multi_asset_alert_attempts WHERE attempted_at_ms>=?""",
                (now_ms - 3_600_000, now_ms - 86_400_000))[0]
            if int(recent["hourly"] or 0) >= self.max_per_hour or int(recent["daily"] or 0) >= self.max_per_day:
                break
            # Persist the attempt before awaiting delivery. A timeout remains
            # ambiguous and must not trigger a duplicate message after restart.
            self.storage.execute("INSERT OR IGNORE INTO multi_asset_alert_attempts VALUES(?,?)",
                (plan.plan_id, now_ms))
            key = hashlib.sha256(f"multi-asset-spot-v1|{plan.plan_id}".encode()).hexdigest()
            delivered, _ = await self.send_once(key, self._format(plan, card)[:3900], now_ms)
            sent += int(delivered)
        return sent
