import asyncio
from dataclasses import replace

from src.alerts.news import NewsAlertService
from src.data.storage import SQLiteStorage
from src.events.types import (
    ContradictionState, EventType, ExpectedDirection, ImpactedAsset, NormalizedEvent,
)


def news_event(now, *, event_id="event-1", kind=EventType.LISTING, credibility=.9):
    return NormalizedEvent(
        event_id=event_id, event_type=kind, summary="Official listing announcement",
        novelty=1.0, credibility=credibility, confirmation_count=1,
        impacted_assets=(ImpactedAsset("bonk-project", "solana:bonk-contract", "solana",
            "bonk-contract", ExpectedDirection.UNKNOWN, ("short",)),),
        expected_direction=ExpectedDirection.UNKNOWN, likely_horizons=("short",),
        decay_half_life_minutes=60, contradiction_state=ContradictionState.NONE,
        confidence=.8, first_published_at_ms=now - 2000,
        first_received_at_ms=now - 1000, latest_received_at_ms=now - 1000,
        raw_event_ids=("raw-1",), authenticity_risk=False, high_impact=True,
        source_ids=("official-source",), evidence_urls=("https://example.invalid/official",),
    )


def test_news_alert_explains_asset_and_corrects_retraction(tmp_path):
    db = SQLiteStorage(tmp_path / "news.db")
    now = 1_800_000_000_000
    sent = []

    async def fake_send(key, message, timestamp):
        sent.append(message)
        return True, "synthetic-id"

    alerts = NewsAlertService(db, {"enabled": True, "min_credibility": .7}, fake_send)
    event = news_event(now)
    assert asyncio.run(alerts.consider(event, now))
    assert not asyncio.run(alerts.consider(event, now + 1))
    assert "solana:bonk-contract" in sent[0]
    assert "Направление цены не определено" in sent[0]
    assert "https://example.invalid/official" in sent[0]
    corrected = replace(event, contradiction_state=ContradictionState.RETRACTED,
        latest_received_at_ms=now + 2000)
    assert asyncio.run(alerts.consider(corrected, now + 2000))
    assert not asyncio.run(alerts.consider(corrected, now + 2001))
    assert "Исправление новости" in sent[1]
    db.close()


def test_news_alert_rejects_unverified_stale_and_nonimpact(tmp_path):
    db = SQLiteStorage(tmp_path / "news.db")
    now = 1_800_000_000_000
    sent = []

    async def fake_send(key, message, timestamp):
        sent.append(message)
        return True, "synthetic-id"

    alerts = NewsAlertService(db, {"enabled": True}, fake_send)
    event = news_event(now)
    for candidate, at in (
        (replace(event, credibility=.3), now),
        (replace(event, authenticity_risk=True), now),
        (replace(event, high_impact=False), now),
        (replace(event, event_type=EventType.RUMOR), now),
        (event, now + 3_600_002),
    ):
        assert not asyncio.run(alerts.consider(candidate, at))
    assert sent == []
    db.close()
