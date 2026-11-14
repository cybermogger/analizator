from __future__ import annotations

import time

import pytest

from src.hyperliquid.rest import HyperliquidREST


class FakeResponse:
    def __init__(self, payload):
        self.status = 200
        self.payload = payload

    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None
    async def json(self, content_type=None): return self.payload
    async def text(self): return ""


class FakeSession:
    def __init__(self): self.requests = []
    def post(self, url, json):
        self.requests.append((url, json))
        kind = json["type"]
        if kind == "allMids": return FakeResponse({"SOL": "100"})
        if kind == "candleSnapshot":
            return FakeResponse([{"T": 159999, "c": "100", "h": "101", "i": "1m", "l": "99", "n": 10, "o": "100", "s": "SOL", "t": 100000, "v": "5"}])
        if kind == "l2Book":
            return FakeResponse({"coin": "SOL", "time": 123, "levels": [[{"px": "99", "sz": "2", "n": 1}], [{"px": "101", "sz": "3", "n": 1}]]})
        if kind == "metaAndAssetCtxs":
            return FakeResponse([{"universe": [{"name": "SOL", "szDecimals": 2}]}, [{"dayNtlVlm": "100", "funding": "0.0001", "markPx": "100", "midPx": "99.9", "openInterest": "5", "oraclePx": "100.1", "premium": "-0.001", "prevDayPx": "98"}]])
        raise AssertionError(kind)


@pytest.mark.asyncio
async def test_official_info_payload_contracts():
    session = FakeSession()
    async with HyperliquidREST("https://api.hyperliquid.xyz/info", timeout=1, session=session) as rest:
        await rest.all_mids()
        await rest.meta_and_asset_contexts()
        await rest.candles("SOL", "1m", 100000, 159999)
        await rest.l2_book("SOL")
    bodies = [body for _, body in session.requests]
    assert bodies == [
        {"type": "allMids"},
        {"type": "metaAndAssetCtxs"},
        {"type": "candleSnapshot", "req": {"coin": "SOL", "interval": "1m", "startTime": 100000, "endTime": 159999}},
        {"type": "l2Book", "coin": "SOL"},
    ]


def test_http_timeout_cannot_exceed_ten_seconds():
    with pytest.raises(ValueError):
        HyperliquidREST("https://api.hyperliquid.xyz/info", timeout=10.01)
    with pytest.raises(ValueError):
        HyperliquidREST("https://api.hyperliquid.xyz/info", timeout=0)
