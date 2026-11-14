import asyncio

from src.alerts.new_tokens import NewTokenAlertService
from src.data.storage import SQLiteStorage


def candidate(now):
    return {"status": "ELIGIBLE", "chain_id": "solana", "contract_address": "exact-mint",
        "symbol": "MEME", "official_identity_confirmed": True,
        "first_seen_at_ms": now - 60_000, "as_of_ms": now - 1000,
        "pool_id": "verified-pool", "pool_provider": "official-rpc",
        "evidence_event_ids": ["chain-tx"], "risk_flags": [],
        "liquidity_usd": 50000, "proven_sellable_capacity_usd": 5000,
        "paper_exit_quote": {"notional_usd": 1000, "net_output_usd": 950,
            "quoted_at_ms": now - 2000}}


def test_new_token_report_requires_strict_evidence_and_deduplicates(tmp_path):
    db = SQLiteStorage(tmp_path / "tokens.db")
    now = 1_800_000_000_000
    sent = []

    async def fake_send(key, message, at):
        sent.append(message)
        return True, "synthetic-id"

    alerts = NewTokenAlertService(db, {"enabled": True}, fake_send)
    good = candidate(now)
    for key, value in (("status", "OBSERVE"), ("official_identity_confirmed", False),
                       ("risk_flags", ["holder_concentration"]),
                       ("proven_sellable_capacity_usd", 10)):
        bad = dict(good, **{key: value})
        assert not asyncio.run(alerts.consider(bad, now))
    assert asyncio.run(alerts.consider(good, now))
    assert not asyncio.run(alerts.consider(good, now + 1))
    assert "solana / exact-mint" in sent[0]
    assert "не прогноз роста" in sent[0]
    assert len(sent) == 1
    db.close()
