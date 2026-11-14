from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TIMEOUT_SECONDS = 8


def telegram_call(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"Telegram request failed within {TIMEOUT_SECONDS}s: {error}") from error
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API rejected {method}: {result.get('description', 'unknown error')}")
    return result


def update_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)


def infer_chat_id(updates: dict[str, Any]) -> str | None:
    for update in reversed(updates.get("result", [])):
        message = update.get("message") or update.get("channel_post")
        if isinstance(message, dict) and isinstance(message.get("chat"), dict):
            chat_id = message["chat"].get("id")
            if chat_id is not None:
                return str(chat_id)
    return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env_path = Path(__file__).resolve().parent / ".env"
    token = getpass.getpass("Telegram bot token (input is hidden): ").strip()
    if not token:
        print("Token is required")
        return 2
    bot = telegram_call(token, "getMe").get("result", {})
    print(f"Bot validated: @{bot.get('username', 'unknown')}")
    print("Send one message to this bot, then press Enter. Only one bounded getUpdates request will be made.")
    input()
    chat_id = infer_chat_id(telegram_call(token, "getUpdates", {"limit": 100, "timeout": 0}))
    if chat_id is None:
        chat_id = input("No recent message found. Enter Telegram chat ID: ").strip()
    if not chat_id:
        print("Chat ID is required")
        return 2
    telegram_call(token, "sendMessage", {"chat_id": chat_id, "text": "Hyperliquid Analyzer: Telegram setup OK"})
    update_env(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
    print(f"Saved Telegram settings to {env_path}. Test message sent.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
