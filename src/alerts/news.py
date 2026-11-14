"""Conservative, source-linked market event notifications."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any

from src.events.types import ContradictionState, EventType, NormalizedEvent
from .multi_asset import SendOnce


_IMPACT = {
    EventType.LISTING: "Доступность торгов может вырасти; проверь объём и спред. Направление цены не определено.",
    EventType.DELISTING: "Риск снижения ликвидности и резких движений; проверь сроки и площадку.",
    EventType.HACK_EXPLOIT: "Риск потери доверия, оттока ликвидности и высокой волатильности.",
    EventType.REGULATORY: "Возможны изменения доступа к рынку и волатильности; эффект зависит от текста решения.",
    EventType.ETF: "Возможны изменения ожиданий спроса и волатильности; подтверждение потока средств отдельно.",
    EventType.TOKEN_UNLOCK: "Потенциальное предложение вырастет; фактические продажи не подтверждены.",
    EventType.OUTAGE: "Доступность торгов или вывода может ухудшиться; проверь статус площадки.",
    EventType.MACRO: "Возможна волатильность всего рынка через ставки и риск-аппетит; направление неизвестно.",
    EventType.OFFICIAL_TOKEN_LAUNCH: "Новый актив может быть крайне волатилен; проверь контракт, ликвидность и условия торгов.",
    EventType.PROTOCOL_UPGRADE: "Обновление сети может изменить комиссии, пропускную способность или риски исполнения; рыночная реакция и направление цены неизвестны.",
}


class NewsAlertService:
    def __init__(self, storage: Any, settings: dict[str, Any], send_once: SendOnce) -> None:
        self.storage = storage
        self.send_once = send_once
        self.enabled = bool(settings.get("enabled", False))
        self.min_credibility = min(1.0, max(0.0, float(settings.get("min_credibility", .7))))
        self.max_per_day = max(0, min(100, int(settings.get("max_per_day", 12))))
        storage.execute("""CREATE TABLE IF NOT EXISTS news_alert_attempts(
            alert_key TEXT PRIMARY KEY, attempted_at_ms INTEGER NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0)""")
        storage.execute("CREATE INDEX IF NOT EXISTS news_alert_attempts_time ON news_alert_attempts(attempted_at_ms)")

    @staticmethod
    def _eligible(event: NormalizedEvent, now_ms: int, min_credibility: float) -> bool:
        return bool(event.high_impact and not event.authenticity_risk
            and event.contradiction_state == ContradictionState.NONE
            and event.credibility >= min_credibility and event.event_type in _IMPACT
            and event.evidence_urls and event.source_ids
            and 0 <= now_ms - event.latest_received_at_ms <= 3_600_000
            and 0 <= now_ms - event.first_published_at_ms <= 86_400_000
            and (event.impacted_assets or event.event_type in (EventType.MACRO, EventType.REGULATORY)))

    @staticmethod
    def _format(event: NormalizedEvent) -> str:
        published = datetime.fromtimestamp(event.first_published_at_ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        assets = ", ".join(sorted({asset.canonical_asset_id for asset in event.impacted_assets})) or "рынок в целом"
        evidence = "\n".join(str(url) for url in event.evidence_urls[:3])
        summary = " ".join(event.summary.split())[:500]
        return (f"📰 Событие: {event.event_type.value}\n"
            f"Активы: {assets}\n"
            f"Что известно: {summary}\n"
            f"Возможное влияние: {_IMPACT[event.event_type]}\n"
            f"Опубликовано: {published}; источники: {', '.join(event.source_ids[:3])}.\n"
            f"Проверить первоисточник:\n{evidence}\n"
            "Это сценарий влияния, не прогноз цены и не торговый сигнал.")[:3900]

    async def consider(self, event: NormalizedEvent, now_ms: int) -> bool:
        if not self.enabled or not self.max_per_day:
            return False
        alert_key = hashlib.sha256(f"news-v1|{event.event_id}".encode()).hexdigest()
        correction = event.contradiction_state in (ContradictionState.RETRACTED, ContradictionState.CONTRADICTED)
        if correction:
            original = self.storage.query("SELECT delivered FROM news_alert_attempts WHERE alert_key=?", (alert_key,))
            if not original or not original[0]["delivered"] or not event.evidence_urls:
                return False
            alert_key = hashlib.sha256(f"news-correction-v1|{event.event_id}".encode()).hexdigest()
            message = (f"⚠️ Исправление новости {event.event_id}: исходное утверждение "
                f"{event.contradiction_state.value}. Не опирайся на прежний сценарий влияния.\n"
                f"Источник: {event.evidence_urls[-1]}")[:3900]
        elif self._eligible(event, now_ms, self.min_credibility):
            message = self._format(event)
        else:
            return False
        if self.storage.query("SELECT 1 FROM news_alert_attempts WHERE alert_key=?", (alert_key,)):
            return False
        count = self.storage.query("SELECT COUNT(*) AS n FROM news_alert_attempts WHERE attempted_at_ms>=?",
            (now_ms - 86_400_000,))[0]["n"]
        if int(count) >= self.max_per_day:
            return False
        self.storage.execute("INSERT OR IGNORE INTO news_alert_attempts(alert_key,attempted_at_ms) VALUES(?,?)", (alert_key, now_ms))
        delivered, _ = await self.send_once(alert_key, message, now_ms)
        if delivered:
            self.storage.execute("UPDATE news_alert_attempts SET delivered=1 WHERE alert_key=?", (alert_key,))
        return delivered
