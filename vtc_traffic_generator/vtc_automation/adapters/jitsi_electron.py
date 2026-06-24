from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import ServiceAdapter
from vtc_automation.event_log import emit_event


DEFAULT_COORDINATES = {
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
        self.mic_enabled = self._optional_bool("initial_mic_enabled")
        self.camera_enabled = self._optional_bool("initial_camera_enabled")
        self.screen_sharing = self._optional_bool("initial_screen_sharing", False)

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
        emit_event(self.config, "connect_vtc_session_start", {"vtc_url": vtc_url}, self.service_name)
        await self._type_url(vtc_url)
        await asyncio.sleep(float(self.adapter_config.get("page_load_wait_sec", 5)))
        self.dump_accessibility_tree("prejoin")

        camera_name = self.adapter_config.get("camera_name")
        microphone_name = self.adapter_config.get("microphone_name")
        if not self.adapter_config.get("skip_device_selection", False) and (camera_name or microphone_name):
            await self.select_devices(
                camera_name=str(camera_name or ""),
                microphone_name=str(microphone_name or ""),
            )

        await self._enter_display_name(display_name)
        await self._click_join()

        if not await self.is_in_meeting():
            raise RuntimeError("Jitsi Electron did not appear to join the meeting")

        self.dump_accessibility_tree("meeting_joined")
        emit_event(self.config, "meeting_joined", {"vtc_url": vtc_url}, self.service_name)
        joined_callback = self.config.get("_meeting_joined_callback")
        if callable(joined_callback):
            joined_callback(vtc_url)
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
        return audio_selected and video_selected

    async def is_in_meeting(self):
        await asyncio.sleep(float(self.adapter_config.get("joined_wait_sec", 3)))
        return self.window_id is not None

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
                return shlex.split(launch_command)
            return [str(part) for part in launch_command]

        executable_path = self.adapter_config.get("executable_path")
        if not executable_path:
            raise ValueError("adapter_config.executable_path or adapter_config.launch_command is required")

        args = [str(executable_path)]
        args.extend(self.adapter_config.get("launch_args", self._default_launch_args()))
        return args

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

    async def _type_url(self, vtc_url: str):
        self._activate_window()
        self._run_xdotool(["key", "ctrl+l"])
        self._run_xdotool(["type", "--delay", "1", vtc_url])
        self._run_xdotool(["key", "Return"])

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
            if after_state == desired_state or after_state is None:
                self._set_cached_state(control, desired_state)
                self._emit_action_event(
                    event_name,
                    control,
                    method,
                    before_state,
                    desired_state if after_state is None else after_state,
                    True,
                )
                return True

        method = self._click_accessible_names(self._desired_action_names(control, desired_state), self._roles(f"{control}_button"))
        if method:
            await asyncio.sleep(float(self.adapter_config.get("state_change_wait_sec", 1)))
            after_state = self._infer_control_state(control)
            success = after_state == desired_state or after_state is None
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
            success = after_state == desired_state or after_state is None
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
            success = after_state == desired_state or after_state is None
            if success:
                self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", method, before_state, after_state, success)
            return success

        method = self._click_accessible_names(self._desired_action_names("screen_share", desired_state), self._roles("screen_share_button"))
        if method:
            if desired_state:
                self.dump_accessibility_tree("screen_share_picker_opened")
                self._select_screen_share_target()
            after_state = self._infer_control_state("screen_share")
            success = after_state == desired_state or after_state is None
            if success:
                self.screen_sharing = desired_state
            self._emit_action_event(event_name, "screen_share", method, before_state, after_state, success)
            return success

        coords = self._coordinate("screen_share_button")
        if coords:
            self._emit_fallback("screen_share_button", "coordinate", "shortcut/accessibility unavailable")
            self._click_coordinate(coords)
            if desired_state:
                self._select_screen_share_target()
            after_state = self._infer_control_state("screen_share")
            success = after_state == desired_state or after_state is None
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

        method = self._click_accessible_names([str(target_title)], [], partial=False)
        if not method:
            method = self._click_accessible_names([str(target_title)], [], partial=True)
        if not method:
            coords = self._coordinate("screen_share_target")
            if coords:
                self._emit_fallback("screen_share_target", "coordinate", "target window not found in accessibility tree")
                self._click_coordinate(coords)
                method = "coordinate"

        if not method:
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
            return self.mic_enabled if state is None else state
        if control == "camera":
            state = self._infer_state_from_accessibility(self._names("camera_currently_on"), self._names("camera_currently_off"))
            if state is not None:
                self.camera_enabled = state
            return self.camera_enabled if state is None else state
        if control == "screen_share":
            state = self._infer_state_from_accessibility(
                self._names("screen_share_currently_on"),
                self._names("screen_share_currently_off"),
            )
            if state is not None:
                self.screen_sharing = state
            return self.screen_sharing if state is None else state
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

    def _activate_window(self):
        if self.window_id:
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
        return self._optional_bool("trust_shortcut_state", True) is True

    def _click_coordinate(self, coords: tuple[int, int]):
        x, y = coords
        self._activate_window()
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
