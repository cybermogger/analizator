from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .provider_guard import validate_provider_config
from src.onchain.config import validate_onchain_config


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).resolve()
    # Offline tests can opt out before importing project modules; production keeps
    # the existing .env behavior. Tests must never accidentally ingest secrets.
    if os.getenv("ANALYZER_DISABLE_DOTENV", "").lower() not in {"1", "true", "yes", "on"}:
        load_dotenv(config_path.parent / ".env")
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = config_path.parent
    path_overrides = {
        "sqlite_path": "ANALYZER_DB_PATH",
        "raw_path": "ANALYZER_RAW_PATH",
        "models_path": "ANALYZER_MODELS_PATH",
        "logs_path": "ANALYZER_LOGS_PATH",
    }
    for key, environment_name in path_overrides.items():
        override = os.getenv(environment_name)
        if override:
            config["storage"][key] = override
        value = Path(config["storage"][key])
        config["storage"][key] = str(value if value.is_absolute() else root / value)
    strict_env = os.getenv("STRICT_ALERT_MODE")
    if strict_env is not None:
        config["alerts"]["strict_alert_mode"] = strict_env.lower() in {"1", "true", "yes", "on"}
    dashboard = config.setdefault("dashboard", {})
    dashboard["host"] = os.getenv("ANALYZER_DASHBOARD_HOST", dashboard.get("host", "127.0.0.1"))
    dashboard["container_loopback_proxy"] = os.getenv("ANALYZER_DASHBOARD_LOOPBACK_PROXY") == "1"
    validate_provider_config(config)
    validate_onchain_config(config)
    # Transitional aliases keep the proven SOL/HYPE analysis pipeline unchanged;
    # endpoint ownership now lives exclusively in the provider registry config.
    hyperliquid = config["providers"]["hyperliquid"]
    config["market"]["rest_url"] = hyperliquid["rest_url"]
    config["market"]["websocket_url"] = hyperliquid["websocket_url"]
    return config, root
