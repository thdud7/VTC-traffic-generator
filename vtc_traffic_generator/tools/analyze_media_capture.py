#!/usr/bin/env python3
"""Analyze Jitsi media traffic in a packet capture.

The analyzer intentionally separates transport evidence from media-kind
evidence. SRTP keeps the RTP base header visible, so SSRC and payload type can
be counted from UDP payload bytes. Audio/video classification is only asserted
when a WebRTC getStats or SDP mapping is supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STUN_MAGIC_COOKIE = bytes.fromhex("2112a442")
DTLS_CONTENT_TYPES = {20, 21, 22, 23, 25}


@dataclass
class StreamStats:
    direction: str
    src_ip: str
    src_port: str
    dst_ip: str
    dst_port: str
    ssrc: int
    payload_type: int
    packets: int = 0
    bytes: int = 0
    first_epoch: float | None = None
    last_epoch: float | None = None
    codec_mime_type: str | None = None
    kind: str | None = None
    source: str = "rtp-header"
    first_sequence_number: int | None = None
    last_sequence_number: int | None = None
    sequence_gap_count: int = 0
    sequence_wrap_count: int = 0
    duplicate_or_reordered_count: int = 0

    def add(self, epoch: float, byte_count: int, sequence_number: int) -> None:
        self.packets += 1
        self.bytes += byte_count
        self.first_epoch = epoch if self.first_epoch is None else min(self.first_epoch, epoch)
        self.last_epoch = epoch if self.last_epoch is None else max(self.last_epoch, epoch)
        if self.last_sequence_number is None:
            self.first_sequence_number = sequence_number
            self.last_sequence_number = sequence_number
            return

        delta = (sequence_number - self.last_sequence_number) & 0xFFFF
        if delta == 1:
            if sequence_number < self.last_sequence_number:
                self.sequence_wrap_count += 1
        elif 1 < delta < 32768:
            self.sequence_gap_count += delta - 1
            if sequence_number < self.last_sequence_number:
                self.sequence_wrap_count += 1
        else:
            self.duplicate_or_reordered_count += 1
        self.last_sequence_number = sequence_number

    @property
    def duration_sec(self) -> float:
        if self.first_epoch is None or self.last_epoch is None:
            return 0.0
        return max(0.0, self.last_epoch - self.first_epoch)

    def to_json(self) -> dict[str, Any]:
        duration = self.duration_sec
        mean_bitrate = (self.bytes * 8.0 / duration) if duration > 0 else None
        return {
            "direction": self.direction,
            "endpoint_direction": self.direction,
            "src_ip": self.src_ip,
            "src_port": int(self.src_port),
            "dst_ip": self.dst_ip,
            "dst_port": int(self.dst_port),
            "ssrc": self.ssrc,
            "payload_type": self.payload_type,
            "codec_mime_type": self.codec_mime_type,
            "kind": self.kind,
            "packets": self.packets,
            "bytes": self.bytes,
            "first_utc": epoch_to_iso(self.first_epoch),
            "last_utc": epoch_to_iso(self.last_epoch),
            "first_seen": epoch_to_iso(self.first_epoch),
            "last_seen": epoch_to_iso(self.last_epoch),
            "duration_sec": duration,
            "packets_per_sec": self.packets / duration if duration > 0 else None,
            "bytes_per_sec": self.bytes / duration if duration > 0 else None,
            "mean_bitrate_bps": mean_bitrate,
            "likely_kind": likely_kind(self.direction, self.kind, self.payload_type),
            "sequence_gaps": self.sequence_gap_count,
            "sequence_wraps": self.sequence_wrap_count,
            "duplicate_or_reordered_packets": self.duplicate_or_reordered_count,
            "first_sequence_number": self.first_sequence_number,
            "last_sequence_number": self.last_sequence_number,
            "source": self.source,
        }


@dataclass
class Analysis:
    pcap: Path
    client_ip: str | None
    jvb_ip: str | None
    jvb_port: int
    packet_count: int = 0
    first_epoch: float | None = None
    last_epoch: float | None = None
    udp_5tuples: dict[str, dict[str, Any]] = field(default_factory=dict)
    protocol_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    streams: dict[tuple[str, str, str, str, str, int, int], StreamStats] = field(default_factory=dict)
    codec_by_ssrc: dict[int, dict[str, Any]] = field(default_factory=dict)
    codec_by_pt: dict[int, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    rtp_time_bins: dict[tuple[str, str, int], dict[str, Any]] = field(default_factory=dict)
    capinfos: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def span_sec(self) -> float:
        if self.first_epoch is None or self.last_epoch is None:
            return 0.0
        return max(0.0, self.last_epoch - self.first_epoch)

    def direction_for(self, src_ip: str, dst_ip: str, dst_port: str) -> str:
        if self.client_ip and self.jvb_ip:
            if src_ip == self.client_ip and dst_ip == self.jvb_ip and int(dst_port) == self.jvb_port:
                return "client_to_jvb"
            if src_ip == self.jvb_ip and dst_ip == self.client_ip:
                return "jvb_to_client"
        return "other"

    def to_json(self) -> dict[str, Any]:
        streams = sorted(
            (stream.to_json() for stream in self.streams.values()),
            key=lambda item: (item["direction"], item["ssrc"], item["payload_type"]),
        )
        audio_streams = [
            stream
            for stream in streams
            if stream.get("kind") == "audio" or stream.get("likely_kind") in {"audio", "remote_audio"}
        ]
        video_streams = [
            stream
            for stream in streams
            if stream.get("kind") == "video" or stream.get("likely_kind") in {"camera_video", "remote_video"}
        ]
        media_udp = [
            value
            for value in self.udp_5tuples.values()
            if (not self.jvb_ip or value["src_ip"] == self.jvb_ip or value["dst_ip"] == self.jvb_ip)
            and (int(value["src_port"]) == self.jvb_port or int(value["dst_port"]) == self.jvb_port)
        ]
        basic_validation, fail_reasons = self.basic_webrtc_validation(streams, media_udp)
        verdict = "PASS" if all(basic_validation.values()) else "FAIL"

        return {
            "pcap": str(self.pcap),
            "client_ip": self.client_ip,
            "jvb_ip": self.jvb_ip,
            "jvb_port": self.jvb_port,
            "capture_start_utc": epoch_to_iso(self.first_epoch),
            "capture_end_utc": epoch_to_iso(self.last_epoch),
            "packet_span_sec": self.span_sec,
            "packet_count": self.packet_count,
            "capinfos": self.capinfos,
            "protocol_counts": dict(self.protocol_counts),
            "udp_5tuples": sorted(self.udp_5tuples.values(), key=lambda item: item["packets"], reverse=True),
            "rtp_streams": streams,
            "ssrc_classification": streams,
            "rtp_time_bins": sorted(self.rtp_time_bins.values(), key=lambda item: (item["epoch_sec"], item["direction"], item["likely_kind"])),
            "audio_streams": audio_streams,
            "video_streams": video_streams,
            "events_loaded": len(self.events),
            "basic_webrtc_validation": basic_validation,
            "final_verdict": verdict,
            "fail_reasons": fail_reasons,
            "errors": self.errors,
        }

    def basic_webrtc_validation(
        self,
        streams: list[dict[str, Any]],
        media_udp: list[dict[str, Any]],
    ) -> tuple[dict[str, bool], list[str]]:
        protocol_counts = dict(self.protocol_counts)
        lifecycle_ok = self.pcap_covers_full_lifecycle()
        c2j_streams = [
            stream for stream in streams if stream.get("direction") == "client_to_jvb" and int(stream.get("packets") or 0) > 0
        ]
        j2c_streams = [
            stream for stream in streams if stream.get("direction") == "jvb_to_client" and int(stream.get("packets") or 0) > 0
        ]
        required_duration = min(max(30.0, self.span_sec * 0.5), self.span_sec * 0.9) if self.span_sec else 30.0
        longest_c2j = max([float(stream.get("duration_sec") or 0) for stream in c2j_streams] or [0.0])
        longest_j2c = max([float(stream.get("duration_sec") or 0) for stream in j2c_streams] or [0.0])
        long_streams = [
            stream
            for stream in streams
            if float(stream.get("duration_sec") or 0) >= 10.0 and int(stream.get("packets") or 0) >= 50
        ]
        major_gap_streams = [
            stream
            for stream in long_streams
            if int(stream.get("sequence_gaps") or 0) > max(20, int(stream.get("packets") or 0) * 0.02)
        ]
        checks = {
            "pcap_covers_full_lifecycle": lifecycle_ok,
            "jvb_udp_flow_exists": bool(media_udp),
            "stun_exists": int(protocol_counts.get("stun", 0)) > 0,
            "dtls_exists": int(protocol_counts.get("dtls", 0)) > 0,
            "rtp_srtp_like_exists": int(protocol_counts.get("rtp", 0)) > 0,
            "rtcp_exists": int(protocol_counts.get("rtcp", 0)) > 0,
            "bidirectional_rtp_exists": bool(c2j_streams and j2c_streams),
            "persistent_media_flow": longest_c2j >= required_duration and longest_j2c >= required_duration,
            "major_sequence_gaps_absent": bool(long_streams) and not major_gap_streams,
        }
        reasons = []
        reason_text = {
            "pcap_covers_full_lifecycle": "The PCAP does not cover capture start, meeting-ready, disconnect, and capture stop lifecycle evidence.",
            "jvb_udp_flow_exists": "No UDP flow involving the configured JVB media port was found.",
            "stun_exists": "No STUN/ICE traffic was found.",
            "dtls_exists": "No DTLS handshake/application packets were found.",
            "rtp_srtp_like_exists": "No RTP/SRTP-like packets were parsed from UDP payloads.",
            "rtcp_exists": "No RTCP packets were found.",
            "bidirectional_rtp_exists": "No bidirectional RTP/SRTP-like media streams were found.",
            "persistent_media_flow": "RTP/SRTP-like media did not persist in both directions for the required capture span.",
            "major_sequence_gaps_absent": "No long RTP stream without major sequence gaps was found.",
        }
        for key, passed in checks.items():
            if not passed:
                reasons.append(reason_text[key])
        if major_gap_streams:
            reasons.append(
                "Major sequence gaps: "
                + ", ".join(
                    f"{stream.get('direction')} ssrc={stream.get('ssrc')} gaps={stream.get('sequence_gaps')}"
                    for stream in major_gap_streams[:5]
                )
            )
        return checks, reasons

    def pcap_covers_full_lifecycle(self) -> bool:
        if not self.events or self.first_epoch is None or self.last_epoch is None:
            return False

        capture_start = first_event_epoch(self.events, {"capture_started", "packet_capture_start"})
        meeting_ready = first_event_epoch(self.events, {"meeting_join_ready"})
        meeting_end = last_event_epoch(
            self.events,
            {"meeting_disconnected", "terminal_disconnect", "meeting_end"},
        )
        capture_stop = last_event_epoch(self.events, {"capture_stopped", "packet_capture_done"})
        if None in (capture_start, meeting_ready, meeting_end, capture_stop):
            return False
        return self.first_epoch <= meeting_ready and self.last_epoch >= meeting_end and capture_start <= meeting_ready <= meeting_end <= capture_stop


def epoch_to_iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def iso_to_epoch(value: Any) -> float | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def record_epoch(record: dict[str, Any]) -> float | None:
    data = record.get("details")
    details = data if isinstance(data, dict) else {}
    return iso_to_epoch(record.get("ts") or details.get("actual_utc") or details.get("capture_utc"))


def record_event_type(record: dict[str, Any]) -> str:
    return str(record.get("event_type") or record.get("event") or "")


def first_event_epoch(events: list[dict[str, Any]], names: set[str]) -> float | None:
    epochs = [epoch for record in events if record_event_type(record) in names for epoch in [record_epoch(record)] if epoch is not None]
    return min(epochs) if epochs else None


def last_event_epoch(events: list[dict[str, Any]], names: set[str]) -> float | None:
    epochs = [epoch for record in events if record_event_type(record) in names for epoch in [record_epoch(record)] if epoch is not None]
    return max(epochs) if epochs else None


def likely_kind(direction: str, kind: str | None, payload_type: int) -> str:
    if kind == "audio" or payload_type in {0, 8, 9, 111}:
        return "remote_audio" if direction == "jvb_to_client" else "audio"
    if kind == "video":
        return "remote_video" if direction == "jvb_to_client" else "camera_video"
    if 96 <= int(payload_type) <= 127:
        return "remote_video" if direction == "jvb_to_client" else "camera_video"
    return "unknown"


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def load_stats_mappings(paths: Iterable[Path]) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    codecs: dict[str, dict[str, Any]] = {}
    outbound: list[dict[str, Any]] = []
    codec_by_ssrc: dict[int, dict[str, Any]] = {}
    codec_by_pt: dict[int, dict[str, Any]] = {}

    for path in paths:
        if not path or not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            for stat in iter_stats_objects(record):
                stat_type = stat.get("type")
                stat_id = str(stat.get("id") or "")
                if stat_type == "codec" or stat_id.startswith("RTCCodec"):
                    codecs[stat_id] = stat
                    payload_type = stat.get("payloadType")
                    if payload_type is not None:
                        try:
                            codec_by_pt[int(payload_type)] = stat
                        except (TypeError, ValueError):
                            pass
                elif stat_type == "outbound-rtp":
                    outbound.append(stat)

    for stat in outbound:
        try:
            ssrc = int(stat.get("ssrc"))
        except (TypeError, ValueError):
            continue
        codec = codecs.get(str(stat.get("codecId") or ""), {})
        mapping = {
            "kind": stat.get("kind") or stat.get("mediaType"),
            "codec_mime_type": codec.get("mimeType") or stat.get("mimeType"),
            "payload_type": codec.get("payloadType"),
            "stats_id": stat.get("id"),
            "codec_id": stat.get("codecId"),
            "track_identifier": stat.get("trackIdentifier"),
        }
        codec_by_ssrc[ssrc] = mapping
        if mapping.get("payload_type") is not None:
            try:
                codec_by_pt[int(mapping["payload_type"])] = mapping
            except (TypeError, ValueError):
                pass
    return codec_by_ssrc, codec_by_pt


def iter_stats_objects(record: Any) -> Iterable[dict[str, Any]]:
    if isinstance(record, dict):
        if "stats" in record:
            yield from iter_stats_objects(record["stats"])
        elif "reports" in record:
            yield from iter_stats_objects(record["reports"])
        elif "values" in record and isinstance(record["values"], list):
            yield from iter_stats_objects(record["values"])
        elif "type" in record or "ssrc" in record:
            yield record
        else:
            for value in record.values():
                if isinstance(value, (dict, list)):
                    yield from iter_stats_objects(value)
    elif isinstance(record, list):
        for value in record:
            yield from iter_stats_objects(value)


def load_events(paths: Iterable[Path]) -> list[dict[str, Any]]:
    events = []
    for path in paths:
        if not path or not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def parse_capinfos(path: Path) -> dict[str, Any]:
    result = run_command(["capinfos", "-M", str(path)])
    data: dict[str, Any] = {"returncode": result.returncode}
    if result.returncode != 0:
        data["stderr"] = result.stderr.strip()
        return data
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def analyze(args: argparse.Namespace) -> Analysis:
    pcap = Path(args.pcap).expanduser().resolve()
    analysis = Analysis(pcap=pcap, client_ip=args.client_ip, jvb_ip=args.jvb_ip, jvb_port=int(args.jvb_port))
    stats_paths = [Path(path).expanduser() for path in args.stats_jsonl or []]
    event_paths = [Path(path).expanduser() for path in args.events_jsonl or []]
    analysis.codec_by_ssrc, analysis.codec_by_pt = load_stats_mappings(stats_paths)
    analysis.events = load_events(event_paths)
    analysis.capinfos = parse_capinfos(pcap)

    command = [
        "tshark",
        "-r",
        str(pcap),
        "-T",
        "fields",
        "-E",
        "separator=\t",
        "-e",
        "frame.time_epoch",
        "-e",
        "ip.src",
        "-e",
        "udp.srcport",
        "-e",
        "ip.dst",
        "-e",
        "udp.dstport",
        "-e",
        "udp.length",
        "-e",
        "udp.payload",
        "-Y",
        "udp",
    ]
    result = run_command(command)
    if result.returncode != 0:
        analysis.errors.append(result.stderr.strip())
        return analysis

    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        epoch_text, src_ip, src_port, dst_ip, dst_port, udp_len_text, payload_hex = fields[:7]
        try:
            epoch = float(epoch_text)
        except ValueError:
            continue
        analysis.packet_count += 1
        analysis.first_epoch = epoch if analysis.first_epoch is None else min(analysis.first_epoch, epoch)
        analysis.last_epoch = epoch if analysis.last_epoch is None else max(analysis.last_epoch, epoch)
        try:
            udp_len = int(udp_len_text)
        except ValueError:
            udp_len = 0

        tuple_key = f"{src_ip}:{src_port}->{dst_ip}:{dst_port}"
        tuple_data = analysis.udp_5tuples.setdefault(
            tuple_key,
            {
                "src_ip": src_ip,
                "src_port": int(src_port or 0),
                "dst_ip": dst_ip,
                "dst_port": int(dst_port or 0),
                "direction": analysis.direction_for(src_ip, dst_ip, dst_port or "0"),
                "packets": 0,
                "bytes": 0,
                "first_utc": None,
                "last_utc": None,
            },
        )
        tuple_data["packets"] += 1
        tuple_data["bytes"] += udp_len
        tuple_data["_first_epoch"] = epoch if "_first_epoch" not in tuple_data else min(tuple_data["_first_epoch"], epoch)
        tuple_data["_last_epoch"] = epoch if "_last_epoch" not in tuple_data else max(tuple_data["_last_epoch"], epoch)

        payload = parse_payload_hex(payload_hex)
        protocol = classify_udp_payload(payload)
        analysis.protocol_counts[protocol] += 1
        if protocol != "rtp":
            continue
        rtp = parse_rtp_header(payload)
        if not rtp:
            continue
        direction = analysis.direction_for(src_ip, dst_ip, dst_port or "0")
        key = (direction, src_ip, src_port, dst_ip, dst_port, rtp["ssrc"], rtp["payload_type"])
        stream = analysis.streams.get(key)
        if stream is None:
            mapping = analysis.codec_by_ssrc.get(rtp["ssrc"]) or analysis.codec_by_pt.get(rtp["payload_type"]) or {}
            stream = StreamStats(
                direction=direction,
                src_ip=src_ip,
                src_port=src_port,
                dst_ip=dst_ip,
                dst_port=dst_port,
                ssrc=rtp["ssrc"],
                payload_type=rtp["payload_type"],
                codec_mime_type=mapping.get("codec_mime_type") or mapping.get("mimeType"),
                kind=mapping.get("kind"),
                source="rtp-header+stats" if mapping else "rtp-header",
            )
            analysis.streams[key] = stream
        stream.add(epoch, udp_len, rtp["sequence_number"])
        bin_kind = likely_kind(direction, stream.kind, rtp["payload_type"])
        epoch_sec = int(epoch)
        bin_key = (direction, bin_kind, epoch_sec)
        bin_data = analysis.rtp_time_bins.setdefault(
            bin_key,
            {
                "epoch_sec": epoch_sec,
                "utc": epoch_to_iso(float(epoch_sec)),
                "direction": direction,
                "likely_kind": bin_kind,
                "packets": 0,
                "bytes": 0,
            },
        )
        bin_data["packets"] += 1
        bin_data["bytes"] += udp_len

    for tuple_data in analysis.udp_5tuples.values():
        tuple_data["first_utc"] = epoch_to_iso(tuple_data.pop("_first_epoch", None))
        tuple_data["last_utc"] = epoch_to_iso(tuple_data.pop("_last_epoch", None))

    return analysis


def parse_payload_hex(value: str) -> bytes:
    value = value.replace(":", "").strip()
    if not value:
        return b""
    try:
        return bytes.fromhex(value)
    except ValueError:
        return b""


def classify_udp_payload(payload: bytes) -> str:
    if len(payload) < 2:
        return "short"
    if len(payload) >= 20 and payload[0] & 0xC0 == 0 and payload[4:8] == STUN_MAGIC_COOKIE:
        return "stun"
    if payload[0] in DTLS_CONTENT_TYPES and len(payload) >= 3 and payload[1] in (0xFE, 0x03):
        return "dtls"
    if payload[0] & 0xC0 == 0x80 and 192 <= payload[1] <= 223:
        return "rtcp"
    if parse_rtp_header(payload):
        return "rtp"
    return "unknown"


def parse_rtp_header(payload: bytes) -> dict[str, int] | None:
    if len(payload) < 12:
        return None
    if payload[0] & 0xC0 != 0x80:
        return None
    cc = payload[0] & 0x0F
    header_len = 12 + (cc * 4)
    if len(payload) < header_len:
        return None
    payload_type = payload[1] & 0x7F
    ssrc = int.from_bytes(payload[8:12], "big")
    if ssrc == 0:
        return None
    return {
        "payload_type": payload_type,
        "sequence_number": int.from_bytes(payload[2:4], "big"),
        "timestamp": int.from_bytes(payload[4:8], "big"),
        "ssrc": ssrc,
    }


def write_outputs(analysis: Analysis, output_json: Path | None, output_md: Path | None) -> tuple[Path, Path]:
    json_path = output_json or analysis.pcap.with_suffix(".media-analysis.json")
    md_path = output_md or analysis.pcap.with_suffix(".media-analysis.md")
    result = analysis.to_json()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def render_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Media Capture Analysis",
        "",
        f"- PCAP: `{result['pcap']}`",
        f"- Capture UTC: {result.get('capture_start_utc')} to {result.get('capture_end_utc')}",
        f"- Packet span: {result.get('packet_span_sec'):.3f} sec",
        f"- UDP packet count analyzed: {result.get('packet_count')}",
        f"- Verdict: **{result.get('final_verdict')}**",
        "",
        "## Protocol Counts",
        "",
    ]
    for key, value in sorted(result.get("protocol_counts", {}).items()):
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Basic WebRTC Validation", ""])
    for key, value in sorted(result.get("basic_webrtc_validation", {}).items()):
        lines.append(f"- {'PASS' if value else 'FAIL'} `{key}`")
    lines.extend(["", "## Top UDP 5-Tuples", ""])
    for item in result.get("udp_5tuples", [])[:12]:
        lines.append(
            f"- {item['src_ip']}:{item['src_port']} -> {item['dst_ip']}:{item['dst_port']} "
            f"({item['direction']}): {item['packets']} packets, {item['bytes']} bytes"
        )
    lines.extend(["", "## RTP Streams", ""])
    streams = result.get("rtp_streams", [])
    if not streams:
        lines.append("No RTP-like streams were parsed from UDP payloads.")
    else:
        lines.append("| Direction | SSRC | PT | Likely kind | Codec | Packets | Bytes | Duration s | Mean bps | Gaps |")
        lines.append("| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |")
        for stream in streams:
            lines.append(
                f"| {stream['direction']} | {stream['ssrc']} | {stream['payload_type']} | "
                f"{stream.get('likely_kind') or stream.get('kind') or 'unknown'} | {stream.get('codec_mime_type') or 'unknown'} | "
                f"{stream['packets']} | {stream['bytes']} | {stream['duration_sec']:.3f} | "
                f"{stream.get('mean_bitrate_bps') or 0:.1f} | {stream.get('sequence_gaps') or 0} |"
            )
    if result.get("fail_reasons"):
        lines.extend(["", "## Fail / Inconclusive Reasons", ""])
        for reason in result["fail_reasons"]:
            lines.append(f"- {reason}")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcap", help="Input PCAPNG file")
    parser.add_argument("--stats-jsonl", action="append", help="WebRTC getStats JSONL file", default=[])
    parser.add_argument("--events-jsonl", action="append", help="Event JSONL file", default=[])
    parser.add_argument("--client-ip", help="Client private IP")
    parser.add_argument("--jvb-ip", help="Jitsi Videobridge private IP")
    parser.add_argument("--jvb-port", type=int, default=10000)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-md", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    analysis = analyze(args)
    json_path, md_path = write_outputs(analysis, args.output_json, args.output_md)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0 if not analysis.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
