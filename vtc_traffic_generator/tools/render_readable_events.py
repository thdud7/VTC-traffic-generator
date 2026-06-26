#!/usr/bin/env python3
"""Render concise, human-readable VTC experiment event logs."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INCLUDED_EVENTS = {
    "meeting_join_ready",
    "scenario_start",
    "speech_playback_start",
    "speech_playback_end",
    "speech_playback_done",
    "audio_playback_done",
    "mic_on",
    "mic_off",
    "camera_on",
    "camera_off",
    "screen_share_start",
    "screen_share_end",
    "screen_share_stop",
    "meeting_end",
    "meeting_disconnected",
    "terminal_disconnect",
    "capture_started",
    "capture_stopped",
}

NORMALIZED_EVENTS = {
    "speech_playback_end": "speech_playback_done",
    "audio_playback_done": "speech_playback_done",
    "screen_share_stop": "screen_share_end",
    "meeting_disconnected": "terminal_disconnect",
}

DETAIL_KEYS = (
    "success",
    "bot_index",
    "bot_name",
    "meeting_id",
    "icsi_meeting_id",
    "speaker_id",
    "icsi_participant",
    "dialogue_act_type",
    "speech_id",
    "playback_segment_id",
    "scheduled_t_rel_sec",
    "playback_duration_sec",
    "requested_state",
    "enabled",
    "state",
    "stop_reason",
    "pcapng_path",
)


def parse_utc(value: Any) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def event_type(record: dict[str, Any]) -> str:
    return str(record.get("event_type") or record.get("event") or "")


def details(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("details")
    return value if isinstance(value, dict) else {}


def is_successful(record: dict[str, Any]) -> bool:
    data = details(record)
    if data.get("success") is False:
        return False
    if data.get("suppressed") is True:
        return False
    return True


def bot_id(record: dict[str, Any]) -> str:
    data = details(record)
    return str(
        record.get("bot_id")
        or data.get("bot_name")
        or data.get("bot_index")
        or record.get("role")
        or "unknown"
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as infile:
        for line_number, line in enumerate(infile, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                record.setdefault("_source_path", str(path))
                record.setdefault("_line_number", line_number)
                records.append(record)
    return records


def read_action_log(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as infile:
        for line_number, line in enumerate(infile, 1):
            line = line.rstrip("\n")
            if not line:
                continue
            fields: dict[str, str] = {}
            parts = line.split("\t")
            if parts:
                fields["ts"] = parts[0]
            for part in parts[1:]:
                key, sep, value = part.partition("=")
                if sep:
                    fields[key] = value
            name = fields.get("event")
            if not name:
                continue
            parsed_details: dict[str, Any] = {}
            if fields.get("details"):
                try:
                    value = json.loads(fields["details"])
                    if isinstance(value, dict):
                        parsed_details = value
                except json.JSONDecodeError:
                    parsed_details = {"details": fields["details"]}
            records.append(
                {
                    "ts": fields.get("ts"),
                    "event_type": name,
                    "bot_id": fields.get("bot") or "controller",
                    "details": parsed_details,
                    "_source_path": str(path),
                    "_line_number": line_number,
                }
            )
    return records


def load_records(paths: list[Path]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            records.extend(read_jsonl(path))
        else:
            records.extend(read_action_log(path))
    return records


def compact_details(record: dict[str, Any]) -> str:
    data = {key: details(record)[key] for key in DETAIL_KEYS if key in details(record)}
    if not data:
        return ""
    return " " + " ".join(f"{key}={format_value(value)}" for key, value in data.items())


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return str(value)


def render(
    records: list[dict[str, Any]],
    experiment_name: str | None = None,
    run_id: str | None = None,
    bot_filter: str | None = None,
) -> list[str]:
    filtered = [
        record
        for record in records
        if event_type(record) in INCLUDED_EVENTS and is_successful(record)
    ]
    if bot_filter:
        filtered = [record for record in filtered if bot_id(record) == bot_filter]
    filtered.sort(key=lambda record: (parse_utc(record.get("ts")), bot_id(record), event_type(record)))
    lines = [
        f"# Experiment: {experiment_name or infer_experiment_name(filtered)}",
        f"# Run: {run_id or infer_run_id(filtered)}",
        "# Format: [UTC timestamp] bot event success key=value ...",
        "",
    ]
    for record in filtered:
        name = NORMALIZED_EVENTS.get(event_type(record), event_type(record))
        ts = parse_utc(record.get("ts")).isoformat().replace("+00:00", "Z")
        lines.append(f"[{ts}] {bot_id(record)} {name} success{compact_details(record)}")
    return lines


def infer_run_id(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = record.get("run_id") or record.get("execution_id")
        if value:
            return str(value)
    return "unknown"


def infer_experiment_name(records: list[dict[str, Any]]) -> str:
    for record in records:
        value = record.get("experiment_id") or record.get("experiment_name")
        if value:
            return str(value)
    return "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="JSONL or action log files")
    parser.add_argument("--output", type=Path, help="Output readable log path")
    parser.add_argument("--experiment-name", help="Experiment name for the readable log header")
    parser.add_argument("--run-id", help="Run id for the readable log header")
    parser.add_argument("--bot-id", help="Only render events for this bot id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lines = render(
        load_records(args.paths),
        experiment_name=args.experiment_name,
        run_id=args.run_id,
        bot_filter=args.bot_id,
    )
    output = "\n".join(lines) + ("\n" if lines else "")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
