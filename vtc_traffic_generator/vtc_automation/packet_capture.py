from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .event_log import emit_event, get_bot_id, get_service_name


DEFAULT_OUTPUT_DIR = "/tmp/vtc-captures"


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

        try:
            dumpcap_binary = shutil.which(self.dumpcap_path)
            if not dumpcap_binary:
                emit_event(
                    self.config,
                    "packet_capture_error",
                    {"error": "dumpcap not found", "dumpcap_path": self.dumpcap_path},
                )
                return

            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.pcapng_path = self.output_dir / f"{self._file_stem()}.pcapng"

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
        except Exception as exc:
            emit_event(
                self.config,
                "packet_capture_error",
                {"error": str(exc)},
            )

    def stop_and_analyze(self) -> None:
        if not self.enabled:
            return

        try:
            self._stop()
            self._analyze()
        except Exception as exc:
            emit_event(
                self.config,
                "packet_capture_error",
                {"error": str(exc)},
            )

    def _stop(self) -> None:
        if not self.process:
            return

        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                _, stderr = self.process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                _, stderr = self.process.communicate(timeout=10)
        else:
            _, stderr = self.process.communicate(timeout=10)

        emit_event(
            self.config,
            "packet_capture_done",
            {
                "pcapng_path": str(self.pcapng_path) if self.pcapng_path else None,
                "returncode": self.process.returncode,
                "stderr": (stderr or "").strip(),
            },
        )

    def _analyze(self) -> None:
        if not self.pcapng_path or not self.pcapng_path.exists():
            return

        tshark_binary = shutil.which(self.tshark_path)
        if not tshark_binary:
            emit_event(
                self.config,
                "packet_analysis_error",
                {"error": "tshark not found", "tshark_path": self.tshark_path},
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
        self._write_metadata(result)

        emit_event(
            self.config,
            "packet_analysis_done",
            {
                "pcapng_path": str(self.pcapng_path),
                "analysis_path": str(self.analysis_path),
                "display_filter": self.display_filter,
                "returncode": result.returncode,
                "stderr": result.stderr.strip(),
            },
        )

    def _write_metadata(self, result: subprocess.CompletedProcess[str]) -> None:
        if not self.pcapng_path:
            return

        metadata_path = self.pcapng_path.with_suffix(".metadata.json")
        metadata = {
            "pcapng_path": str(self.pcapng_path),
            "analysis_path": str(self.analysis_path) if self.analysis_path else None,
            "bot_id": get_bot_id(self.config),
            "service": get_service_name(self.config),
            "interface": self.interface,
            "capture_filter": self.capture_filter,
            "display_filter": self.display_filter,
            "tshark_returncode": result.returncode,
            "tshark_stderr": result.stderr.strip(),
        }
        metadata_path.write_text(json.dumps(metadata, sort_keys=True, indent=2), encoding="utf-8")

    def _file_stem(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        service = self._sanitize(get_service_name(self.config))
        bot_id = self._sanitize(get_bot_id(self.config))
        return f"{timestamp}-{service}-{bot_id}"

    def _sanitize(self, value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
        return sanitized or "unknown"
