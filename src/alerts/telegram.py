from __future__ import annotations

from typing import Any

import aiohttp
from src.webapp.urls import valid_web_app_url


class TelegramClient:
    def __init__(self, token: str | None, chat_id: str | None, timeout: float = 10):
        if not 0 < timeout <= 10:
            raise ValueError("Telegram HTTP timeout must be > 0 and <= 10 seconds")
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def send(self, text: str) -> str | None:
        if not self.configured:
            return None
        return await self.send_to(str(self.chat_id), text)

    async def send_to(self, chat_id: str, text: str) -> str | None:
        if not self.token or not chat_id:
            return None
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}) as response:
                    if response.status != 200:
                        return None
                    payload: dict[str, Any] = await response.json()
                    message_id = payload.get("result", {}).get("message_id") if payload.get("ok") else None
                    return str(message_id) if message_id is not None else None
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def send_web_app_button(self, chat_id: str, url: str) -> str | None:
        """Send a Telegram Web App launch button only for a configured HTTPS app URL."""
        if not self.token or not chat_id or not valid_web_app_url(url):
            return None
        endpoint = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = {
            "chat_id": chat_id,
            "text": "Откройте Hyperliquid Analyzer в Telegram Mini App.",
            "reply_markup": {"inline_keyboard": [[{
                "text": "Открыть Analyzer", "web_app": {"url": url}
            }]]},
            "disable_web_page_preview": True,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=body) as response:
                    if response.status != 200:
                        return None
                    payload: dict[str, Any] = await response.json()
                    message_id = payload.get("result", {}).get("message_id") if payload.get("ok") else None
                    return str(message_id) if message_id is not None else None
        except (aiohttp.ClientError, TimeoutError):
            return None

    async def get_updates(self, offset: int | None = None, poll_seconds: int = 5) -> list[dict[str, Any]] | None:
        if not self.token:
            return []
        poll_seconds = max(0, min(int(poll_seconds), 5))
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        payload: dict[str, Any] = {"timeout": poll_seconds, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        timeout = aiohttp.ClientTimeout(total=min(10.0, max(self.timeout, poll_seconds + 2)))
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    if response.status != 200:
                        return None
                    value: dict[str, Any] = await response.json()
                    result = value.get("result", []) if value.get("ok") else []
                    return [item for item in result if isinstance(item, dict)]
        except (aiohttp.ClientError, TimeoutError):
            return None
