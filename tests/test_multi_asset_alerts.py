import asyncio

from src.alerts.multi_asset import MultiAssetAlertService
from src.data.storage import SQLiteStorage
from src.service.strategy_shadow import StrategyShadow


def alert_card(now, iid="binance:spot:BONK-USDT"):
    return {"instrument_id": iid, "provider": "binance", "venue": "binance-spot",
        "market_type": "spot", "quote": "USDT", "base": iid.split(":")[-1].split("-")[0],
        "observed_at_ms": now, "observation_status": "OBSERVED", "book": {"status": "FRESH"},
        "candles": {tf: {"trend": "UP", "realized_return_volatility_pct_per_bar": .4,
            "latest_exchange_time_ms": now - 1000, "source_provider": "binance",
            "source_venue": "binance-spot"} for tf in ("15m", "1h", "4h")},
        "candle_quality": {tf: {"status": "FRESH", "received_at_ms": now - 1000,
            "gaps": 0} for tf in ("15m", "1h", "4h")},
        "bbo": {"source_provider": "binance", "source_venue": "binance-spot",
            "received_at_ms": now - 1000, "exchange_time_kind": "RECEIVE_TIME_FALLBACK",
            "bid": 99.9, "ask": 100.1}}


def test_experimental_alert_is_exact_bounded_and_idempotent(tmp_path):
    db = SQLiteStorage(tmp_path / "alerts.db")
    now = 1_800_000_000_000
    card = alert_card(now)
    plans = StrategyShadow(db).evaluate(card)
    delivered = []

    async def send_once(key, message, at):
        delivered.append((key, message, at))
        return True, "synthetic-id"

    alerts = MultiAssetAlertService(db, {"enabled": True, "max_per_hour": 1, "max_per_day": 1},
        send_once, excluded_bases=("SOL", "HYPE"))
    assert asyncio.run(alerts.consider(plans, card, now)) == 1
    assert asyncio.run(alerts.consider(plans, card, now + 1)) == 0
    assert len(delivered) == 1
    assert "BONK" in delivered[0][1] and "стоп" in delivered[0][1]
    assert "не подтверждённый прогноз" in delivered[0][1]
    assert "вероятность" not in delivered[0][1]
    assert len(delivered[0][1]) < 3900
    db.close()


def test_experimental_alert_requires_fresh_complete_evidence(tmp_path):
    db = SQLiteStorage(tmp_path / "evidence.db")
    now = 1_800_000_000_000
    card = alert_card(now)
    plans = StrategyShadow(db).evaluate(card)
    delivered = []

    async def send_once(key, message, at):
        delivered.append(key)
        return True, "synthetic-id"

    alerts = MultiAssetAlertService(db, {"enabled": True}, send_once)
    card["book"]["status"] = "STALE"
    assert asyncio.run(alerts.consider(plans, card, now)) == 0
    card["book"]["status"] = "FRESH"
    card["candle_quality"]["4h"]["gaps"] = 1
    assert asyncio.run(alerts.consider(plans, card, now)) == 0
    card["candle_quality"]["4h"]["gaps"] = 0
    assert asyncio.run(alerts.consider(plans, card, now + 61_000)) == 0
    card["observed_at_ms"] = now
    assert delivered == []
    db.close()


def test_experimental_alert_hourly_cap_blocks_other_market(tmp_path):
    db = SQLiteStorage(tmp_path / "cap.db")
    now = 1_800_000_000_000
    strategy = StrategyShadow(db)
    sent = []

    async def send_once(key, message, at):
        sent.append(key)
        return True, "synthetic-id"

    alerts = MultiAssetAlertService(db, {"enabled": True, "max_per_hour": 1, "max_per_day": 2}, send_once)
    first = alert_card(now)
    second = alert_card(now, "binance:spot:PEPE-USDT")
    second["base"] = "PEPE"
    assert asyncio.run(alerts.consider(strategy.evaluate(first), first, now)) == 1
    assert asyncio.run(alerts.consider(strategy.evaluate(second), second, now)) == 0
    assert len(sent) == 1
    db.close()
