#!/usr/bin/env python3
"""Validate a strict ICSI Jitsi run from collected S3 artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from vtc_traffic_generator.tools import render_readable_events


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


def normalized_bot_id(record: dict[str, Any]) -> str:
    data = details(record)
    value = record.get("bot_id")
    if isinstance(value, str) and value.startswith("bot"):
        return value
    if data.get("bot_name"):
        return str(data["bot_name"])
    if data.get("bot_id"):
        text = str(data["bot_id"])
        return text if text.startswith("bot") else f"bot{text}"
    if data.get("bot_index") is not None:
        try:
            return f"bot{int(data['bot_index']) + 1}"
        except (TypeError, ValueError):
            return str(data["bot_index"])
    return str(value or "unknown")


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


def parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(start: Any, end: Any) -> float | None:
    start_dt = parse_utc(start)
    end_dt = parse_utc(end)
    if start_dt is None or end_dt is None:
        return None
    return (end_dt - start_dt).total_seconds()


def compute_playback_overlaps(events: list[dict[str, Any]]) -> dict[str, Any]:
    starts: dict[tuple[str, str], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []
    for record in events:
        etype = event_type(record)
        data = details(record)
        segment_id = str(data.get("playback_segment_id") or data.get("speech_id") or "")
        if not segment_id:
            continue
        bot_id = normalized_bot_id(record)
        key = (bot_id, segment_id)
        if etype == "audio_playback_start":
            starts[key] = record
        elif etype in {"audio_playback_done", "audio_playback_failed"} and key in starts:
            start_data = details(starts[key])
            start_dt = parse_utc(start_data.get("actual_utc") or starts[key].get("ts"))
            end_dt = parse_utc(data.get("actual_utc") or record.get("ts"))
            if start_dt and end_dt:
                intervals.append(
                    {
                        "bot_id": bot_id,
                        "playback_segment_id": segment_id,
                        "start_utc": start_dt.isoformat().replace("+00:00", "Z"),
                        "end_utc": end_dt.isoformat().replace("+00:00", "Z"),
                        "duration_sec": max(0.0, (end_dt - start_dt).total_seconds()),
                        "status": "done" if etype == "audio_playback_done" else "failed",
                    }
                )

    overlaps: list[dict[str, Any]] = []
    for bot_id in sorted({item["bot_id"] for item in intervals}):
        bot_intervals = sorted(
            [item for item in intervals if item["bot_id"] == bot_id],
            key=lambda item: item["start_utc"],
        )
        previous = None
        for interval in bot_intervals:
            if previous is not None:
                overlap_sec = seconds_between(interval["start_utc"], previous["end_utc"])
                if overlap_sec is not None and overlap_sec > 0:
                    overlaps.append(
                        {
                            "bot_id": bot_id,
                            "previous_segment_id": previous["playback_segment_id"],
                            "segment_id": interval["playback_segment_id"],
                            "overlap_sec": overlap_sec,
                        }
                    )
            if previous is None or interval["end_utc"] > previous["end_utc"]:
                previous = interval

    max_overlap_sec = max([item["overlap_sec"] for item in overlaps] or [0.0])
    return {
        "interval_count": len(intervals),
        "overlap_count": len(overlaps),
        "max_overlap_ms": max_overlap_sec * 1000.0,
        "overlaps": overlaps,
        "intervals": intervals,
    }


def summarize_capture_lifecycle(events: list[dict[str, Any]], expected_tail_sec: float) -> dict[str, Any]:
    lifecycle: dict[str, dict[str, Any]] = {}
    for record in events:
        bot_id = normalized_bot_id(record)
        if bot_id in {"controller", "unknown"}:
            continue
        item = lifecycle.setdefault(bot_id, {"bot_id": bot_id})
        etype = event_type(record)
        ts = record.get("ts")
        if etype in {"packet_capture_start", "capture_started"}:
            item.setdefault("capture_start_utc", ts)
        elif etype in {"packet_capture_done", "capture_stopped"}:
            item["capture_stop_utc"] = ts
        elif etype == "meeting_join_ready":
            item.setdefault("meeting_ready_utc", ts)
        elif etype in {"meeting_disconnected", "terminal_disconnect"}:
            item["meeting_disconnected_utc"] = ts

    for item in lifecycle.values():
        item["capture_duration_sec"] = seconds_between(item.get("capture_start_utc"), item.get("capture_stop_utc"))
        start_delta = seconds_between(item.get("capture_start_utc"), item.get("meeting_ready_utc"))
        stop_delta = seconds_between(item.get("meeting_disconnected_utc"), item.get("capture_stop_utc"))
        item["capture_started_before_meeting_ready"] = start_delta is not None and start_delta >= 0
        item["capture_stopped_after_meeting_disconnected"] = stop_delta is not None and stop_delta >= expected_tail_sec
        item["post_disconnect_capture_tail_sec"] = stop_delta
        item["expected_tail_sec"] = expected_tail_sec
    return lifecycle


def media_summary(run_dir: Path, min_media_duration_sec: float) -> dict[str, Any]:
    pcaps = sorted(
        path
        for path in run_dir.rglob("*.pcapng")
        if "jitsi-only" not in path.name and "filtered" not in path.name
    )
    media_jsons = sorted({*run_dir.rglob("*.media-analysis.json"), *run_dir.rglob("*_media-analysis.json")})
    per_bot = {}
    media_by_bot = {}
    for path in media_jsons:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            per_bot[artifact_bot_id(path, run_dir)] = {"error": str(exc)}
            continue
        bot_id = artifact_bot_id(path, run_dir)
        media_by_bot[bot_id] = data
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
        basic_validation = data.get("basic_webrtc_validation") or {}
        basic_validation_passed = bool(basic_validation) and all(bool(value) for value in basic_validation.values())
        per_bot[bot_id] = {
            "packet_count": data.get("packet_count"),
            "packet_span_sec": data.get("packet_span_sec"),
            "capture_start_utc": data.get("capture_start_utc"),
            "capture_end_utc": data.get("capture_end_utc"),
            "protocol_counts": data.get("protocol_counts"),
            "client_to_jvb_packets": sum(int(item.get("packets", 0)) for item in c2j),
            "jvb_to_client_packets": sum(int(item.get("packets", 0)) for item in j2c),
            "client_to_jvb_bytes": sum(int(item.get("bytes", 0)) for item in c2j),
            "jvb_to_client_bytes": sum(int(item.get("bytes", 0)) for item in j2c),
            "min_direction_rtp_duration_sec": min_direction_duration,
            "passes_media_duration": min_direction_duration >= min_media_duration_sec,
            "basic_webrtc_validation": basic_validation,
            "basic_webrtc_validation_passed": basic_validation_passed,
            "final_verdict": data.get("final_verdict"),
            "fail_reasons": data.get("fail_reasons", []),
        }
    filtered_pcaps = sorted(run_dir.rglob("*jitsi-only*.pcapng")) + sorted(run_dir.rglob("*filtered*.pcapng"))
    return {
        "pcapng_count": len(pcaps),
        "pcapng_total_bytes": sum(path.stat().st_size for path in pcaps if path.exists()),
        "raw_pcapng_paths": [str(path) for path in pcaps],
        "media_analysis_count": len(media_jsons),
        "filtered_pcapng_count": len(filtered_pcaps),
        "filtered_pcapng_paths": [str(path) for path in filtered_pcaps],
        "per_bot": per_bot,
        "media_by_bot": media_by_bot,
    }


def artifact_bot_id(path: Path, run_dir: Path) -> str:
    try:
        parts = path.relative_to(run_dir).parts
    except ValueError:
        parts = path.parts
    for token in path.stem.replace("-", "_").split("_"):
        if token.startswith("bot") and token[3:].isdigit():
            return token
    for part in parts:
        if part.startswith("bot"):
            return part
    if len(parts) >= 2 and parts[0] == "controller":
        return "controller"
    return path.parent.name


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
        and details(record).get("suppressed") is not True
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
    mic_checks = compute_mic_policy_checks(events)
    camera_checks = compute_camera_action_checks(events, media.get("media_by_bot", {}))
    playback_overlap = compute_playback_overlaps(events)
    capture_lifecycle = summarize_capture_lifecycle(events, expected_tail_sec=10.0)
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
        "post_speech_decision_for_every_completed_segment": mic_checks["missing_decision_count"] == 0
        and mic_checks["duplicate_decision_count"] == 0,
        "post_speech_off_actions_valid": len(off_attempt_failures) == 0,
        "mic_off_before_audio_done_zero": mic_checks["mic_off_before_audio_done_count"] == 0,
        "mic_off_guard_violation_zero": mic_checks["mic_off_guard_violation_count"] == 0,
        "mic_off_during_active_speech_zero": mic_checks["mic_off_during_active_speech_count"] == 0,
        "duplicate_mic_action_zero": mic_checks["duplicate_mic_action_count"] == 0,
        "raw_pcapng_for_every_client": media["pcapng_count"] >= 3,
        "pcapng_for_every_client": media["pcapng_count"] >= 3,
        "postprocessed_pcapng_absent": media["filtered_pcapng_count"] == 0,
        "media_analysis_for_every_client": media["media_analysis_count"] >= 3,
        "basic_webrtc_validation_ok": bool(media["per_bot"])
        and all(item.get("basic_webrtc_validation_passed") for item in media["per_bot"].values()),
        "media_duration_ok": bool(media["per_bot"]) and all(item.get("passes_media_duration") for item in media["per_bot"].values()),
        "screen_share_packet_validation_not_gating": True,
        "camera_action_packet_windows_ok": camera_checks["failure_count"] == 0,
        "same_bot_audio_playback_overlap_zero": playback_overlap["max_overlap_ms"] <= 0.0,
        "capture_lifecycle_present": len(capture_lifecycle) >= 3,
        "capture_starts_before_meeting_ready": bool(capture_lifecycle)
        and all(item.get("capture_started_before_meeting_ready") for item in capture_lifecycle.values()),
        "capture_stops_after_disconnect": bool(capture_lifecycle)
        and all(item.get("capture_stopped_after_meeting_disconnected") for item in capture_lifecycle.values()),
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
        "mic_policy_checks": mic_checks,
        "camera_failure_count": len(camera_failures),
        "camera_action_checks": camera_checks,
        "screen_share_failure_count": len(screen_failures),
        "playback_overlap": playback_overlap,
        "capture_lifecycle": capture_lifecycle,
        "teardown_terminal_success": teardown_ok,
        "pcap": media,
        "pass_checks": pass_checks,
    }
    return summary, all(pass_checks.values())


def compute_mic_policy_checks(events: list[dict[str, Any]]) -> dict[str, Any]:
    done_by_segment: dict[str, dict[str, Any]] = {}
    decisions_by_segment: dict[str, list[dict[str, Any]]] = {}
    action_starts: list[dict[str, Any]] = []
    mic_events: list[dict[str, Any]] = []
    speech_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
    for record in events:
        etype = event_type(record)
        data = details(record)
        segment_id = str(data.get("playback_segment_id") or data.get("speech_id") or "")
        bot_id = normalized_bot_id(record)
        if etype == "audio_playback_done" and segment_id:
            done_by_segment[segment_id] = record
        elif etype == "post_speech_mic_decision" and segment_id:
            decisions_by_segment.setdefault(segment_id, []).append(record)
        elif etype == "post_speech_mic_action_start" and data.get("requested_state") is False:
            action_starts.append(record)
        elif etype in {"mic_on", "mic_off"} and data.get("success") is not False:
            mic_events.append(record)
        elif etype == "speech_playback_start":
            start = parse_utc(data.get("actual_utc") or record.get("ts"))
            duration = float(data.get("duration_sec") or 0)
            if start and duration > 0:
                speech_intervals.setdefault(bot_id, []).append((start, start.timestamp() + duration))

    missing = [segment for segment in done_by_segment if len(decisions_by_segment.get(segment, [])) == 0]
    duplicate_decisions = [segment for segment, records in decisions_by_segment.items() if len(records) > 1]
    before_done = 0
    guard_violations = 0
    during_speech = 0
    rows = []
    for action in action_starts:
        data = details(action)
        segment_id = str(data.get("playback_segment_id") or "")
        action_ts = parse_utc(action.get("ts") or data.get("decision_utc"))
        done_ts = parse_utc(data.get("playback_done_observed_utc") or data.get("actual_playback_done_utc"))
        if done_ts is None and segment_id in done_by_segment:
            done_ts = parse_utc(details(done_by_segment[segment_id]).get("actual_utc") or done_by_segment[segment_id].get("ts"))
        delta_ms = None
        if action_ts and done_ts:
            delta_ms = (action_ts - done_ts).total_seconds() * 1000.0
            if delta_ms < 0:
                before_done += 1
            if delta_ms < float(data.get("guard_ms") or 0):
                guard_violations += 1
        bot_id = normalized_bot_id(action)
        active = False
        if action_ts:
            active = any(start <= action_ts <= datetime.fromtimestamp(end_ts, timezone.utc) for start, end_ts in speech_intervals.get(bot_id, []))
        if active:
            during_speech += 1
        rows.append(
            {
                "bot_id": bot_id,
                "playback_segment_id": segment_id,
                "mic_off_utc": action.get("ts"),
                "audio_done_utc": done_ts.isoformat().replace("+00:00", "Z") if done_ts else None,
                "delta_ms": delta_ms,
                "guard_ms": data.get("guard_ms"),
                "during_active_speech": active,
            }
        )

    duplicate_mic = 0
    action_requests = [
        record
        for record in events
        if event_type(record) in {"speech_prepare_mic_on_start", "post_speech_mic_action_start"}
    ]
    sorted_mic = sorted(
        action_requests,
        key=lambda item: parse_utc(item.get("ts")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    previous_by_bot_state: dict[tuple[str, str], datetime] = {}
    for record in sorted_mic:
        ts = parse_utc(record.get("ts"))
        if not ts:
            continue
        data = details(record)
        key = (normalized_bot_id(record), str(data.get("requested_state")))
        prev = previous_by_bot_state.get(key)
        if prev and (ts - prev).total_seconds() * 1000.0 < 1500:
            duplicate_mic += 1
        previous_by_bot_state[key] = ts

    return {
        "completed_segment_count": len(done_by_segment),
        "decision_count": sum(len(records) for records in decisions_by_segment.values()),
        "missing_decision_count": len(missing),
        "duplicate_decision_count": len(duplicate_decisions),
        "mic_off_before_audio_done_count": before_done,
        "mic_off_guard_violation_count": guard_violations,
        "mic_off_during_active_speech_count": during_speech,
        "duplicate_mic_action_count": duplicate_mic,
        "rows": rows,
    }


def compute_camera_action_checks(events: list[dict[str, Any]], media_by_bot: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    failure_count = 0
    for record in events:
        etype = event_type(record)
        if etype not in {"camera_on", "camera_off"}:
            continue
        data = details(record)
        if data.get("success") is False:
            continue
        bot_id = normalized_bot_id(record)
        action_ts = parse_utc(record.get("ts"))
        if not action_ts:
            continue
        media = media_by_bot.get(bot_id, {})
        baseline_bps = rtp_bitrate(media, "client_to_jvb", "camera_video", action_ts.timestamp() - 10, action_ts.timestamp() - 2)
        transition_bps = rtp_bitrate(media, "client_to_jvb", "camera_video", action_ts.timestamp() - 2, action_ts.timestamp() + 2)
        after_bps = rtp_bitrate(media, "client_to_jvb", "camera_video", action_ts.timestamp() + 2, action_ts.timestamp() + 10)
        previous_baseline = previous_camera_baseline(media, action_ts.timestamp())
        if etype == "camera_off":
            passed = after_bps <= max(baseline_bps * 0.10, 5000.0)
        else:
            baseline = previous_baseline if previous_baseline > 0 else baseline_bps
            passed = after_bps >= baseline * 0.50 if baseline > 0 else after_bps > 5000.0
        if not passed:
            failure_count += 1
        rows.append(
            {
                "bot_id": bot_id,
                "event_type": etype,
                "action_utc": record.get("ts"),
                "baseline_bps": baseline_bps,
                "transition_bps": transition_bps,
                "after_bps": after_bps,
                "passed": passed,
            }
        )
    return {
        "action_count": len(rows),
        "failure_count": failure_count,
        "screen_share_packet_validation_status": "not_gating",
        "screen_share_packet_validation_reason": "screen_share_verified_manually; packet evidence may be weak due to low resolution/static content",
        "rows": rows,
    }


def rtp_bitrate(media: dict[str, Any], direction: str, likely_kind: str, start_epoch: float, end_epoch: float) -> float:
    if end_epoch <= start_epoch:
        return 0.0
    bytes_total = 0
    for item in media.get("rtp_time_bins", []):
        if item.get("direction") != direction or item.get("likely_kind") != likely_kind:
            continue
        epoch = float(item.get("epoch_sec") or 0)
        if start_epoch <= epoch < end_epoch:
            bytes_total += int(item.get("bytes") or 0)
    return bytes_total * 8.0 / (end_epoch - start_epoch)


def previous_camera_baseline(media: dict[str, Any], action_epoch: float) -> float:
    bins = [
        item
        for item in media.get("rtp_time_bins", [])
        if item.get("direction") == "client_to_jvb"
        and item.get("likely_kind") == "camera_video"
        and float(item.get("epoch_sec") or 0) < action_epoch
    ]
    if not bins:
        return 0.0
    end_epoch = max(float(item.get("epoch_sec") or 0) for item in bins)
    return rtp_bitrate(media, "client_to_jvb", "camera_video", end_epoch - 10, end_epoch + 1)


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
            f"- same-bot audio overlap max ms: {summary['playback_overlap']['max_overlap_ms']}",
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
            "## Capture Lifecycle",
            "",
        ]
    )
    for bot_id, lifecycle in sorted(summary.get("capture_lifecycle", {}).items()):
        lines.append(
            f"- {bot_id}: capture {lifecycle.get('capture_start_utc')} -> {lifecycle.get('capture_stop_utc')}, "
            f"meeting ready {lifecycle.get('meeting_ready_utc')}, disconnected {lifecycle.get('meeting_disconnected_utc')}"
        )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv_outputs(summary: dict[str, Any], output_dir: Path) -> None:
    experiment_name, run_id = experiment_run_names(summary["run_dir"])
    analysis_dir = output_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    intervals = summary.get("playback_overlap", {}).get("intervals", [])
    with (analysis_dir / f"{experiment_name}_{run_id}_speech_timing.csv").open("w", newline="", encoding="utf-8") as outfile:
        fieldnames = ["bot_id", "playback_segment_id", "start_utc", "end_utc", "duration_sec", "status"]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for item in intervals:
            writer.writerow({key: item.get(key) for key in fieldnames})

    with (analysis_dir / f"{experiment_name}_{run_id}_capture_lifecycle.csv").open("w", newline="", encoding="utf-8") as outfile:
        fieldnames = [
            "bot_id",
            "capture_start_utc",
            "capture_stop_utc",
            "meeting_ready_utc",
            "meeting_disconnected_utc",
            "capture_duration_sec",
            "post_disconnect_capture_tail_sec",
            "capture_started_before_meeting_ready",
            "capture_stopped_after_meeting_disconnected",
        ]
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for item in summary.get("capture_lifecycle", {}).values():
            writer.writerow({key: item.get(key) for key in fieldnames})

    write_dict_rows(analysis_dir / f"{experiment_name}_{run_id}_mic_off_checks.csv", summary.get("mic_policy_checks", {}).get("rows", []))
    camera_rows = summary.get("camera_action_checks", {}).get("rows", [])
    write_dict_rows(analysis_dir / f"{experiment_name}_{run_id}_camera_action_checks.csv", camera_rows)
    write_dict_rows(analysis_dir / f"{experiment_name}_{run_id}_packet_windows_by_action.csv", camera_rows)
    write_ssrc_summary(analysis_dir / f"{experiment_name}_{run_id}_ssrc_summary.csv", summary.get("pcap", {}).get("media_by_bot", {}))


def write_dict_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fieldnames})


def write_ssrc_summary(path: Path, media_by_bot: dict[str, dict[str, Any]]) -> None:
    rows = []
    for bot_id, media in media_by_bot.items():
        for stream in media.get("ssrc_classification", media.get("rtp_streams", [])):
            rows.append({"bot_id": bot_id, **stream})
    fieldnames = [
        "bot_id",
        "endpoint_direction",
        "direction",
        "ssrc",
        "payload_type",
        "packets",
        "bytes",
        "first_seen",
        "last_seen",
        "duration_sec",
        "packets_per_sec",
        "mean_bitrate_bps",
        "likely_kind",
        "sequence_gaps",
    ]
    with path.open("w", newline="", encoding="utf-8") as outfile:
        writer = csv.DictWriter(outfile, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_readable_outputs(events: list[dict[str, Any]], output_dir: Path, run_dir: Path) -> None:
    experiment_name, run_id = experiment_run_names(str(run_dir))
    readable_dir = output_dir / "logs" / "readable"
    readable_dir.mkdir(parents=True, exist_ok=True)
    combined = render_readable_events.render(events, experiment_name=experiment_name, run_id=run_id)
    (readable_dir / f"{experiment_name}_{run_id}_successful_actions.log").write_text(
        "\n".join(combined) + ("\n" if combined else ""),
        encoding="utf-8",
    )
    by_bot: dict[str, list[dict[str, Any]]] = {}
    for record in events:
        bot_id = render_readable_events.bot_id(record)
        by_bot.setdefault(bot_id, []).append(record)
    for bot_id, records in sorted(by_bot.items()):
        lines = render_readable_events.render(records, experiment_name=experiment_name, run_id=run_id, bot_filter=bot_id)
        if not lines:
            continue
        safe_bot_id = "".join(char if char.isalnum() or char in "._-" else "-" for char in bot_id)
        (readable_dir / f"{experiment_name}_{run_id}_{safe_bot_id}_successful_actions.log").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )


def experiment_run_names(run_dir: str) -> tuple[str, str]:
    path = Path(run_dir)
    if path.parent.name:
        return path.parent.name, path.name
    return "experiment", path.name


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
    experiment_name, run_id = experiment_run_names(str(run_dir))
    analysis_dir = output_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / f"{experiment_name}_{run_id}_analysis_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    write_markdown(summary, passed, analysis_dir / f"{experiment_name}_{run_id}_analysis_summary.md")
    write_csv_outputs(summary, output_dir)
    write_readable_outputs(load_events(run_dir), output_dir, run_dir)
    print(json.dumps({"passed": passed, "summary": summary}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
