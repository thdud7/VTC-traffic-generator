from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .event_log import emit_event, get_bot_id, get_service_name


DEFAULT_OUTPUT_DIR = "/tmp/vtc-captures"
DEFAULT_BINARY_DIRS = ("/usr/local/bin", "/usr/bin", "/usr/sbin", "/snap/bin")


class PacketCaptureSession:
    def __init__(
        self,
        config: Mapping[str, Any],
        enabled: bool,
        interface: str | None = None,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        capture_filter: str | None = None,
        display_filter: str | None = None,
        dumpcap_path: str = "dumpcap",
        tshark_path: str = "tshark",
    ):
        self.config = config
        self.enabled = enabled
        self.interface = interface
        self.output_dir = Path(output_dir).expanduser()
        self.capture_filter = capture_filter
        self.display_filter = display_filter
        self.dumpcap_path = dumpcap_path
        self.tshark_path = tshark_path
        self.process: subprocess.Popen | None = None
        self.pcapng_path: Path | None = None
        self.analysis_path: Path | None = None
        self.filtered_pcapng_path: Path | None = None
        self.filtered_pcapng_filter: str | None = None
        self.media_analysis_json_path: Path | None = None
        self.media_analysis_md_path: Path | None = None
        self.metadata_path: Path | None = None
        self.started_at_monotonic: float | None = None
        self.stopped_at_monotonic: float | None = None
        self.stop_reason: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "PacketCaptureSession":
        packet_capture = config.get("packet_capture", {})
        if not isinstance(packet_capture, Mapping):
            packet_capture = {}

        enabled = bool(packet_capture.get("enabled", False))
        return cls(
            config=config,
            enabled=enabled,
            interface=packet_capture.get("interface"),
            output_dir=str(packet_capture.get("output_dir") or DEFAULT_OUTPUT_DIR),
            capture_filter=packet_capture.get("capture_filter"),
            display_filter=packet_capture.get("display_filter"),
            dumpcap_path=str(packet_capture.get("dumpcap_path") or "dumpcap"),
            tshark_path=str(packet_capture.get("tshark_path") or "tshark"),
        )

    def start(self) -> None:
        if not self.enabled:
            return

        with self._lock:
            if self.process and self.process.poll() is None:
                emit_event(
                    self.config,
                    "packet_capture_start_skipped",
                    {
                        "reason": "already_running",
                        "pcapng_path": str(self.pcapng_path) if self.pcapng_path else None,
                        "pid": self.process.pid,
                    },
                )
                return

        try:
            dumpcap_binary = self._resolve_binary(self.dumpcap_path)
            if not dumpcap_binary:
                emit_event(
                    self.config,
                    "packet_capture_error",
                    {
                        "error": "dumpcap not found",
                        "dumpcap_path": self.dumpcap_path,
                        "path": os.environ.get("PATH"),
                    },
                )
                return

            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.pcapng_path = self.output_dir / f"{self._file_stem()}.pcapng"
            self.config["_packet_capture_path"] = str(self.pcapng_path)
            self.started_at_monotonic = time.monotonic()

            command = [dumpcap_binary, "-q", "-w", str(self.pcapng_path)]
            if self.interface:
                command.extend(["-i", self.interface])
            if self.capture_filter:
                command.extend(["-f", self.capture_filter])

            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            emit_event(
                self.config,
                "packet_capture_start",
                {
                    "pcapng_path": str(self.pcapng_path),
                    "interface": self.interface,
                    "capture_filter": self.capture_filter,
                    "pid": self.process.pid,
                },
            )
            emit_event(
                self.config,
                "capture_started",
                {
                    "pcapng_path": str(self.pcapng_path),
                    "interface": self.interface,
                    "capture_filter": self.capture_filter,
                    "pid": self.process.pid,
                },
            )
        except Exception as exc:
            emit_event(
                self.config,
                "packet_capture_error",
                {"error": str(exc)},
            )

    def stop_and_analyze(self) -> None:
        self.stop_and_analyze_with_reason("unspecified")

    def stop_and_analyze_with_reason(self, reason: str = "unspecified") -> None:
        if not self.enabled:
            return

        try:
            self._stop(reason)
            self._analyze()
        except Exception as exc:
            emit_event(
                self.config,
                "packet_capture_error",
                {"error": str(exc)},
            )

    def _stop(self, reason: str) -> None:
        with self._lock:
            if not self.process:
                emit_event(
                    self.config,
                    "packet_capture_stop_skipped",
                    {"reason": "not_started", "stop_reason": reason},
                )
                return
            process = self.process
            self.process = None

        self.stop_reason = reason
        if process.poll() is None:
            self._emit_capture_tail_marker(reason)
            process.send_signal(signal.SIGTERM)
            try:
                _, stderr = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                _, stderr = process.communicate(timeout=10)
        else:
            _, stderr = process.communicate(timeout=10)
        self.stopped_at_monotonic = time.monotonic()

        emit_event(
            self.config,
            "packet_capture_done",
            {
                "pcapng_path": str(self.pcapng_path) if self.pcapng_path else None,
                "returncode": process.returncode,
                "stop_reason": reason,
                "duration_monotonic_sec": (
                    self.stopped_at_monotonic - self.started_at_monotonic
                    if self.started_at_monotonic is not None and self.stopped_at_monotonic is not None
                    else None
                ),
                "stderr": (stderr or "").strip(),
            },
        )
        emit_event(
            self.config,
            "capture_stopped",
            {
                "pcapng_path": str(self.pcapng_path) if self.pcapng_path else None,
                "returncode": process.returncode,
                "stop_reason": reason,
                "duration_monotonic_sec": (
                    self.stopped_at_monotonic - self.started_at_monotonic
                    if self.started_at_monotonic is not None and self.stopped_at_monotonic is not None
                    else None
                ),
            },
        )

    def _emit_capture_tail_marker(self, reason: str) -> None:
        packet_capture = self.config.get("packet_capture", {})
        if not isinstance(packet_capture, Mapping):
            packet_capture = {}
        if packet_capture.get("tail_marker_enabled", True) is False:
            return

        host = str(packet_capture.get("tail_marker_host") or "127.0.0.1")
        port = int(packet_capture.get("tail_marker_port") or 9)
        payload = (
            f"vtc-capture-tail-marker "
            f"{self.config.get('experiment_id') or ''} "
            f"{self.config.get('execution_id') or ''} "
            f"{self.config.get('bot_name') or get_bot_id(self.config)} "
            f"{reason}"
        ).encode("utf-8", errors="replace")[:512]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload, (host, port))
            emit_event(
                self.config,
                "packet_capture_tail_marker",
                {"host": host, "port": port, "bytes": len(payload), "stop_reason": reason, "success": True},
            )
        except Exception as exc:
            emit_event(
                self.config,
                "packet_capture_tail_marker",
                {"host": host, "port": port, "stop_reason": reason, "success": False, "error": str(exc)},
            )

    def _analyze(self) -> None:
        if not self.pcapng_path or not self.pcapng_path.exists():
            return

        tshark_binary = self._resolve_binary(self.tshark_path)
        if not tshark_binary:
            emit_event(
                self.config,
                "packet_analysis_error",
                {
                    "error": "tshark not found",
                    "tshark_path": self.tshark_path,
                    "path": os.environ.get("PATH"),
                },
            )
            return

        self.analysis_path = self.pcapng_path.with_suffix(".analysis.txt")
        command = [
            tshark_binary,
            "-r",
            str(self.pcapng_path),
            "-q",
            "-z",
            "io,stat,1",
            "-z",
            "endpoints,ip",
            "-z",
            "conv,ip",
        ]
        if self.display_filter:
            command.extend(["-Y", self.display_filter])

        result = subprocess.run(command, capture_output=True, text=True)
        self.analysis_path.write_text(result.stdout, encoding="utf-8")
        filtered_result = self._write_filtered_pcapng(tshark_binary)
        media_result = self._analyze_media()
        self._write_metadata(result, filtered_result)

        emit_event(
            self.config,
            "packet_analysis_done",
            {
                "pcapng_path": str(self.pcapng_path),
                "analysis_path": str(self.analysis_path),
                "filtered_pcapng_path": str(self.filtered_pcapng_path) if self.filtered_pcapng_path else None,
                "filtered_pcapng_filter": self.filtered_pcapng_filter,
                "media_analysis_json_path": str(self.media_analysis_json_path) if self.media_analysis_json_path else None,
                "media_analysis_md_path": str(self.media_analysis_md_path) if self.media_analysis_md_path else None,
                "display_filter": self.display_filter,
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
                "filtered_returncode": filtered_result.returncode if filtered_result else None,
                "filtered_stderr": filtered_result.stderr.strip() if filtered_result else None,
                "media_returncode": media_result.returncode if media_result else None,
                "media_stderr": media_result.stderr.strip() if media_result else None,
            },
        )

    def _write_filtered_pcapng(self, tshark_binary: str) -> subprocess.CompletedProcess[str] | None:
        if not self.pcapng_path:
            return None

        packet_capture = self.config.get("packet_capture", {})
        if not isinstance(packet_capture, Mapping):
            packet_capture = {}

        if not bool(packet_capture.get("filtered_pcapng_enabled", False)):
            return None

        display_filter = self._filtered_pcapng_display_filter(packet_capture)
        self.filtered_pcapng_filter = display_filter
        self.filtered_pcapng_path = self.pcapng_path.with_suffix(".jitsi-only.pcapng")
        command = [
            tshark_binary,
            "-r",
            str(self.pcapng_path),
            "-Y",
            display_filter,
            "-w",
            str(self.filtered_pcapng_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        emit_event(
            self.config,
            "packet_filtered_capture_done" if result.returncode == 0 else "packet_filtered_capture_failed",
            {
                "pcapng_path": str(self.pcapng_path),
                "filtered_pcapng_path": str(self.filtered_pcapng_path),
                "filtered_pcapng_filter": display_filter,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
        return result

    def _filtered_pcapng_display_filter(self, packet_capture: Mapping[str, Any]) -> str:
        configured_filter = packet_capture.get("filtered_pcapng_filter")
        if configured_filter:
            return str(configured_filter)

        client_ip = str(packet_capture.get("client_ip") or "").strip()
        jvb_ip = str(packet_capture.get("jvb_ip") or "").strip()
        jvb_port = str(packet_capture.get("jvb_port") or "10000").strip()
        terms = []
        if client_ip:
            terms.append(f"ip.addr == {client_ip}")
        if jvb_ip:
            terms.append(f"ip.addr == {jvb_ip}")
        terms.append(f"udp.port == {jvb_port}")
        return " && ".join(terms)

    def _analyze_media(self) -> subprocess.CompletedProcess[str] | None:
        if not self.pcapng_path:
            return None

        analyzer_path = Path(__file__).resolve().parents[1] / "tools" / "analyze_media_capture.py"
        if not analyzer_path.exists():
            return None

        packet_capture = self.config.get("packet_capture", {})
        if not isinstance(packet_capture, Mapping):
            packet_capture = {}

        self.media_analysis_json_path = self.pcapng_path.with_suffix(".media-analysis.json")
        self.media_analysis_md_path = self.pcapng_path.with_suffix(".media-analysis.md")
        command = [
            "python3",
            str(analyzer_path),
            str(self.pcapng_path),
            "--output-json",
            str(self.media_analysis_json_path),
            "--output-md",
            str(self.media_analysis_md_path),
        ]
        if packet_capture.get("client_ip"):
            command.extend(["--client-ip", str(packet_capture["client_ip"])])
        if packet_capture.get("jvb_ip"):
            command.extend(["--jvb-ip", str(packet_capture["jvb_ip"])])
        if packet_capture.get("jvb_port"):
            command.extend(["--jvb-port", str(packet_capture["jvb_port"])])

        event_log_path = self._event_log_path()
        if event_log_path and event_log_path.exists():
            command.extend(["--events-jsonl", str(event_log_path)])

        result = subprocess.run(command, capture_output=True, text=True)
        emit_event(
            self.config,
            "packet_media_analysis_done" if result.returncode == 0 else "packet_media_analysis_failed",
            {
                "pcapng_path": str(self.pcapng_path),
                "media_analysis_json_path": str(self.media_analysis_json_path),
                "media_analysis_md_path": str(self.media_analysis_md_path),
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
        return result

    def _write_metadata(
        self,
        result: subprocess.CompletedProcess[str],
        filtered_result: subprocess.CompletedProcess[str] | None = None,
    ) -> None:
        if not self.pcapng_path:
            return

        metadata_path = self.pcapng_path.with_suffix(".metadata.json")
        self.metadata_path = metadata_path
        metadata = {
            "pcapng_path": str(self.pcapng_path),
            "analysis_path": str(self.analysis_path) if self.analysis_path else None,
            "filtered_pcapng_path": str(self.filtered_pcapng_path) if self.filtered_pcapng_path else None,
            "filtered_pcapng_filter": self.filtered_pcapng_filter,
            "media_analysis_json_path": str(self.media_analysis_json_path) if self.media_analysis_json_path else None,
            "media_analysis_md_path": str(self.media_analysis_md_path) if self.media_analysis_md_path else None,
            "bot_id": get_bot_id(self.config),
            "service": get_service_name(self.config),
            "interface": self.interface,
            "capture_filter": self.capture_filter,
            "display_filter": self.display_filter,
            "execution_id": str(self.config.get("execution_id") or ""),
            "experiment_id": str(self.config.get("experiment_id") or ""),
            "git_sha": str(self.config.get("git_sha") or ""),
            "config_sha256": str(self.config.get("config_sha256") or ""),
            "stop_reason": self.stop_reason,
            "duration_monotonic_sec": (
                self.stopped_at_monotonic - self.started_at_monotonic
                if self.started_at_monotonic is not None and self.stopped_at_monotonic is not None
                else None
            ),
            "tshark_returncode": result.returncode,
            "tshark_stderr": result.stderr.strip(),
            "filtered_pcapng_returncode": filtered_result.returncode if filtered_result else None,
            "filtered_pcapng_stderr": filtered_result.stderr.strip() if filtered_result else None,
        }
        metadata_path.write_text(json.dumps(metadata, sort_keys=True, indent=2), encoding="utf-8")

    def _event_log_path(self) -> Path | None:
        adapter_config = self.config.get("adapter_config", {})
        if isinstance(adapter_config, Mapping) and adapter_config.get("event_log_path"):
            return Path(str(adapter_config["event_log_path"])).expanduser()
        if self.config.get("event_log_path"):
            return Path(str(self.config["event_log_path"])).expanduser()
        return None

    def _file_stem(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        execution_id = self._sanitize(str(self.config.get("execution_id") or ""))
        service = self._sanitize(get_service_name(self.config))
        bot_id = self._sanitize(get_bot_id(self.config))
        parts = [part for part in (execution_id, timestamp, service, bot_id) if part]
        return "-".join(parts)

    def _resolve_binary(self, configured_path: str) -> str | None:
        resolved_path = shutil.which(configured_path)
        if resolved_path:
            return resolved_path

        configured = Path(configured_path)
        if configured.is_absolute() and configured.exists() and os.access(configured, os.X_OK):
            return str(configured)

        if configured.parent == Path("."):
            for binary_dir in DEFAULT_BINARY_DIRS:
                candidate = Path(binary_dir) / configured.name
                if candidate.exists() and os.access(candidate, os.X_OK):
                    return str(candidate)

        return None

    def _sanitize(self, value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        return sanitized or "unknown"
