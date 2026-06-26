from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


DEFAULT_EVENT_LOG_PATH = "/tmp/vtc-events.jsonl"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def get_bot_id(config: Mapping[str, Any]) -> str:
    bot = config.get("bot")
    if isinstance(bot, Mapping):
        display_name = bot.get("display_name")
        if display_name:
            return str(display_name)

    return str(config.get("bot_id") or config.get("bot_name") or "unknown")


def get_service_name(config: Mapping[str, Any]) -> str:
    return str(config.get("service") or config.get("vtc_platform") or "unknown")


def get_event_log_path(config: Mapping[str, Any]) -> str:
    adapter_config = config.get("adapter_config")
    if isinstance(adapter_config, Mapping):
        event_log_path = adapter_config.get("event_log_path")
        if event_log_path:
            return str(event_log_path)

    return str(config.get("event_log_path") or DEFAULT_EVENT_LOG_PATH)


def emit_event(
    config: Mapping[str, Any],
    event: str,
    details: Mapping[str, Any] | None = None,
    service: str | None = None,
) -> None:
    role = str(config.get("role") or "unknown")
    record = {
        "ts": utc_now_iso(),
        "monotonic_ns": time.monotonic_ns(),
        "run_id": str(config.get("execution_id") or config.get("run_id") or "unknown"),
        "experiment_id": str(config.get("experiment_id") or config.get("run_id") or "unknown"),
        "execution_id": str(config.get("execution_id") or config.get("run_id") or "unknown"),
        "bot_id": get_bot_id(config),
        "service": service or get_service_name(config),
        "role": role,
        "git_sha": str(config.get("git_sha") or ""),
        "config_sha256": str(config.get("config_sha256") or ""),
        "event_type": event,
        "event": event,
        "details": dict(details or {}),
    }

    path = Path(get_event_log_path(config))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as event_file:
        event_file.write(json.dumps(record, sort_keys=True) + "\n")


def resolve_git_sha(cwd: str | Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()
