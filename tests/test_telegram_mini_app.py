from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.webapp.api import build_snapshot, create_web_app, valid_web_app_url
from src.webapp.auth import InitDataError, TelegramInitDataAuthenticator
from src.alerts.commands import TelegramCommandService
from src.alerts.telegram import TelegramClient


TOKEN = "123456:offline-test-token"
NOW = int(time.time())


def signed_init_data(token: str = TOKEN, *, user_id: int = 42, auth_date: int = NOW) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test-query",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@dataclass
class Truth:
    status: SimpleNamespace
    canonical_price: float
    calculated_at_ms: int
    quorum: int
    required_quorum: int
    venue_divergence_bps: float
    cross_exchange_basis_bps: float
    stale_sources: tuple[str, ...]
    rejected_outliers: tuple[str, ...]
    reason: str


class FakeService:
    def __init__(self):
        self.config = {"market": {"assets": ["SOL", "HYPE"]}}
        payload = {
            "primary_horizon": "1h", "horizons": {"1h": {"LONG": 40, "SHORT": 30, "WAIT": 30}},
            "mark": 150.0, "direction": "WAIT", "probability": 40.0, "confidence": 0.0,
            "data_freshness": {"status": "STALE", "book_receive_age_seconds": 201.0},
            "regime": "RANGE", "production_model_version": "prod-rules-v1",
        }
        self.repository = SimpleNamespace(latest_signal=lambda coin: {
            "payload_json": json.dumps(payload), "model_status": "MODEL_REJECTED_OOS",
            "timestamp": NOW * 1000,
        })
        truth = Truth(SimpleNamespace(value="degraded"), 0.0, NOW * 1000, 1, 2, 0.0, 0.0, ("provider-b",), (), "quorum missing")
        self.market_data = SimpleNamespace(
            truth={"native:solana": truth},
            health=lambda: {"provider-a": {"status": "healthy", "lag_ms": 50, "last_received_at_ms": NOW*1000, "consecutive_failures": 0}},
        )
        self.events = SimpleNamespace(
            health=lambda: {"official-feed": {"status": "disabled", "enabled": False, "official": True, "disabled_reason": "not configured"}},
            latest=lambda _limit: [],
        )
        self.onchain = SimpleNamespace(
            health=lambda: {"solana-rpc": {"status": "disabled", "enabled": False, "chain_id": "solana", "capabilities": {}, "not_available": ["holders"]}},
            latest=lambda _limit: [],
        )
        self.paper = SimpleNamespace(
            performance=lambda: {
                "initial_equity_usd": 10000.0, "cash_usd": 10000.0, "gross_realized_pnl_usd": 0.0,
                "net_realized_pnl_usd": 0.0, "net_unrealized_pnl_usd": 0.0,
                "fees_usd": 0.0, "funding_usd": 0.0, "slippage_usd": 0.0,
                "open_positions": 0, "reconciles": True,
            }, latest=lambda _limit: [], portfolio=SimpleNamespace(positions=lambda: {}),
        )
        self.state = SimpleNamespace(connected=True, last_update_ms=NOW * 1000)
        self.storage = SimpleNamespace(health=lambda: {"path": "must-not-leak.db", "integrity": "ok", "schema_version": 11, "bytes": 4096})
        self.telegram = SimpleNamespace(token=TOKEN)
        self.operational_metrics = lambda: {"degraded_reasons": ["market_truth:native:solana:degraded"]}


def test_telegram_init_data_signature_freshness_and_allowlist():
    auth = TelegramInitDataAuthenticator(TOKEN, {42}, clock=lambda: NOW)
    assert auth.validate(signed_init_data()).user_id == 42
    with pytest.raises(InitDataError, match="allowlisted"):
        auth.validate(signed_init_data(user_id=7))
    with pytest.raises(InitDataError, match="Expired"):
        auth.validate(signed_init_data(auth_date=NOW - 301))
    with pytest.raises(InitDataError, match="Expired"):
        auth.validate(signed_init_data(auth_date=NOW + 31))
    with pytest.raises(InitDataError, match="signature"):
        auth.validate(signed_init_data().replace("query_id=AAE-test-query", "query_id=AAE-tampered"))


def test_telegram_init_data_rejects_duplicate_and_missing_config():
    auth = TelegramInitDataAuthenticator(TOKEN, {42}, clock=lambda: NOW)
    data = signed_init_data()
    with pytest.raises(InitDataError, match="Duplicate"):
        auth.validate(data + "&auth_date=" + str(NOW))
    with pytest.raises(InitDataError, match="not configured"):
        TelegramInitDataAuthenticator(None, {42}, clock=lambda: NOW).validate(data)
    with pytest.raises(InitDataError, match="not configured"):
        TelegramInitDataAuthenticator(TOKEN, [], clock=lambda: NOW).validate(data)


def test_api_requires_auth_and_serializes_real_service_shapes_without_paths():
    async def run():
        service = FakeService()
        app = create_web_app(service, {"allowed_user_ids": [42], "init_data_max_age_seconds": 300})
        async with TestClient(TestServer(app)) as client:
            shell = await client.get("/app/")
            assert shell.status == 200
            assert "Content-Security-Policy" in shell.headers
            denied = await client.get("/api/v1/snapshot")
            assert denied.status == 401
            wrong_origin = await client.get(
                "/api/v1/snapshot", headers={"Origin": "https://attacker.example", "Authorization": "tma " + signed_init_data()}
            )
            assert wrong_origin.status == 403
            valid = await client.get("/api/v1/snapshot", headers={"Authorization": "tma " + signed_init_data()})
            assert valid.status == 200
            assert valid.headers["Cache-Control"].startswith("no-store")
            payload = await valid.json()
            assert payload["assets"][0]["direction"] == "WAIT"
            assert payload["assets"][0]["freshness"]["status"] == "STALE"
            assert payload["paper"]["open_position_count"] == 0
            assert payload["market_truth"]["native:solana"]["status"] == "degraded"
            assert payload["event_sources"]["official-feed"]["status"] == "disabled"
            assert payload["onchain_sources"]["solana-rpc"]["not_available"] == ["holders"]
            encoded = json.dumps(payload)
            assert "must-not-leak.db" not in encoded
            assert "path" not in payload["service"]["database"]
            assert payload["assets"][0]["explanation"].endswith(".")
    import asyncio
    asyncio.run(run())


def test_api_fails_closed_when_token_or_allowlist_missing():
    async def run():
        service = FakeService()
        service.telegram.token = None
        app = create_web_app(service, {"allowed_user_ids": [42]})
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/v1/snapshot", headers={"Authorization": "tma " + signed_init_data()})
            assert response.status == 503
            assert await response.json() == {"error": "mini_app_auth_not_configured"}
    import asyncio
    asyncio.run(run())


def test_static_interface_has_all_sections_and_no_unsafe_init_data_or_html_sink():
    root = Path(__file__).resolve().parents[1] / "webapp"
    html = (root / "index.html").read_text(encoding="utf-8")
    script = (root / "app.js").read_text(encoding="utf-8")
    for section in ("overview", "signals", "events", "portfolio", "sources"):
        assert f"view-{section}" in html
        assert f'data-nav="{section}"' in html
    assert "initDataUnsafe" not in script
    assert ".innerHTML" not in script
    assert "textContent" in script
    assert "window.Telegram?.WebApp" in script


def test_web_app_url_requires_owned_https_app_route():
    assert valid_web_app_url("https://analyzer.my-domain.net/app/")
    for value in (None, "http://analyzer.my-domain.net/app/", "https://localhost/app/", "https://127.0.0.1/app/", "https://analyzer.my-domain.net/", "https://user:pass@my-domain.net/app/", "https://example.com/app/?x=1"):
        assert not valid_web_app_url(value)


def test_app_command_sends_web_app_button_only_for_valid_public_https_url():
    class Telegram:
        token = "offline-token"
        chat_id = "7"
        def __init__(self): self.sent = []
        async def send_web_app_button(self, chat_id, url):
            self.sent.append(("button", chat_id, url))
            return "1"
        async def send_to(self, chat_id, text): self.sent.append(("text", chat_id, text))

    async def run(url):
        telegram = Telegram()
        analyzer = SimpleNamespace(telegram=telegram, repository=None, config={"mini_app": {"public_url": url}})
        command = TelegramCommandService(analyzer)
        await command.send_app_launch("7")
        return telegram.sent
    import asyncio
    valid = asyncio.run(run("https://analyzer.my-domain.net/app/"))
    assert valid == [("button", "7", "https://analyzer.my-domain.net/app/")]
    invalid = asyncio.run(run(None))
    assert len(invalid) == 1 and invalid[0][0] == "text"
    assert "TELEGRAM_MINI_APP_URL" in invalid[0][2]


def test_telegram_web_app_button_payload_is_offline_and_contains_no_trade_action(monkeypatch):
    import asyncio
    import src.alerts.telegram as telegram_module

    calls = []
    class Response:
        status = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def json(self): return {"ok": True, "result": {"message_id": 9}}
    class Session:
        def __init__(self, **_kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        def post(self, url, *, json):
            calls.append((url, json))
            return Response()
    monkeypatch.setattr(telegram_module.aiohttp, "ClientSession", Session)
    client = TelegramClient(TOKEN, "7")
    assert asyncio.run(client.send_web_app_button("7", "https://analyzer.my-domain.net/app/")) == "9"
    assert len(calls) == 1
    assert calls[0][1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == "https://analyzer.my-domain.net/app/"
    assert "order" not in json.dumps(calls[0][1]).lower()
    assert asyncio.run(client.send_web_app_button("7", "http://localhost/app/")) is None
    assert len(calls) == 1
