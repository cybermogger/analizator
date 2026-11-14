from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
import uuid


EXIT_STALE_HEARTBEAT = 75


def _positive_environment(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def read_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def heartbeat_is_fresh(
    path: Path,
    child_pid: int,
    started_at_ms: int,
    stale_seconds: float,
    run_id: str | None = None,
) -> bool:
    heartbeat = read_heartbeat(path)
    if heartbeat is None:
        return False
    try:
        updated_at = int(heartbeat["updated_at_ms"])
        heartbeat_pid = int(heartbeat["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    age_ms = int(time.time() * 1000) - updated_at
    identity_matches = (
        heartbeat.get("watchdog_run_id") == run_id
        if run_id is not None
        else heartbeat_pid == child_pid
    )
    return identity_matches and updated_at >= started_at_ms and 0 <= age_ms <= stale_seconds * 1000


def stop_child(process: subprocess.Popen[Any], terminate_seconds: float) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=terminate_seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def main() -> int:
    heartbeat = Path(os.getenv("ANALYZER_HEARTBEAT_PATH", "/data/heartbeat.json"))
    stale_seconds = _positive_environment("WATCHDOG_STALE_SECONDS", 180)
    startup_grace = _positive_environment("WATCHDOG_STARTUP_GRACE_SECONDS", 180)
    check_seconds = _positive_environment("WATCHDOG_CHECK_SECONDS", 5)
    terminate_seconds = _positive_environment("WATCHDOG_TERMINATE_SECONDS", 25)
    command = [sys.executable, "bot.py"]
    started_at_ms = int(time.time() * 1000)
    run_id = uuid.uuid4().hex
    child_environment = os.environ.copy()
    child_environment["ANALYZER_WATCHDOG_RUN_ID"] = run_id
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).resolve().parent,
        start_new_session=os.name == "posix",
        env=child_environment,
    )
    stopping = False

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    started_monotonic = time.monotonic()
    while True:
        returncode = process.poll()
        if returncode is not None:
            return 0 if stopping else int(returncode or 1)
        if stopping:
            stop_child(process, terminate_seconds)
            return 0
        if time.monotonic() - started_monotonic > startup_grace and not heartbeat_is_fresh(
            heartbeat, process.pid, started_at_ms, stale_seconds, run_id
        ):
            print(
                json.dumps(
                    {"event": "watchdog_stale_heartbeat", "path": str(heartbeat), "child_pid": process.pid},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            stop_child(process, terminate_seconds)
            return EXIT_STALE_HEARTBEAT
        time.sleep(min(check_seconds, 30))


if __name__ == "__main__":
    raise SystemExit(main())
