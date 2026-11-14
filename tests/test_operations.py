from __future__ import annotations

import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from setup_telegram import infer_chat_id, update_env
from src.alerts.critical import CriticalErrorNotifier
from src.data.repository import MarketRepository
from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.service.config import load_config
from src.service.health import write_heartbeat
from watchdog import heartbeat_is_fresh


class FakeTelegram:
    configured = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> str:
        self.messages.append(message)
        return str(len(self.messages))


@pytest.mark.asyncio
async def test_critical_error_is_persistently_deduplicated(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "state.db")
    repository = MarketRepository(storage)
    telegram = FakeTelegram()
    notifier = CriticalErrorNotifier(repository, telegram, cooldown_minutes=60)
    try:
        assert await notifier.notify("analysis:SOL", RuntimeError("request failed after 8 seconds"))
        assert not await notifier.notify("analysis:SOL", RuntimeError("request failed after 9 seconds"))
        rows = storage.query("SELECT occurrences,last_notified FROM critical_errors")
        assert rows[0]["occurrences"] == 2
        assert rows[0]["last_notified"] is not None
        assert len(telegram.messages) == 1
    finally:
        storage.close()


def test_heartbeat_is_atomic_and_bound_to_watchdog_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "heartbeat.json"
    now = int(time.time() * 1000)
    monkeypatch.setenv("ANALYZER_WATCHDOG_RUN_ID", "run-a")
    service = SimpleNamespace(
        state=SimpleNamespace(connected=True, last_update_ms=now),
        last_alert_ms=None,
    )
    write_heartbeat(path, "running", service)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert heartbeat_is_fresh(path, payload["pid"], now - 1000, 10)
    assert not heartbeat_is_fresh(path, payload["pid"] + 1, now - 1000, 10)
    assert heartbeat_is_fresh(path, 0, now - 1000, 10, "run-a")
    assert not heartbeat_is_fresh(path, payload["pid"], now - 1000, 10, "run-b")


def test_container_path_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("ANALYZER_DB_PATH", str(tmp_path / "db" / "analyzer.db"))
    monkeypatch.setenv("ANALYZER_RAW_PATH", str(tmp_path / "raw"))
    monkeypatch.setenv("ANALYZER_MODELS_PATH", str(tmp_path / "models"))
    monkeypatch.setenv("ANALYZER_LOGS_PATH", str(tmp_path / "logs"))
    config, _ = load_config(root / "config.yaml")
    assert config["storage"]["sqlite_path"] == str(tmp_path / "db" / "analyzer.db")
    assert config["storage"]["raw_path"] == str(tmp_path / "raw")
    assert config["storage"]["models_path"] == str(tmp_path / "models")
    assert config["storage"]["logs_path"] == str(tmp_path / "logs")


def test_setup_telegram_preserves_other_environment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("STRICT_ALERT_MODE=false\nTELEGRAM_BOT_TOKEN=old\n", encoding="utf-8")
    update_env(path, {"TELEGRAM_BOT_TOKEN": "new", "TELEGRAM_CHAT_ID": "42"})
    value = path.read_text(encoding="utf-8")
    assert "STRICT_ALERT_MODE=false" in value
    assert "TELEGRAM_BOT_TOKEN=new" in value
    assert "TELEGRAM_CHAT_ID=42" in value
    assert infer_chat_id({"result": [{"message": {"chat": {"id": -123}}}]}) == "-123"


def test_storage_migration_and_critical_table(tmp_path: Path) -> None:
    storage = SQLiteStorage(tmp_path / "migration.db")
    try:
        assert storage.health()["schema_version"] == SCHEMA_VERSION
        assert storage.query("PRAGMA table_info(critical_errors)")
    finally:
        storage.close()
