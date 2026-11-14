from __future__ import annotations

import re


_URL = re.compile(r"https?://[^\s<>\]\[(){}\"']+", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_CREDENTIAL = re.compile(
    r"(?i)([?&](?:api[_-]?key|token|secret|password|access_token|key)=)[^&#\s]+"
)
_TELEGRAM_BOT = re.compile(r"(?i)(/bot)[A-Za-z0-9:_-]{12,}")


def redact_sensitive_text(value: object, limit: int = 500) -> str:
    """Redact URLs and common credential forms before logs or operator alerts."""
    text = str(value)
    text = _URL.sub("[URL]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _CREDENTIAL.sub(r"\1[REDACTED]", text)
    text = _TELEGRAM_BOT.sub(r"\1[REDACTED]", text)
    return text[:max(0, int(limit))]


def safe_exception(error: BaseException, limit: int = 500) -> str:
    return f"{type(error).__name__}: {redact_sensitive_text(error, limit)}"
