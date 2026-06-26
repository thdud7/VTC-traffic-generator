from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from .base import ServiceAdapter
from vtc_automation.event_log import emit_event
from vtc_automation.live_media_probe import MediaProbeSnapshot, probe_pcap_media


DEFAULT_COORDINATES = {
    "room_url_input": [260, 137],
    "room_url_go_button": [266, 193],
    "name_input": None,
    "join_button": None,
    "mic_button": None,
    "camera_button": None,
    "hangup_button": None,
    "device_settings_button": None,
    "settings_audio_tab": None,
    "settings_video_tab": None,
    "screen_share_button": None,
    "screen_share_target": None,
    "screen_share_confirm": None,
}


DEFAULT_ACCESSIBILITY_NAMES = {
    "name_input": ["Enter your name", "Name", "Display name"],
    "join_button": ["Join meeting", "Join now", "Ask to join", "Join"],
    "hangup_button": ["Leave the meeting", "Leave meeting", "Hang up", "End call"],
    "device_settings_button": ["Device settings", "Settings", "Audio settings", "Video settings"],
    "more_actions_button": ["More actions", "More options"],
    "settings_menu_item": ["Settings", "Device settings"],
    "settings_audio_tab": ["Audio", "Microphone"],
    "settings_video_tab": ["Video", "Camera"],
    "audio_device_section": ["Microphone", "Audio input", "Audio"],
    "video_device_section": ["Camera", "Video input", "Video"],
    "screen_share_button": ["Share screen", "Start screen sharing", "Share your screen"],
    "screen_share_confirm": ["Share", "Start sharing"],
    "mic_currently_on": ["Mute microphone"],
    "mic_currently_off": ["Unmute microphone"],
    "mic_turn_on": ["Unmute microphone"],
    "mic_turn_off": ["Mute microphone"],
    "camera_currently_on": ["Stop camera"],
    "camera_currently_off": ["Start camera"],
    "camera_turn_on": ["Start camera"],
    "camera_turn_off": ["Stop camera"],
    "screen_share_currently_on": ["Stop sharing"],
    "screen_share_currently_off": ["Share screen", "Start screen sharing", "Share your screen"],
    "screen_share_turn_on": ["Share screen", "Start screen sharing", "Share your screen"],
    "screen_share_turn_off": ["Stop sharing"],
}


DEFAULT_ACCESSIBILITY_ROLES = {
    "name_input": ["text", "text entry", "entry"],
    "join_button": ["push button", "button"],
    "hangup_button": ["push button", "button"],
    "device_settings_button": ["push button", "button"],
    "screen_share_button": ["push button", "button"],
    "screen_share_confirm": ["push button", "button"],
    "mic_button": ["push button", "toggle button", "button"],
    "camera_button": ["push button", "toggle button", "button"],
}


@dataclass
class MeetingProbeResult:
    joined: bool = False
    media_ready: bool = False
    probe_name: str = "none"
    strong_evidence: bool = False
    observed: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    consecutive_success_count: int = 0
    sampled_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "joined": self.joined,
            "media_ready": self.media_ready,
            "probe_name": self.probe_name,
            "strong_evidence": self.strong_evidence,
            "observed": self.observed,
            "error": self.error,
            "consecutive_success_count": self.consecutive_success_count,
            "sampled_at": self.sampled_at,
        }


class JitsiElectronAdapter(ServiceAdapter):
    service_name = "jitsi_electron"
    supported_modes = ("native",)

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        self.adapter_config = dict(config.get("adapter_config", {}))
        self.display = self.adapter_config.get("display", ":99")
        self.display_backend = self.adapter_config.get("display_backend", "xvfb")
        self.action_timeout_sec = int(self.adapter_config.get("action_timeout_sec", 10))
        self.launch_timeout_sec = int(self.adapter_config.get("launch_timeout_sec", 20))
        self.app_log_path = self.adapter_config.get("app_log_path", "/tmp/jitsi-electron.log")
        self.adapter_log_path = self.adapter_config.get("adapter_log_path", "/tmp/vtc-jitsi-adapter.log")
        self.accessibility_dump_path = self.adapter_config.get("accessibility_dump_path")
        self.window_id = None
        self.process = None
        self.mic_enabled = None
        self.camera_enabled = None
        self.screen_sharing = None
        self._meeting_probe_history: list[dict[str, Any]] = []
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        self._current_vtc_url: str | None = None

    async def launch(self):
        emit_event(
            self.config,
            "adapter_launch_start",
            {"display": self.display, "display_backend": self.display_backend},
            self.service_name,
        )

        if self.adapter_config.get("restart_existing", False):
            self._run_command(
                ["pkill", "-f", self.adapter_config.get("process_match", "jitsi-meet")],
                timeout=5,
                check=False,
            )

        if self._optional_bool("reset_user_data_dir", False):
            user_data_dir = Path(str(self.adapter_config.get("user_data_dir", "~/.config/Jitsi Meet"))).expanduser()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            emit_event(
                self.config,
                "jitsi_user_data_dir_reset",
                {"path": str(user_data_dir), "success": True},
                self.service_name,
            )

        launch_command = self._build_launch_command()
        app_log = Path(self.app_log_path)
        app_log.parent.mkdir(parents=True, exist_ok=True)

        with app_log.open("ab") as stdout_file:
            self.process = subprocess.Popen(
                launch_command,
                env=self._command_env(),
                stdout=stdout_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        self.window_id = self._wait_for_window()
        self._position_window_after_launch()
        emit_event(
            self.config,
            "adapter_launch_done",
            {"window_id": self.window_id, "app_log_path": self.app_log_path},
            self.service_name,
        )
        return self.window_id

    async def connect(self, duration):
        try:
            await self.launch()
            await self.connect_to_meeting(
                vtc_url=str(self.config["vtc_url"]),
                display_name=self._display_name(),
            )
            await asyncio.sleep(duration * 60)
            await self.leave()
            await self.close()
            return f"{self._display_name()} connected to {self.service_name}."
        except Exception as exc:
            emit_event(
                self.config,
                "adapter_error",
                {"error": str(exc), "adapter_log_path": self.adapter_log_path, "app_log_path": self.app_log_path},
                self.service_name,
            )
            raise

    async def connect_to_meeting(self, vtc_url: str, display_name: str):
        self._current_vtc_url = vtc_url
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        emit_event(self.config, "connect_vtc_session_start", {"vtc_url": vtc_url}, self.service_name)
        if not self.adapter_config.get("skip_url_entry", False):
            await self._type_url(vtc_url)
        else:
            self._activate_window()
        await asyncio.sleep(float(self.adapter_config.get("page_load_wait_sec", 5)))
        if self._activate_meeting_window(vtc_url):
            self._position_window_after_launch()
        self.dump_accessibility_tree("prejoin")

        camera_name = self.adapter_config.get("camera_name")
        microphone_name = self.adapter_config.get("microphone_name")
        if not self.adapter_config.get("skip_device_selection", False) and (camera_name or microphone_name):
            if not await self.select_devices(
                camera_name=str(camera_name or ""),
                microphone_name=str(microphone_name or ""),
            ):
                self._write_device_diagnostics("device_selection_failed")
                raise RuntimeError(
                    "Jitsi Electron device selection failed: "
                    f"camera={camera_name!r} microphone={microphone_name!r}"
                )

        if not self.adapter_config.get("skip_join_flow", False):
            await self._enter_display_name(display_name)
            await self._ensure_prejoin_microphone_capture(str(microphone_name or ""))
            await self._click_join()

        probe_result = await self.is_in_meeting()
        if not probe_result:
            self.collect_diagnostics("join_or_media_ready_timeout")
            raise RuntimeError("Jitsi Electron did not appear to join the meeting")

        self.dump_accessibility_tree("meeting_joined")
        if microphone_name and self._optional_bool("verify_audio_capture_attached", False):
            attached = self._verify_jitsi_audio_capture_attached(str(microphone_name))
            if not attached and self._optional_bool("require_audio_capture_attached", False):
                raise RuntimeError(f"Jitsi Electron did not attach audio capture to {microphone_name!r}")

        self._notify_meeting_joined(vtc_url)
        self._notify_media_ready(vtc_url)
        emit_event(self.config, "connect_vtc_session_done", {"vtc_url": vtc_url}, self.service_name)

    async def leave(self):
        before_state = await self.is_in_meeting()
        method = self._click_accessible_or_coordinate("hangup_button")
        emit_event(
            self.config,
            "leave_meeting",
            {
                "action": "leave",
                "method": method or "none",
                "before_state": before_state,
                "after_state": None,
                "success": bool(method),
            },
            self.service_name,
        )

    async def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()

    async def mute(self):
        return await self._ensure_toggle_state("mic", False, "m", "mic_off")

    async def unmute(self):
        return await self._ensure_toggle_state("mic", True, "m", "mic_on")

    async def camera_on(self):
        return await self._ensure_toggle_state("camera", True, "v", "camera_on")

    async def camera_off(self):
        return await self._ensure_toggle_state("camera", False, "v", "camera_off")

    async def start_screen_share(self):
        return await self._ensure_screen_share_state(True)

    async def stop_screen_share(self):
        return await self._ensure_screen_share_state(False)

    async def select_devices(self, camera_name: str, microphone_name: str):
        opened = self._open_device_settings()
        self.dump_accessibility_tree("settings_opened")
        if not opened:
            if self._optional_bool("allow_pulse_default_device_selection_fallback", False):
                return self._accept_default_media_devices(camera_name, microphone_name)

            self._write_device_diagnostics("device_settings_open_failed")
            emit_event(
                self.config,
                "adapter_error",
                {"action": "select_devices", "method": "accessibility", "success": False},
                self.service_name,
            )
            return False

        audio_selected = True
        video_selected = True
        if microphone_name:
            audio_selected = self._select_device_name("audio", microphone_name)
        if camera_name:
            video_selected = self._select_device_name("video", camera_name)

        self._run_xdotool(["key", "Escape"], check=False)
        success = audio_selected and video_selected
        if not success:
            self._write_device_diagnostics("device_selection_incomplete")
        return success

    def _accept_default_media_devices(self, camera_name: str, microphone_name: str) -> bool:
        audio_status = self._pulse_source_status(microphone_name) if microphone_name else {"success": True}
        video_status = self._virtual_video_status(camera_name) if camera_name else {"success": True}
        audio_success = bool(audio_status.get("success"))
        video_success = bool(video_status.get("success"))

        if microphone_name:
            audio_event = {
                "action": "select_audio_device",
                "method": "pulse-default-fallback",
                "target": microphone_name,
                "match_type": audio_status.get("match_type", "none"),
                "default_source": audio_status.get("default_source"),
                "source_index": audio_status.get("source_index"),
                "source_name": audio_status.get("source_name"),
                "mute": audio_status.get("mute"),
                "success": audio_success,
            }
            emit_event(self.config, "audio_device_selected", audio_event, self.service_name)
            emit_event(self.config, "microphone_device_selected", audio_event, self.service_name)

        if camera_name:
            emit_event(
                self.config,
                "video_device_selected",
                {
                    "action": "select_video_device",
                    "method": "virtual-video-fallback",
                    "target": camera_name,
                    "device": video_status.get("device"),
                    "success": video_success,
                },
                self.service_name,
            )

        success = audio_success and video_success
        emit_event(
            self.config,
            "device_selection_fallback_used",
            {
                "method": "pulse-default-and-virtual-video",
                "audio": audio_status,
                "video": video_status,
                "success": success,
            },
            self.service_name,
        )
        if not success:
            self._write_device_diagnostics("device_selection_fallback_failed")
        return success

    def _pulse_source_status(self, microphone_name: str) -> dict[str, Any]:
        info = self._run_command(["pactl", "info"], check=False)
        sources = self._run_command(["pactl", "list", "short", "sources"], check=False)
        mute = self._run_command(["pactl", "get-source-mute", microphone_name], check=False)
        return self._parse_pulse_source_status(
            microphone_name=microphone_name,
            info_stdout=info.stdout,
            sources_stdout=sources.stdout,
            mute_stdout=mute.stdout,
            info_returncode=info.returncode,
            sources_returncode=sources.returncode,
            mute_returncode=mute.returncode,
        )

    def _parse_pulse_source_status(
        self,
        microphone_name: str,
        info_stdout: str,
        sources_stdout: str,
        mute_stdout: str,
        info_returncode: int = 0,
        sources_returncode: int = 0,
        mute_returncode: int = 0,
    ) -> dict[str, Any]:
        default_source = None
        for line in info_stdout.splitlines():
            if line.startswith("Default Source:"):
                default_source = line.split(":", 1)[1].strip()
                break

        rows = self._parse_pactl_short_rows(sources_stdout)
        matching_row = None
        for row in rows:
            if len(row) >= 2 and self._device_name_matches(row[1], microphone_name):
                matching_row = row
                break

        mute_value = None
        if mute_returncode == 0 and mute_stdout.strip():
            lowered = mute_stdout.lower()
            if "yes" in lowered:
                mute_value = "yes"
            elif "no" in lowered:
                mute_value = "no"

        match_type = "none"
        if matching_row:
            match_type = "exact" if matching_row[1] == microphone_name else "partial"

        success = (
            info_returncode == 0
            and sources_returncode == 0
            and bool(matching_row)
            and self._device_name_matches(default_source or "", microphone_name)
            and mute_value != "yes"
        )
        return {
            "success": success,
            "target": microphone_name,
            "default_source": default_source,
            "source_index": matching_row[0] if matching_row and matching_row else None,
            "source_name": matching_row[1] if matching_row and len(matching_row) >= 2 else None,
            "source_names": [row[1] for row in rows if len(row) >= 2],
            "mute": mute_value,
            "match_type": match_type,
            "info_returncode": info_returncode,
            "sources_returncode": sources_returncode,
            "mute_returncode": mute_returncode,
        }

    def _virtual_video_status(self, camera_name: str) -> dict[str, Any]:
        virtual_video = self.config.get("virtual_video")
        device = None
        if isinstance(virtual_video, Mapping):
            device = virtual_video.get("device")
        device = str(device or self.adapter_config.get("video_device") or "/dev/video5")
        return {
            "success": Path(device).exists(),
            "target": camera_name,
            "device": device,
        }

    def _verify_jitsi_audio_capture_attached(self, microphone_name: str) -> bool:
        timeout = float(self.adapter_config.get("audio_capture_verify_timeout_sec", 8))
        interval = float(self.adapter_config.get("audio_capture_verify_interval_sec", 0.5))
        deadline = time.time() + timeout
        status: dict[str, Any] = {"success": False, "target": microphone_name}

        while time.time() < deadline:
            status = self._jitsi_audio_capture_status(microphone_name)
            if status.get("success"):
                break
            time.sleep(interval)

        emit_event(
            self.config,
            "jitsi_audio_capture_attached" if status.get("success") else "jitsi_audio_capture_failed",
            status,
            self.service_name,
        )
        if not status.get("success"):
            self._write_device_diagnostics("jitsi_audio_capture_failed")
        return bool(status.get("success"))

    async def _ensure_prejoin_microphone_capture(self, microphone_name: str) -> bool:
        if not microphone_name or not self._optional_bool("initial_mic_enabled", False):
            return True
        before = self._jitsi_audio_capture_status(microphone_name)
        if before.get("success"):
            emit_event(
                self.config,
                "prejoin_microphone_capture_verified",
                {"method": "none", "before": before, "after": before, "success": True},
                self.service_name,
            )
            return True

        coords = self._coordinate("mic_button")
        if not coords:
            emit_event(
                self.config,
                "prejoin_microphone_capture_failed",
                {"method": "none", "before": before, "reason": "mic_button coordinate is not configured", "success": False},
                self.service_name,
            )
            return False

        self._emit_fallback("mic_button", "coordinate", "prejoin microphone source-output was not attached")
        self._click_coordinate(coords)
        await asyncio.sleep(float(self.adapter_config.get("prejoin_mic_capture_wait_sec", 3)))
        after = self._jitsi_audio_capture_status(microphone_name)
        event_name = "prejoin_microphone_capture_verified" if after.get("success") else "prejoin_microphone_capture_failed"
        emit_event(
            self.config,
            event_name,
            {"method": "coordinate", "before": before, "after": after, "success": bool(after.get("success"))},
            self.service_name,
        )
        return bool(after.get("success"))

    def _jitsi_audio_capture_status(self, microphone_name: str) -> dict[str, Any]:
        source_status = self._pulse_source_status(microphone_name)
        outputs = self._run_command(["pactl", "list", "short", "source-outputs"], check=False)
        rows = self._parse_pactl_short_rows(outputs.stdout)
        source_index = str(source_status.get("source_index") or "")
        matching_outputs = [row for row in rows if len(row) >= 2 and row[1] == source_index]
        return {
            "success": bool(source_status.get("success")) and bool(matching_outputs),
            "target": microphone_name,
            "source": source_status,
            "source_output_returncode": outputs.returncode,
            "source_outputs": rows,
            "matching_source_outputs": matching_outputs,
        }

    def _parse_pactl_short_rows(self, stdout: str) -> list[list[str]]:
        rows = []
        for line in stdout.splitlines():
            line = line.strip()
            if line:
                rows.append(line.split("\t"))
        return rows

    def _device_name_matches(self, actual: str, expected: str) -> bool:
        if not actual or not expected:
            return False
        return actual == expected or expected in actual or actual in expected

    async def is_in_meeting(self):
        result = await self.wait_for_meeting_probe()
        if result.joined and result.media_ready:
            emit_event(self.config, "meeting_state_verified", result.to_dict(), self.service_name)
            return True

        if self.window_id is not None and self._optional_bool("allow_window_id_meeting_fallback", False):
            emit_event(
                self.config,
                "meeting_state_unverified_fallback",
                {"window_id": self.window_id, "success": True},
                self.service_name,
            )
            return True

        emit_event(
            self.config,
            "meeting_state_unverified",
            {"window_id": self.window_id, "success": False, "last_probe": result.to_dict()},
            self.service_name,
        )
        return False

    async def wait_for_meeting_probe(self) -> MeetingProbeResult:
        timeout = float(
            self.adapter_config.get(
                "join_timeout_sec",
                self.adapter_config.get("joined_wait_sec", 20),
            )
        )
        join_interval = min(0.5, max(0.05, float(self.adapter_config.get("join_poll_interval_sec", 0.5))))
        media_interval = min(
            0.5,
            max(0.05, float(self.adapter_config.get("media_ready_poll_interval_sec", join_interval))),
        )
        stable_required = max(1, int(self.adapter_config.get("join_stable_samples", 2)))
        media_timeout = float(self.adapter_config.get("media_ready_timeout_sec", timeout))
        deadline = time.time() + timeout
        media_deadline: float | None = None
        consecutive = 0
        previous_snapshot: MediaProbeSnapshot | None = None
        last_result = MeetingProbeResult(error="not sampled")

        while time.time() < deadline or (
            last_result.joined and media_deadline is not None and time.time() < media_deadline
        ):
            result, previous_snapshot = self._probe_meeting_and_media(previous_snapshot)
            if result.joined:
                if media_deadline is None:
                    media_deadline = time.time() + media_timeout
                self._notify_meeting_joined(self._current_vtc_url)
            if result.joined and result.media_ready and result.strong_evidence:
                consecutive += 1
            else:
                consecutive = 0
            result.consecutive_success_count = consecutive
            self._record_meeting_probe(result)
            if consecutive >= stable_required:
                self._notify_media_ready(self._current_vtc_url)
                return result
            last_result = result
            await asyncio.sleep(media_interval if result.joined else join_interval)

        return last_result

    def _probe_meeting_and_media(
        self,
        previous_snapshot: MediaProbeSnapshot | None,
    ) -> tuple[MeetingProbeResult, MediaProbeSnapshot | None]:
        accessibility = self._find_in_meeting_evidence()
        live_snapshot = self._live_media_snapshot()
        observed: dict[str, Any] = {}
        if accessibility:
            observed["accessibility"] = accessibility

        media_ready = False
        strong_evidence = False
        probe_name = "none"
        error = None
        if live_snapshot:
            observed["live_media"] = live_snapshot.to_dict()
            delta = live_snapshot.media_like_delta_from(previous_snapshot)
            observed["live_media_delta"] = delta
            media_ready = (
                live_snapshot.readable
                and live_snapshot.dtls_packets > 0
                and live_snapshot.bidirectional_media_like
                and delta["client_to_jvb_media_like_packets"] > 0
                and delta["jvb_to_client_media_like_packets"] > 0
            )
            strong_evidence = media_ready
            probe_name = "live_pcap_jvb_media"
            error = live_snapshot.error

        joined = bool(accessibility) or media_ready
        if accessibility and not media_ready:
            probe_name = "accessibility"
            strong_evidence = True

        return (
            MeetingProbeResult(
                joined=joined,
                media_ready=media_ready,
                probe_name=probe_name,
                strong_evidence=strong_evidence,
                observed=observed,
                error=error,
            ),
            live_snapshot or previous_snapshot,
        )

    def _live_media_snapshot(self) -> MediaProbeSnapshot | None:
        pcap_path = self.config.get("_packet_capture_path")
        if not pcap_path:
            return None
        packet_capture = self.config.get("packet_capture")
        if not isinstance(packet_capture, Mapping):
            packet_capture = {}
        jvb_ip = str(packet_capture.get("jvb_ip") or "")
        if not jvb_ip:
            return None
        client_ip = packet_capture.get("client_ip")
        jvb_port = int(packet_capture.get("jvb_port") or 10000)
        return probe_pcap_media(
            path=str(pcap_path),
            client_ip=str(client_ip) if client_ip else None,
            jvb_ip=jvb_ip,
            jvb_port=jvb_port,
        )

    def _record_meeting_probe(self, result: MeetingProbeResult) -> None:
        payload = result.to_dict()
        self._meeting_probe_history.append(payload)
        self._meeting_probe_history = self._meeting_probe_history[-100:]
        emit_event(self.config, "meeting_probe_sample", payload, self.service_name)

    def _notify_meeting_joined(self, vtc_url: str | None = None) -> None:
        if self._meeting_joined_notified:
            return
        self._meeting_joined_notified = True
        vtc_url = vtc_url or self._current_vtc_url or str(self.config.get("vtc_url") or "")
        emit_event(self.config, "meeting_joined", {"vtc_url": vtc_url}, self.service_name)
        joined_callback = self.config.get("_meeting_joined_callback")
        if callable(joined_callback):
            joined_callback(vtc_url)

    def _notify_media_ready(self, vtc_url: str | None = None) -> None:
        if self._media_ready_notified:
            return
        self._media_ready_notified = True
        vtc_url = vtc_url or self._current_vtc_url or str(self.config.get("vtc_url") or "")
        emit_event(self.config, "media_ready", {"vtc_url": vtc_url}, self.service_name)
        media_ready_callback = self.config.get("_media_ready_callback")
        if callable(media_ready_callback):
            media_ready_callback(vtc_url)

    def dump_accessibility_tree(self, stage: str | None = None, output_path: str | None = None) -> bool:
        output_path = output_path or self._dump_path(stage)
        if not output_path:
            return False

        result = self._run_dogtail_tree(
            ["dump", "--output", output_path, "--window-regex", self._window_regex_for_accessibility()],
            check=False,
        )
        success = result.returncode == 0
        emit_event(
            self.config,
            "accessibility_tree_dumped",
            {
                "action": "dump_accessibility_tree",
                "stage": stage,
                "method": "accessibility",
                "target": output_path,
                "success": success,
                "stderr": result.stderr,
            },
            self.service_name,
        )
        return success

    def _build_launch_command(self) -> list[str]:
        launch_command = self.adapter_config.get("launch_command")
        if launch_command:
            if isinstance(launch_command, str):
                command = shlex.split(launch_command)
            else:
                command = [str(part) for part in launch_command]
            if self.adapter_config.get("launch_url_as_arg", False):
                command.append(self._launch_url_argument())
            return command

        executable_path = self.adapter_config.get("executable_path")
        if not executable_path:
            raise ValueError("adapter_config.executable_path or adapter_config.launch_command is required")

        if self.adapter_config.get("launch_url_as_arg", False):
            return [str(executable_path), self._launch_url_argument()]

        args = [str(executable_path)]
        args.extend(self.adapter_config.get("launch_args", self._default_launch_args()))
        return args

    def _launch_url_argument(self) -> str:
        vtc_url = str(self.config["vtc_url"])
        protocol = self.adapter_config.get("launch_url_protocol")
        if not protocol:
            return vtc_url

        parsed = urlsplit(vtc_url)
        if protocol == "jitsi-meet":
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                return vtc_url
            return urlunsplit(("jitsi-meet", parsed.netloc, parsed.path, parsed.query, parsed.fragment))

        return vtc_url

    def _default_launch_args(self) -> list[str]:
        args = [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--disable-dev-shm-usage",
        ]
        if self.adapter_config.get("ignore_certificate_errors", True):
            args.append("--ignore-certificate-errors")
        return args

    def _command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = str(self.display)
        extra_env = self.adapter_config.get("env", {})
        if isinstance(extra_env, Mapping):
            env.update({str(key): str(value) for key, value in extra_env.items()})
        return env

    def _window_title_regexes(self) -> list[str]:
        title_regex = self.adapter_config.get("window_title_regex", "Jitsi Meet|jitsi|meet")
        if isinstance(title_regex, str):
            return [title_regex]
        return [str(item) for item in title_regex]

    def _wait_for_window(self) -> str:
        deadline = time.time() + self.launch_timeout_sec
        last_error = ""
        while time.time() < deadline:
            for title_regex in self._window_title_regexes():
                result = self._run_xdotool(
                    ["search", "--onlyvisible", "--name", title_regex],
                    check=False,
                    timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    window_id = result.stdout.strip().splitlines()[-1]
                    self._run_xdotool(["windowactivate", "--sync", window_id], check=False)
                    return window_id
                last_error = result.stderr or result.stdout
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for Jitsi Electron window. Last output: {last_error}")

    def _position_window_after_launch(self) -> None:
        geometry = self.adapter_config.get("window_geometry")
        if not isinstance(geometry, Mapping) or not self.window_id:
            return
        width = geometry.get("width")
        height = geometry.get("height")
        left = geometry.get("left", 0)
        top = geometry.get("top", 0)
        if width and height:
            self._run_xdotool(["windowsize", str(self.window_id), str(int(width)), str(int(height))], check=False)
        self._run_xdotool(["windowmove", str(self.window_id), str(int(left)), str(int(top))], check=False)
        self._activate_window()

    async def _type_url(self, vtc_url: str):
        self._activate_window()
        if self.adapter_config.get("url_entry_mode") == "room_input":
            coords = self._coordinate("room_url_input")
            if coords:
                self._click_coordinate(coords)
            self._run_xdotool(["key", "ctrl+a"], check=False)
            self._run_xdotool(["type", "--delay", "1", vtc_url])
            go_coords = self._coordinate("room_url_go_button")
            if go_coords:
                self._click_coordinate(go_coords)
            else:
                self._run_xdotool(["key", "Return"])
            self._activate_meeting_window(vtc_url)
            return

        self._run_xdotool(["key", "ctrl+l"])
        self._run_xdotool(["type", "--delay", "1", vtc_url])
        self._run_xdotool(["key", "Return"])

    def _activate_meeting_window(self, vtc_url: str) -> bool:
        base_url = vtc_url.split("#", 1)[0]
        patterns = [
            str(self.adapter_config.get("meeting_window_regex") or ""),
            re.escape(base_url),
        ]
        patterns = [pattern for pattern in patterns if pattern]
        deadline = time.time() + float(self.adapter_config.get("meeting_window_wait_sec", 10))

        while time.time() < deadline:
            for pattern in patterns:
                result = self._run_xdotool(
                    ["search", "--onlyvisible", "--name", pattern],
                    check=False,
                    timeout=2,
                )
                if result.returncode == 0 and result.stdout.strip():
                    self.window_id = result.stdout.strip().splitlines()[-1]
                    self._activate_window()
                    return True
            time.sleep(0.5)
        return False

    async def _enter_display_name(self, display_name: str):
        method = self._click_accessible_names(self._names("name_input"), self._roles("name_input"))
        if not method:
            coords = self._coordinate("name_input")
            if coords:
                self._emit_fallback("name_input", "coordinate", "accessibility unavailable")
                self._click_coordinate(coords)
                method = "coordinate"
            else:
                self._emit_fallback("name_input", "keyboard", "accessibility/coordinate unavailable")
                self._run_xdotool(["key", "Tab"], check=False)
                method = "keyboard"

        self._run_xdotool(["key", "ctrl+a"], check=False)
        self._run_xdotool(["type", "--delay", "1", display_name])
        emit_event(
            self.config,
            "display_name_entered",
            {"action": "enter_display_name", "method": method, "success": True},
            self.service_name,
        )

    async def _click_join(self):
        method = self._click_accessible_names(self._names("join_button"), self._roles("join_button"))
        if not method:
            coords = self._coordinate("join_button")
            if coords:
                self._emit_fallback("join_button", "coordinate", "accessibility unavailable")
                self._click_coordinate(coords)
                method = "coordinate"
            else:
                self._emit_fallback("join_button", "keyboard", "accessibility/coordinate unavailable")
                self._run_xdotool(["key", "Return"])
                method = "keyboard"

        emit_event(
            self.config,
            "join_meeting_clicked",
            {"action": "join", "method": method, "success": True},
            self.service_name,
        )

    async def _ensure_toggle_state(self, control: str, desired_state: bool, shortcut: str, event_name: str):
        before_state = self._infer_control_state(control)
        if before_state == desired_state:
            self._set_cached_state(control, desired_state)
            self._emit_action_event(event_name, control, "none", before_state, before_state, True)
            return True

        if before_state is not None:
            method = self._press_shortcut(shortcut)
            await asyncio.sleep(float(self.adapter_config.get("state_change_wait_sec", 1)))
            after_state = self._infer_control_state(control)
            if self._trust_shortcut_state() and after_state == before_state:
                after_state = desired_state
            if after_state == desired_state:
                self._set_cached_state(control, desired_state)
                self._emit_action_event(
                    event_name,
                    control,
                    method,
                    before_state,
                    after_state,
                    True,
                )
                return True

        method = self._click_accessible_names(self._desired_action_names(control, desired_state), self._roles(f"{control}_button"))
        if method:
            await asyncio.sleep(float(self.adapter_config.get("state_change_wait_sec", 1)))
            after_state = self._infer_control_state(control)
            success = after_state == desired_state
            if success:
                self._set_cached_state(control, desired_state)
            self._emit_action_event(event_name, control, method, before_state, after_state, success)
            return success

        coords = self._coordinate(f"{control}_button")
        if coords:
            self._emit_fallback(f"{control}_button", "coordinate", "shortcut/accessibility did not verify target state")
            self._click_coordinate(coords)
            await asyncio.sleep(float(self.adapter_config.get("state_change_wait_sec", 1)))
            after_state = self._infer_control_state(control)
            success = after_state == desired_state
            if success:
                self._set_cached_state(control, desired_state)
            self._emit_action_event(event_name, control, "coordinate", before_state, after_state, success)
            return success

        self._emit_fallback(control, "none", "state unknown or target state could not be reached without blind toggle")
        self._emit_action_event(event_name, control, "none", before_state, before_state, False)
        return False

    async def _ensure_screen_share_state(self, desired_state: bool):
        before_state = self._infer_control_state("screen_share")
        event_name = "screen_share_start" if desired_state else "screen_share_stop"
        if before_state == desired_state:
            self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", "none", before_state, before_state, True)
            return True

        if before_state is not None:
            method = self._press_shortcut("d")
            await asyncio.sleep(float(self.adapter_config.get("screen_share_picker_wait_sec", 2)))
            if desired_state:
                self.dump_accessibility_tree("screen_share_picker_opened")
                if not self._select_screen_share_target():
                    self._emit_screen_share_error("screen share target could not be selected", before_state, method)
                    return False
            after_state = self._infer_control_state("screen_share")
            if self._trust_shortcut_state() and after_state == before_state:
                after_state = desired_state
            success = after_state == desired_state
            if success:
                self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", method, before_state, after_state, success)
            return success

        method = self._click_accessible_names(self._desired_action_names("screen_share", desired_state), self._roles("screen_share_button"))
        if method:
            if desired_state:
                self.dump_accessibility_tree("screen_share_picker_opened")
                if not self._select_screen_share_target():
                    self._emit_screen_share_error("screen share target could not be selected", before_state, method)
                    return False
            after_state = self._infer_control_state("screen_share")
            success = after_state == desired_state
            if success:
                self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", method, before_state, after_state, success)
            return success

        coords = self._coordinate("screen_share_button")
        if coords:
            self._emit_fallback("screen_share_button", "coordinate", "shortcut/accessibility unavailable")
            self._click_coordinate(coords)
            if desired_state:
                if not self._select_screen_share_target():
                    self._emit_screen_share_error("screen share target could not be selected", before_state, "coordinate")
                    return False
            after_state = self._infer_control_state("screen_share")
            success = after_state == desired_state
            if success:
                self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", "coordinate", before_state, after_state, success)
            return success

        self._emit_screen_share_error("screen share state unknown and no non-blind fallback configured", before_state, "none")
        return False

    def _open_device_settings(self) -> bool:
        method = self._click_accessible_names(self._names("device_settings_button"), self._roles("device_settings_button"))
        if not method:
            coords = self._coordinate("device_settings_button")
            if coords:
                self._emit_fallback("device_settings_button", "coordinate", "accessibility unavailable")
                self._click_coordinate(coords)
                method = "coordinate"
        if not method and self._click_accessible_names(self._names("more_actions_button"), self._roles("more_actions_button")):
            method = self._click_accessible_names(self._names("settings_menu_item"), self._roles("settings_menu_item"))

        emit_event(
            self.config,
            "device_settings_opened",
            {"action": "open_device_settings", "method": method or "none", "success": bool(method)},
            self.service_name,
        )
        return bool(method)

    def _select_device_name(self, device_type: str, device_name: str) -> bool:
        tab_key = "settings_audio_tab" if device_type == "audio" else "settings_video_tab"
        event_name = "audio_device_selected" if device_type == "audio" else "video_device_selected"
        self._click_accessible_names(self._names(tab_key), self._roles(tab_key))
        self._click_accessible_names(self._names(f"{device_type}_device_section"), self._roles(f"{device_type}_device_section"))

        method = self._click_accessible_names([device_name], [], partial=False)
        match_type = "exact"
        if not method:
            method = self._click_accessible_names([device_name], [], partial=True)
            match_type = "partial" if method else "none"

        emit_event(
            self.config,
            event_name,
            {
                "action": f"select_{device_type}_device",
                "method": method or "none",
                "target": device_name,
                "match_type": match_type,
                "success": bool(method),
            },
            self.service_name,
        )
        return bool(method)

    def _select_screen_share_target(self) -> bool:
        target_title = self.adapter_config.get("screen_share_target")
        if not target_title:
            return True
        if self._optional_bool("require_exact_screen_share_target_window", True):
            exact_count = self._target_window_count_exact(str(target_title))
            if exact_count != 1:
                emit_event(
                    self.config,
                    "screen_share_target_window_check_failed",
                    {"target": str(target_title), "exact_count": exact_count, "success": False},
                    self.service_name,
                )
                return False

        method = self._click_accessible_names([str(target_title)], [], partial=False)
        if not method and not self._optional_bool("require_exact_screen_share_target", True):
            method = self._click_accessible_names([str(target_title)], [], partial=True)
        if not method:
            coords = self._coordinate("screen_share_target")
            if coords:
                self._emit_fallback("screen_share_target", "coordinate", "target window not found in accessibility tree")
                self._click_coordinate(coords)
                method = "coordinate"

        if not method:
            if self._target_window_exists(str(target_title)) and self._optional_bool("trust_screen_share_target_window", False):
                self._emit_fallback(
                    "screen_share_target",
                    "keyboard",
                    "target X window exists; accepting picker default with keyboard",
                )
                self._run_xdotool(["key", "Return"], check=False)
                method = "keyboard"
            else:
                return False

        confirm = self._click_accessible_names(self._names("screen_share_confirm"), self._roles("screen_share_confirm"))
        if not confirm:
            coords = self._coordinate("screen_share_confirm")
            if coords:
                self._click_coordinate(coords)
                confirm = "coordinate"
        if not confirm:
            self._run_xdotool(["key", "Return"], check=False)
        return True

    def _infer_control_state(self, control: str) -> bool | None:
        if control == "mic":
            state = self._infer_state_from_accessibility(self._names("mic_currently_on"), self._names("mic_currently_off"))
            if state is not None:
                self.mic_enabled = state
            return self.mic_enabled if state is None and self._use_cached_control_state() else state
        if control == "camera":
            state = self._infer_state_from_accessibility(self._names("camera_currently_on"), self._names("camera_currently_off"))
            if state is not None:
                self.camera_enabled = state
            return self.camera_enabled if state is None and self._use_cached_control_state() else state
        if control == "screen_share":
            state = self._infer_state_from_accessibility(
                self._names("screen_share_currently_on"),
                self._names("screen_share_currently_off"),
            )
            if state is not None:
                self.screen_sharing = state
            return self.screen_sharing if state is None and self._use_cached_control_state() else state
        return None

    def _infer_state_from_accessibility(self, true_names: list[str], false_names: list[str]) -> bool | None:
        if self._find_accessible_names(true_names):
            return True
        if self._find_accessible_names(false_names):
            return False
        return None

    def _desired_action_names(self, control: str, desired_state: bool) -> list[str]:
        key = {
            ("mic", True): "mic_turn_on",
            ("mic", False): "mic_turn_off",
            ("camera", True): "camera_turn_on",
            ("camera", False): "camera_turn_off",
            ("screen_share", True): "screen_share_turn_on",
            ("screen_share", False): "screen_share_turn_off",
        }.get((control, desired_state))
        return self._names(key) if key else []

    def _set_cached_state(self, control: str, state: bool) -> None:
        if control == "mic":
            self.mic_enabled = state
        elif control == "camera":
            self.camera_enabled = state
        elif control == "screen_share":
            self.screen_sharing = state

    def _press_shortcut(self, shortcut: str) -> str:
        self._activate_window()
        self._run_xdotool(["key", shortcut])
        return "shortcut"

    def _target_window_exists(self, title: str) -> bool:
        result = self._run_xdotool(["search", "--name", title], check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def _target_window_count_exact(self, title: str) -> int:
        result = self._run_xdotool(
            ["search", "--onlyvisible", "--name", f"^{re.escape(title)}$"],
            check=False,
        )
        if result.returncode != 0:
            return 0
        return len([line for line in result.stdout.splitlines() if line.strip()])

    def _activate_window(self):
        if self.window_id:
            self._run_xdotool(["windowraise", str(self.window_id)], check=False)
            self._run_xdotool(["windowactivate", "--sync", str(self.window_id)], check=False)

    def _coordinate(self, key: str):
        coordinates = dict(DEFAULT_COORDINATES)
        configured = self.adapter_config.get("coordinates", {})
        if isinstance(configured, Mapping):
            coordinates.update(configured)

        value = coordinates.get(key)
        if not value:
            return None
        if isinstance(value, Mapping):
            return int(value["x"]), int(value["y"])
        if isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
            return int(value[0]), int(value[1])
        raise ValueError(f"Invalid coordinate for {key}: {value}")

    def _window_origin(self) -> tuple[int, int] | None:
        if not self.window_id:
            return None
        result = self._run_xdotool(["getwindowgeometry", "--shell", str(self.window_id)], check=False)
        if result.returncode != 0:
            return None
        origin: dict[str, int] = {}
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"X", "Y"}:
                try:
                    origin[key] = int(value.strip())
                except ValueError:
                    return None
        if "X" not in origin or "Y" not in origin:
            return None
        return origin["X"], origin["Y"]

    def _absolute_coordinate(self, coords: tuple[int, int]) -> tuple[int, int]:
        if not self._optional_bool("coordinates_relative_to_window", True):
            return coords
        origin = self._window_origin()
        if not origin:
            return coords
        return origin[0] + coords[0], origin[1] + coords[1]

    def _optional_bool(self, key: str, default: bool | None = None) -> bool | None:
        value = self.adapter_config.get(key, default)
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _trust_shortcut_state(self) -> bool:
        return self._optional_bool("trust_shortcut_state", False) is True

    def _use_cached_control_state(self) -> bool:
        return self._optional_bool("use_cached_control_state", False) is True

    def _click_coordinate(self, coords: tuple[int, int]):
        self._activate_window()
        x, y = self._absolute_coordinate(coords)
        self._run_xdotool(["mousemove", str(x), str(y)])
        self._run_xdotool(["click", "1"])

    def _click_accessible_or_coordinate(self, key: str) -> str | None:
        method = self._click_accessible_names(self._names(key), self._roles(key))
        if method:
            return method
        coords = self._coordinate(key)
        if coords:
            self._emit_fallback(key, "coordinate", "accessibility unavailable")
            self._click_coordinate(coords)
            return "coordinate"
        return None

    def _click_accessible_names(self, names: list[str], roles: list[str], partial: bool = False) -> str | None:
        if not names:
            return None
        command = ["click", "--window-regex", self._window_regex_for_accessibility()]
        if partial:
            command.append("--partial")
        for name in names:
            command.extend(["--name", name])
        for role in roles:
            command.extend(["--role", role])

        result = self._run_dogtail_tree(command, check=False)
        return "accessibility" if result.returncode == 0 else None

    def _find_accessible_names(self, names: list[str], roles: list[str] | None = None, partial: bool = False) -> dict[str, Any] | None:
        if not names:
            return None
        command = ["find", "--window-regex", self._window_regex_for_accessibility()]
        if partial:
            command.append("--partial")
        for name in names:
            command.extend(["--name", name])
        for role in roles or []:
            command.extend(["--role", role])

        result = self._run_dogtail_tree(command, check=False)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw": result.stdout}

    def _names(self, key: str | None) -> list[str]:
        if not key:
            return []
        names = dict(DEFAULT_ACCESSIBILITY_NAMES)
        configured = self.adapter_config.get("accessibility_names", {})
        if isinstance(configured, Mapping):
            names.update({str(item_key): self._normalize_string_list(item_value) for item_key, item_value in configured.items()})
        return names.get(key, [])

    def _roles(self, key: str | None) -> list[str]:
        if not key:
            return []
        roles = dict(DEFAULT_ACCESSIBILITY_ROLES)
        configured = self.adapter_config.get("accessibility_roles", {})
        if isinstance(configured, Mapping):
            roles.update({str(item_key): self._normalize_string_list(item_value) for item_key, item_value in configured.items()})
        return roles.get(key, [])

    def _normalize_string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    def _dump_path(self, stage: str | None) -> str | None:
        if not self.accessibility_dump_path:
            return None
        if "{stage}" in str(self.accessibility_dump_path):
            return str(self.accessibility_dump_path).format(stage=stage or "tree")
        return str(self.accessibility_dump_path)

    def _diagnostic_path(self, stage: str) -> Path:
        stage = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(stage)).strip("-") or "diagnostics"
        configured = self.adapter_config.get("diagnostic_dir")
        if configured:
            base = Path(str(configured)).expanduser()
        else:
            base = Path(self.adapter_log_path).expanduser().parent / "diagnostics"
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{stage}.json"

    def collect_diagnostics(self, stage: str, extra: Mapping[str, Any] | None = None) -> str:
        path = self._diagnostic_path(stage)
        accessibility_path = path.with_suffix(".accessibility.json")
        diagnostics: dict[str, Any] = {
            "stage": stage,
            "extra": dict(extra or {}),
            "display": self.display,
            "display_backend": self.display_backend,
            "window_id": self.window_id,
            "vtc_url": self.config.get("vtc_url"),
            "bot_id": self.config.get("bot_name"),
            "execution_id": self.config.get("execution_id"),
            "experiment_id": self.config.get("experiment_id"),
            "git_sha": self.config.get("git_sha"),
            "config_sha256": self.config.get("config_sha256"),
            "connection_status": self.config.get("_connection_status"),
            "packet_capture_path": self.config.get("_packet_capture_path"),
            "meeting_probe_history": self._meeting_probe_history[-50:],
            "commands": {},
            "logs": {},
        }

        pcap_path = self.config.get("_packet_capture_path")
        if pcap_path:
            pcap = Path(str(pcap_path)).expanduser()
            diagnostics["packet_capture"] = {
                "path": str(pcap),
                "exists": pcap.exists(),
                "size_bytes": pcap.stat().st_size if pcap.exists() else None,
            }

        self.dump_accessibility_tree(str(stage), str(accessibility_path))
        diagnostics["accessibility_dump_path"] = str(accessibility_path)
        diagnostics["accessibility_dump_exists"] = accessibility_path.exists()

        video_device = str(self.config.get("virtual_video", {}).get("device") or self.adapter_config.get("video_device") or "/dev/video5")
        command_map = {
            "wmctrl_windows": ["wmctrl", "-lG"],
            "xdotool_visible_windows": ["xdotool", "search", "--onlyvisible", "--name", "."],
            "processes": ["/bin/bash", "-lc", "ps -ef | grep -E '[j]itsi|[e]lectron|[f]fmpeg|[d]umpcap|[v]tc_generator'"],
            "pactl_info": ["pactl", "info"],
            "pactl_sinks": ["pactl", "list", "short", "sinks"],
            "pactl_sources": ["pactl", "list", "short", "sources"],
            "pactl_source_outputs": ["pactl", "list", "source-outputs"],
            "pactl_sink_inputs": ["pactl", "list", "sink-inputs"],
            "video_devices": ["/bin/bash", "-lc", f"ls -l {shlex.quote(video_device)} /dev/video* 2>/dev/null || true"],
            "ffmpeg_video_device": ["/bin/bash", "-lc", f"ps -ef | grep -E '[f]fmpeg.*{re.escape(video_device)}' || true"],
        }
        for key, command in command_map.items():
            result = self._run_command(command, check=False)
            diagnostics["commands"][key] = {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        for key, file_path in {
            "app_log": self.app_log_path,
            "adapter_log": self.adapter_log_path,
            "event_log": self.adapter_config.get("event_log_path"),
        }.items():
            if file_path:
                diagnostics["logs"][key] = self._tail_file(str(file_path))

        path.write_text(json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8")
        emit_event(
            self.config,
            "session_diagnostics_saved",
            {"stage": stage, "path": str(path), "accessibility_dump_path": str(accessibility_path), "success": True},
            self.service_name,
        )
        return str(path)

    def _tail_file(self, path: str, max_bytes: int = 65536) -> dict[str, Any]:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            return {"path": str(file_path), "exists": False}
        with file_path.open("rb") as infile:
            infile.seek(0, os.SEEK_END)
            size = infile.tell()
            infile.seek(max(0, size - max_bytes))
            data = infile.read().decode("utf-8", errors="replace")
        return {"path": str(file_path), "exists": True, "size_bytes": size, "tail": data}

    def _write_device_diagnostics(self, stage: str) -> str:
        diagnostics: dict[str, Any] = {
            "stage": stage,
            "display": self.display,
            "window_id": self.window_id,
            "commands": {},
        }
        for key, command in {
            "pactl_info": ["pactl", "info"],
            "pactl_sinks": ["pactl", "list", "short", "sinks"],
            "pactl_sources": ["pactl", "list", "short", "sources"],
            "pactl_source_outputs": ["pactl", "list", "source-outputs"],
            "media_windows": ["wmctrl", "-l"],
        }.items():
            result = self._run_command(command, check=False)
            diagnostics["commands"][key] = {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        path = self._diagnostic_path(stage)
        path.write_text(json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8")
        self.dump_accessibility_tree(stage)
        emit_event(
            self.config,
            "device_diagnostics_saved",
            {"stage": stage, "path": str(path), "success": True},
            self.service_name,
        )
        return str(path)

    def _find_in_meeting_evidence(self) -> dict[str, Any] | None:
        evidence = {}
        for key in ("hangup_button", "mic_currently_on", "mic_currently_off", "camera_currently_on", "camera_currently_off"):
            found = self._find_accessible_names(self._names(key), self._roles(key))
            if found:
                evidence[key] = found
        if "hangup_button" in evidence or (
            ("mic_currently_on" in evidence or "mic_currently_off" in evidence)
            and ("camera_currently_on" in evidence or "camera_currently_off" in evidence)
        ):
            return {"success": True, "evidence_keys": sorted(evidence.keys()), "window_id": self.window_id}
        return None

    def _find_media_session_evidence(self) -> dict[str, Any] | None:
        if not self._optional_bool("allow_media_capture_meeting_fallback", False):
            return None
        microphone_name = str(self.adapter_config.get("microphone_name") or "")
        if not microphone_name:
            return None
        status = self._jitsi_audio_capture_status(microphone_name)
        if not status.get("success"):
            emit_event(
                self.config,
                "meeting_state_media_fallback_unverified",
                {"success": False, "window_id": self.window_id, "audio_capture": status},
                self.service_name,
            )
            return None
        return {
            "success": True,
            "evidence_keys": ["jitsi_audio_capture"],
            "window_id": self.window_id,
            "audio_capture": status,
        }

    def _window_regex_for_accessibility(self) -> str:
        return self.adapter_config.get("accessibility_window_regex") or self._window_title_regexes()[0]

    def _display_name(self) -> str:
        bot = self.config.get("bot")
        if isinstance(bot, Mapping) and bot.get("display_name"):
            return str(bot["display_name"])
        return str(self.adapter_config.get("display_name") or self.config.get("bot_name") or "bot")

    def _emit_action_event(
        self,
        event_name: str,
        action: str,
        method: str,
        before_state: bool | None,
        after_state: bool | None,
        success: bool,
    ) -> None:
        emit_event(
            self.config,
            event_name,
            {
                "action": action,
                "method": method,
                "before_state": before_state,
                "after_state": after_state,
                "success": success,
            },
            self.service_name,
        )

    def _emit_fallback(self, action: str, method: str, reason: str) -> None:
        emit_event(
            self.config,
            "automation_fallback_used",
            {"action": action, "method": method, "reason": reason, "success": method != "none"},
            self.service_name,
        )

    def _emit_screen_share_error(self, reason: str, before_state: bool | None, method: str) -> None:
        emit_event(
            self.config,
            "screen_share_error",
            {
                "action": "screen_share",
                "method": method,
                "before_state": before_state,
                "after_state": self._infer_control_state("screen_share"),
                "success": False,
                "display_backend": self.display_backend,
                "reason": reason,
            },
            self.service_name,
        )

    def _run_dogtail_tree(self, args: list[str], check: bool = True):
        return self._run_command(
            ["python3", "-m", "vtc_traffic_generator.vtc_automation.dogtail_tree", *args],
            check=check,
        )

    def _run_xdotool(self, args: list[str], check: bool = True, timeout: int | None = None):
        return self._run_command(["xdotool", *args], timeout=timeout, check=check)

    def _run_command(self, command: list[str], timeout: int | None = None, check: bool = True):
        timeout = timeout or self.action_timeout_sec
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._command_env(),
        )
        self._log_command(command, result)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {shlex.join(command)}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _log_command(self, command: list[str], result: subprocess.CompletedProcess[str]):
        log_path = Path(self.adapter_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"command={shlex.join(command)} returncode={result.returncode}\n"
                f"stdout={result.stdout}\n"
                f"stderr={result.stderr}\n"
            )
