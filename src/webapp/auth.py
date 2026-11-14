from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """Telegram initData is malformed, invalid, stale, or not allowlisted."""


@dataclass(frozen=True, slots=True)
class TelegramPrincipal:
    user_id: int


class TelegramInitDataAuthenticator:
    def __init__(
        self,
        bot_token: str | None,
        allowed_user_ids: set[int] | frozenset[int] | tuple[int, ...] | list[int],
        *,
        max_age_seconds: int = 300,
        future_skew_seconds: int = 30,
        clock=time.time,
    ) -> None:
        self._bot_token = bot_token or ""
        self._allowed = frozenset(int(value) for value in allowed_user_ids if int(value) > 0)
        self.max_age_seconds = max(30, min(int(max_age_seconds), 3600))
        self.future_skew_seconds = max(0, min(int(future_skew_seconds), 120))
        self._clock = clock

    @property
    def configured(self) -> bool:
        return bool(self._bot_token and self._allowed)

    def validate(self, init_data: str) -> TelegramPrincipal:
        if not self.configured:
            raise InitDataError("Mini App authorization is not configured")
        if not init_data or len(init_data) > 8192:
            raise InitDataError("Missing or oversized initData")
        try:
            pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        except (ValueError, UnicodeError) as exc:
            raise InitDataError("Malformed initData") from exc
        values: dict[str, str] = {}
        for key, value in pairs:
            if key in values:
                raise InitDataError("Duplicate initData field")
            values[key] = value
        supplied_hash = values.get("hash", "")
        if len(supplied_hash) != 64:
            raise InitDataError("Missing initData hash")
        try:
            bytes.fromhex(supplied_hash)
        except ValueError as exc:
            raise InitDataError("Malformed initData hash") from exc

        check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(values.items()) if key != "hash"
        )
        secret_key = hmac.new(
            b"WebAppData", self._bot_token.encode("utf-8"), hashlib.sha256
        ).digest()
        expected = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied_hash.lower()):
            raise InitDataError("Invalid initData signature")

        try:
            auth_date = int(values["auth_date"])
            user = json.loads(values["user"])
            user_id = int(user["id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InitDataError("Missing valid user or auth_date") from exc
        now = int(self._clock())
        if auth_date > now + self.future_skew_seconds or now - auth_date > self.max_age_seconds:
            raise InitDataError("Expired initData")
        if user_id not in self._allowed:
            raise InitDataError("Telegram user is not allowlisted")
        return TelegramPrincipal(user_id=user_id)
