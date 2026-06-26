#!/usr/bin/env python3
"""Validate a strict ICSI Jitsi run from collected S3 artifacts."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any


def load_jsonl_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*.jsonl")):
        with path.open("r", encoding="utf-8", errors="replace") as infile:
            for line_number, line in enumerate(infile, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    events.append(
                        {
                            "event_type": "invalid_jsonl",
                            "source_path": str(path),
                            "line_number": line_number,
                        }
                    )
                    continue
                record.setdefault("source_path", str(path))
                events.append(record)
    return events


def load_controller_action_events(run_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in sorted((run_dir / "controller").glob("actions-*.txt")):
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
                event_name = fields.get("event")
                if not event_name:
                    events.append(
                        {
                            "event_type": "invalid_action_log",
                            "source_path": str(path),
                            "line_number": line_number,
                        }
                    )
                    continue
                details: dict[str, Any] = {}
                if fields.get("details"):
                    try:
                        parsed = json.loads(fields["details"])
                        if isinstance(parsed, dict):
                            details = parsed
                    except json.JSONDecodeError:
                        details = {"unparsed_details": fields["details"]}
                events.append(
                    {
                        "ts": fields.get("ts"),
                        "event_type": event_name,
                        "event": event_name,
                        "bot_id": fields.get("bot", "controller"),
                        "role": "controller",
                        "details": details,
                        "source_path": str(path),
                        "line_number": line_number,
                    }
                )
    return events


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    events = load_jsonl_events(run_dir)
    has_controller_jsonl = any(Path(str(record.get("source_path", ""))).parent.name == "controller" for record in events)
    if not has_controller_jsonl:
        events.extend(load_controller_action_events(run_dir))
    return events


def event_type(record: dict[str, Any]) -> str:
    return str(record.get("event_type") or record.get("event") or "")


def details(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("details")
    return value if isinstance(value, dict) else {}


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize_values(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "median_ms": statistics.median(values) if values else None,
        "p95_ms": percentile(values, 0.95),
        "max_ms": max(values) if values else None,
    }


def media_summary(run_dir: Path, min_media_duration_sec: float) -> dict[str, Any]:
    pcaps = sorted(run_dir.rglob("*.pcapng"))
    media_jsons = sorted(run_dir.rglob("*.media-analysis.json"))
    per_bot = {}
    for path in media_jsons:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            per_bot[path.parent.name] = {"error": str(exc)}
            continue
        tuples = data.get("udp_5tuples", [])
        c2j = [
            item
            for item in tuples
            if item.get("direction") == "client_to_jvb" and int(item.get("dst_port", 0)) == 10000
        ]
        j2c = [
            item
            for item in tuples
            if item.get("direction") == "jvb_to_client" and int(item.get("src_port", 0)) == 10000
        ]
        streams = data.get("rtp_streams", [])
        c_stream = [item for item in streams if item.get("direction") == "client_to_jvb"]
        j_stream = [item for item in streams if item.get("direction") == "jvb_to_client"]
        min_direction_duration = min(
            max([float(item.get("duration_sec") or 0) for item in c_stream] or [0]),
            max([float(item.get("duration_sec") or 0) for item in j_stream] or [0]),
        )
        per_bot[path.parent.name] = {
            "packet_count": data.get("packet_count"),
            "packet_span_sec": data.get("packet_span_sec"),
            "protocol_counts": data.get("protocol_counts"),
            "client_to_jvb_packets": sum(int(item.get("packets", 0)) for item in c2j),
            "jvb_to_client_packets": sum(int(item.get("packets", 0)) for item in j2c),
            "client_to_jvb_bytes": sum(int(item.get("bytes", 0)) for item in c2j),
            "jvb_to_client_bytes": sum(int(item.get("bytes", 0)) for item in j2c),
            "min_direction_rtp_duration_sec": min_direction_duration,
            "passes_media_duration": min_direction_duration >= min_media_duration_sec,
        }
    filtered_pcaps = sorted(run_dir.rglob("*jitsi-only*.pcapng")) + sorted(run_dir.rglob("*filtered*.pcapng"))
    return {
        "pcapng_count": len(pcaps),
        "pcapng_total_bytes": sum(path.stat().st_size for path in pcaps if path.exists()),
        "media_analysis_count": len(media_jsons),
        "filtered_pcapng_count": len(filtered_pcaps),
        "filtered_pcapng_paths": [str(path) for path in filtered_pcaps],
        "per_bot": per_bot,
    }


def analyze(run_dir: Path, min_media_duration_sec: float) -> tuple[dict[str, Any], bool]:
    events = load_events(run_dir)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for record in events:
        by_type.setdefault(event_type(record), []).append(record)

    scheduled = by_type.get("speech_start_request", [])
    playback_starts = by_type.get("speech_playback_start", [])
    playback_ends = by_type.get("speech_playback_end", [])
    start_drifts = [
        float(details(record)["drift_ms"])
        for record in playback_starts
        if details(record).get("drift_ms") is not None
    ]
    end_drifts = [
        float(details(record)["end_drift_ms"])
        for record in playback_ends
        if details(record).get("end_drift_ms") is not None and not details(record).get("clipped")
    ]
    non_clipped_starts = [
        record
        for record in playback_starts
        if not details(record).get("clipped") and details(record).get("scheduled_t_rel_sec") is not None
    ]
    last_non_clipped = max(non_clipped_starts, key=lambda record: float(details(record).get("scheduled_t_rel_sec", 0)), default=None)
    last_non_clipped_drift = (
        float(details(last_non_clipped).get("drift_ms"))
        if last_non_clipped and details(last_non_clipped).get("drift_ms") is not None
        else None
    )

    mic_unverified = by_type.get("speech_started_with_mic_unverified", [])
    mic_true_failures = [
        record
        for record in events
        if event_type(record) in {"mic_on", "speech_prepare_mic_on_result"}
        and details(record).get("requested_state") is True
        and details(record).get("success") is False
    ]
    post_decisions = by_type.get("post_speech_mic_decision", [])
    post_action_results = by_type.get("post_speech_mic_action_result", [])
    keep_count = sum(1 for record in post_decisions if details(record).get("random_decision") == "keep_on")
    off_count = sum(1 for record in post_decisions if details(record).get("random_decision") == "mic_off")
    off_attempt_failures = [
        record
        for record in post_action_results
        if details(record).get("suppressed") is not True and details(record).get("success") is False
    ]

    camera_failures = [
        record
        for record in events
        if event_type(record) in {"camera_on", "camera_off", "camera_state_change"}
        and details(record).get("success") is False
    ]
    screen_failures = [
        record
        for record in events
        if event_type(record) in {"screen_share_start", "screen_share_end", "screen_share_stop", "screen_share_state_change"}
        and details(record).get("success") is False
    ]
    media = media_summary(run_dir, min_media_duration_sec)
    teardown_ok = len(by_type.get("meeting_disconnected", [])) >= 3

    speech_summary = summarize_values(start_drifts)
    pass_checks = {
        "all_scheduled_attempted": len(scheduled) > 0,
        "playback_start_for_every_attempt": len(playback_starts) >= len(scheduled) and len(scheduled) > 0,
        "median_start_drift_ms_lte_250": speech_summary["median_ms"] is not None and speech_summary["median_ms"] <= 250,
        "p95_start_drift_ms_lte_500": speech_summary["p95_ms"] is not None and speech_summary["p95_ms"] <= 500,
        "max_start_drift_ms_lte_1000": speech_summary["max_ms"] is not None and speech_summary["max_ms"] <= 1000,
        "last_non_clipped_drift_ms_lte_1000": last_non_clipped_drift is not None and last_non_clipped_drift <= 1000,
        "mic_verified_for_all_speech": len(mic_unverified) == 0 and len(scheduled) > 0,
        "mic_true_failures_zero": len(mic_true_failures) == 0,
        "post_speech_decisions_present": len(post_decisions) > 0,
        "post_speech_off_actions_valid": len(off_attempt_failures) == 0,
        "pcapng_for_every_client": media["pcapng_count"] >= 3,
        "media_analysis_for_every_client": media["media_analysis_count"] >= 3,
        "media_duration_ok": bool(media["per_bot"]) and all(item.get("passes_media_duration") for item in media["per_bot"].values()),
        "teardown_terminal": teardown_ok,
    }
    summary = {
        "run_dir": str(run_dir),
        "event_count": len(events),
        "scheduled_utterance_count": len(scheduled),
        "speech_playback_start_count": len(playback_starts),
        "speech_start_drift": speech_summary,
        "speech_end_drift": summarize_values(end_drifts),
        "last_non_clipped_start_drift_ms": last_non_clipped_drift,
        "speech_started_with_mic_unverified_count": len(mic_unverified),
        "mic_true_failure_count": len(mic_true_failures),
        "post_speech_mic_decision_count": len(post_decisions),
        "post_speech_keep_on_count": keep_count,
        "post_speech_mic_off_decision_count": off_count,
        "post_speech_mic_off_action_failure_count": len(off_attempt_failures),
        "camera_failure_count": len(camera_failures),
        "screen_share_failure_count": len(screen_failures),
        "teardown_terminal_success": teardown_ok,
        "pcap": media,
        "pass_checks": pass_checks,
    }
    return summary, all(pass_checks.values())


def write_markdown(summary: dict[str, Any], passed: bool, output_path: Path) -> None:
    lines = [
        f"# ICSI Run Analysis",
        "",
        f"Run directory: `{summary['run_dir']}`",
        f"Overall: {'PASS' if passed else 'FAIL'}",
        "",
        "## Acceptance Checks",
    ]
    for key, value in summary["pass_checks"].items():
        lines.append(f"- {'PASS' if value else 'FAIL'} `{key}`")
    lines.extend(
        [
            "",
            "## Speech Timing",
            f"- scheduled utterances: {summary['scheduled_utterance_count']}",
            f"- playback starts: {summary['speech_playback_start_count']}",
            f"- start drift: {summary['speech_start_drift']}",
            f"- end drift: {summary['speech_end_drift']}",
            f"- last non-clipped start drift ms: {summary['last_non_clipped_start_drift_ms']}",
            "",
            "## Microphone",
            f"- speech_started_with_mic_unverified: {summary['speech_started_with_mic_unverified_count']}",
            f"- mic true failures: {summary['mic_true_failure_count']}",
            f"- post-speech decisions: {summary['post_speech_mic_decision_count']}",
            f"- keep-on decisions: {summary['post_speech_keep_on_count']}",
            f"- off decisions: {summary['post_speech_mic_off_decision_count']}",
            "",
            "## PCAP",
            f"- pcapng count: {summary['pcap']['pcapng_count']}",
            f"- media analysis count: {summary['pcap']['media_analysis_count']}",
            f"- filtered pcapng count: {summary['pcap']['filtered_pcapng_count']}",
            "",
        ]
    )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", help="Local experiment artifact directory, e.g. captures/experiments/<run_id>")
    parser.add_argument("--output-dir", help="Directory for analysis_summary.json/md. Defaults to run_dir")
    parser.add_argument("--min-media-duration-sec", type=float, default=170.0)
    args = parser.parse_args()

    run_dir = Path(args.run_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else run_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    summary, passed = analyze(run_dir, args.min_media_duration_sec)
    (output_dir / "analysis_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_markdown(summary, passed, output_dir / "analysis_summary.md")
    print(json.dumps({"passed": passed, "summary": summary}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
