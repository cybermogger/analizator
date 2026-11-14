from __future__ import annotations

import pytest

from src.service.scheduler import _run_auto_retrain


@pytest.mark.asyncio
async def test_auto_retrain_reloads_only_after_success(monkeypatch):
    class Process:
        returncode = 0

        async def communicate(self):
            return b"quant audit complete", None

        def kill(self):
            raise AssertionError("successful process must not be killed")

        async def wait(self):
            return 0

    async def create_process(*_args, **_kwargs):
        return Process()

    class Registry:
        def __init__(self):
            self.loaded = []
            self.artifacts = {}

        def load(self, coin, horizon):
            self.loaded.append((coin, horizon))
            return True

    class Service:
        config = {
            "market": {"assets": ["SOL", "HYPE"]},
            "models": {
                "horizons": {"15m": {}, "1h": {}, "4h": {}},
                "auto_retrain": {"timeout_seconds": 2},
            },
        }
        models = Registry()

    monkeypatch.setattr("src.service.scheduler.asyncio.create_subprocess_exec", create_process)
    service = Service()
    await _run_auto_retrain(service)
    assert service.models.loaded == [
        ("SOL", "15m"), ("SOL", "1h"), ("SOL", "4h"),
        ("HYPE", "15m"), ("HYPE", "1h"), ("HYPE", "4h"),
    ]
