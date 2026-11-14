from __future__ import annotations

import hashlib
import re
import time
from typing import Any

import structlog
from src.util.redaction import redact_sensitive_text, safe_exception


_VARIABLE_VALUE = re.compile(r"(?<![A-Za-z])[+-]?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)(?![A-Za-z])")


class CriticalErrorNotifier:
    """Persistently suppress repeated operational errors within a cooldown."""

    def __init__(self, repository: Any, telegram: Any, cooldown_minutes: float = 60):
        self.repository = repository
        self.telegram = telegram
        self.cooldown_ms = max(1, int(cooldown_minutes * 60_000))
        self.log = structlog.get_logger("critical_errors")

    @staticmethod
    def fingerprint(scope: str, error: BaseException) -> str:
        normalized = _VARIABLE_VALUE.sub("#", redact_sensitive_text(error))[:500]
        value = f"{scope}|{type(error).__name__}|{normalized}"
        return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()

    async def notify(self, scope: str, error: BaseException) -> bool:
        timestamp = int(time.time() * 1000)
        fingerprint = self.fingerprint(scope, error)
        message = f"CRITICAL ERROR\nScope: {scope}\nError: {safe_exception(error, 700)}"
        try:
            state = self.repository.record_critical_error(fingerprint, scope, timestamp, message)
            last_notified = state.get("last_notified")
            if last_notified is not None and timestamp - int(last_notified) < self.cooldown_ms:
                self.log.warning(
                    "critical_error_suppressed",
                    scope=scope,
                    fingerprint=fingerprint[:12],
                    occurrences=state.get("occurrences"),
                )
                return False
            message_id = await self.telegram.send(message)
            if message_id is None:
                self.log.warning("critical_error_not_sent", scope=scope, configured=self.telegram.configured)
                return False
            self.repository.mark_critical_error_notified(fingerprint, timestamp)
            self.log.error("critical_error_notified", scope=scope, fingerprint=fingerprint[:12])
            return True
        except Exception as notifier_error:
            self.log.error("critical_notifier_failed", scope=scope, error_type=type(notifier_error).__name__)
            return False
