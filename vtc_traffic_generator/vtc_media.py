from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    return value


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).expanduser()
    with manifest_path.open("r", encoding="utf-8") as infile:
        return expand_env(json.load(infile))


def normalize_bot_id(bot: Mapping[str, Any] | str) -> str:
    if isinstance(bot, Mapping):
        value = bot.get("bot_id") or bot.get("name") or bot.get("bot_name")
    else:
        value = bot
    return str(value or "").strip()


def bot_number(bot_id: str) -> int | None:
    match = re.search(r"(\d+)$", bot_id)
    if not match:
        return None
    return int(match.group(1))


def display_name_for_bot(bot_id: str) -> str:
    number = bot_number(bot_id)
    if number is None:
        return bot_id
    return f"Bot {number}"


def media_entries_by_bot(manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    bots = manifest.get("bots")
    if isinstance(bots, Mapping):
        return {str(bot_id): dict(entry) for bot_id, entry in bots.items() if isinstance(entry, Mapping)}
    if isinstance(bots, Sequence) and not isinstance(bots, (str, bytes)):
        entries: dict[str, dict[str, Any]] = {}
        for item in bots:
            if not isinstance(item, Mapping):
                continue
            bot_id = normalize_bot_id(item)
            if bot_id:
                entries[bot_id] = dict(item)
        return entries
    return {}


def select_video_entry(manifest: Mapping[str, Any], bot_id: str) -> dict[str, Any]:
    entries = media_entries_by_bot(manifest)
    entry = entries.get(bot_id)
    if not entry:
        raise ValueError(f"media manifest is missing video entry for bot_id={bot_id}")

    s3_uri = entry.get("s3_uri")
    if not s3_uri:
        raise ValueError(f"media manifest entry for bot_id={bot_id} is missing s3_uri")

    local_cache_path = entry.get("local_cache_path")
    if not local_cache_path:
        local_cache_root = (
            manifest.get("local_cache_root")
            or manifest.get("cache_root")
            or "/home/ubuntu/vtc_data/media/google_meet/test/video"
        )
        local_cache_path = str(Path(str(local_cache_root)).expanduser() / Path(str(s3_uri)).name)

    return {
        **entry,
        "bot_id": bot_id,
        "platform": str(entry.get("platform") or manifest.get("platform") or manifest.get("service") or ""),
        "media_profile": str(entry.get("media_profile") or manifest.get("media_profile") or ""),
        "media_type": str(entry.get("media_type") or manifest.get("media_type") or "video"),
        "s3_uri": str(s3_uri),
        "local_cache_path": str(local_cache_path),
    }


def validate_selected_bots(manifest: Mapping[str, Any], selected_bots: Sequence[Mapping[str, Any] | str]) -> None:
    missing: list[str] = []
    for bot in selected_bots:
        bot_id = normalize_bot_id(bot)
        if not bot_id:
            missing.append("clients[] is missing bot_id/name")
            continue
        try:
            select_video_entry(manifest, bot_id)
        except ValueError as exc:
            missing.append(str(exc))
    if missing:
        raise ValueError("Invalid media manifest for selected bots: " + "; ".join(missing))
