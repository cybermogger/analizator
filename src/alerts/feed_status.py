from __future__ import annotations

import time
from typing import Any

import structlog


class FeedStatusNotifier:
    """Persistent one-shot warning and recovery notification for the Hyperliquid feed."""

    def __init__(self, repository: Any, telegram: Any, warning_after_seconds: float = 180):
        self.repository = repository
        self.telegram = telegram
        self.warning_after_ms = max(1, int(warning_after_seconds * 1000))
        self.log = structlog.get_logger("feed_status")

    async def update(self, healthy: bool, details: str | None = None, now_ms: int | None = None) -> str | None:
        now_ms = now_ms or int(time.time() * 1000)
        current = self.repository.technical_state("hyperliquid_feed")
        desired = "healthy" if healthy else "unhealthy"
        if current is None:
            self.repository.save_technical_state("hyperliquid_feed", desired, now_ms, None, details)
            return None
        if current["state"] != desired:
            if healthy:
                notified = current.get("last_notified") is not None
                self.repository.save_technical_state("hyperliquid_feed", "healthy", now_ms, None, details)
                if notified:
                    message = "✅ Hyperliquid data feed restored"
                    message_id = await self.telegram.send(message)
                    if message_id is not None:
                        self.log.info("feed_recovery_notified")
                        return "FEED_RESTORED"
                return None
            self.repository.save_technical_state("hyperliquid_feed", "unhealthy", now_ms, None, details)
            return None
        if not healthy and current.get("last_notified") is None and now_ms - int(current["changed_at"]) >= self.warning_after_ms:
            message = "⚠️ Hyperliquid data feed unavailable/stale. Directional alerts are paused."
            message_id = await self.telegram.send(message)
            if message_id is not None:
                self.repository.save_technical_state(
                    "hyperliquid_feed", "unhealthy", int(current["changed_at"]), now_ms, details
                )
                self.log.warning("feed_warning_notified", details=details)
                return "FEED_WARNING"
        return None
