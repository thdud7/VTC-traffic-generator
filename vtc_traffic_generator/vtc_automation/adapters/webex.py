import asyncio
import glob
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vtc_automation.event_log import emit_event, utc_now_iso

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError

from .browser import BrowserMeetingAdapter


class WebexAdapter(BrowserMeetingAdapter):
    service_name = "webex"

    DEFAULT_SELECTORS = {
        "browser_join": [
            'button:has-text("Join from your browser")',
            'a:has-text("Join from your browser")',
            '[data-test="join-from-browser"]',
            '[aria-label*="Join from your browser"]',
        ],
        "guest_join": [
            'button:has-text("Join as a guest")',
            'button:has-text("Continue as guest")',
            'a:has-text("Join as a guest")',
            '[aria-label*="guest"]',
        ],
        "name_input": [
            'input[name="displayName"]',
            'input[name="name"]',
            'input[aria-label*="name" i]',
            'input[placeholder*="name" i]',
        ],
        "email_input": [
            'input[type="email"]',
            'input[name="email"]',
            'input[aria-label*="email" i]',
            'input[placeholder*="email" i]',
        ],
        "password_input": [
            'input[type="password"]',
            'input[name="password"]',
            'input[aria-label*="password" i]',
            'input[placeholder*="password" i]',
        ],
        "next_button": [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            '[aria-label="Next"]',
            '[aria-label="Continue"]',
        ],
        "join_button": [
            'button:has-text("Join meeting")',
            'button:has-text("Join webinar")',
            'button:has-text("Join")',
            'button:has-text("Start meeting")',
            '[aria-label*="Join meeting"]',
            '[aria-label="Join"]',
        ],
        "joined": [
            '[aria-label*="Leave meeting"]',
            '[aria-label*="Leave"]',
            'button:has-text("Leave")',
            '[data-test*="leave"]',
            '[aria-label*="Mute"]',
            '[aria-label*="Unmute"]',
        ],
        "mic_enable": [
            '[aria-label*="Unmute"]',
            'button:has-text("Unmute")',
            '[data-test*="unmute"]',
        ],
        "mic_disable": [
            '[aria-label*="Mute"]',
            'button:has-text("Mute")',
            '[data-test*="mute"]',
        ],
        "camera_enable": [
            '[aria-label*="Start video"]',
            '[aria-label*="Turn on camera"]',
            'button:has-text("Start video")',
            '[data-test*="start-video"]',
        ],
        "camera_disable": [
            '[aria-label*="Stop video"]',
            '[aria-label*="Turn off camera"]',
            'button:has-text("Stop video")',
            '[data-test*="stop-video"]',
        ],
        "share_start": [
            '[aria-label*="Share content"]',
            '[aria-label*="Share screen"]',
            'button:has-text("Share")',
            '[data-test*="share"]',
        ],
        "share_stop": [
            '[aria-label*="Stop sharing"]',
            'button:has-text("Stop sharing")',
            '[data-test*="stop-share"]',
        ],
        "share_target": [
            'button:has-text("Screen")',
            '[aria-label*="Screen"]',
            '[aria-label*="Entire screen"]',
        ],
        "share_confirm": [
            'button:has-text("Share")',
            'button:has-text("Allow")',
        ],
        "leave_button": [
            '[aria-label*="Leave meeting"]',
            '[aria-label*="Leave"]',
            'button:has-text("Leave")',
            '[data-test*="leave"]',
        ],
        "leave_confirm": [
            'button:has-text("Leave meeting")',
            'button:has-text("Leave")',
            'button:has-text("End meeting")',
        ],
    }

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        adapter_config = self.adapter_config()
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.mic_enabled = bool(adapter_config.get("initial_microphone_enabled", True))
        self.camera_enabled = bool(adapter_config.get("initial_camera_enabled", True))
        self.screen_sharing = bool(adapter_config.get("initial_screen_sharing", False))
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        self.browser_log = []
        self.sanity_check_results = []

    def adapter_config(self):
        return self.config.get("adapter_config", {})

    def browser_args(self):
        adapter_config = self.adapter_config()
        configured = adapter_config.get("browser_args")
        args = list(configured or super().browser_args())
        required = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--window-size=1280,720",
            "--lang=en-US",
        ]
        if adapter_config.get("ignore_certificate_errors") is True:
            required.append("--ignore-certificate-errors")
        if adapter_config.get("disable_gpu") is True:
            required.extend(["--disable-gpu", "--disable-gpu-compositing"])

        share_target = adapter_config.get("screen_share_target") or adapter_config.get("auto_select_desktop_capture_source")
        if share_target:
            required.append(f"--auto-select-desktop-capture-source={share_target}")

        for arg in required:
            if not self._has_browser_arg(args, arg):
                args.append(arg)

        for arg in adapter_config.get("extra_browser_args") or []:
            if not self._has_browser_arg(args, str(arg)):
                args.append(str(arg))
        return args

    def launch_options(self):
        adapter_config = self.adapter_config()
        options = {
            "args": self.browser_args(),
            "headless": bool(adapter_config.get("headless", False)),
        }

        display = self._configured_display()
        if display:
            env = dict(os.environ)
            env["DISPLAY"] = display
            options["env"] = env

        executable_path = (
            adapter_config.get("browser_executable_path")
            or adapter_config.get("chromium_executable_path")
        )
        if executable_path:
            options["executable_path"] = str(executable_path)
        elif adapter_config.get("browser_channel"):
            options["channel"] = str(adapter_config["browser_channel"])
        else:
            discovered = self._discover_browser_executable()
            if discovered:
                options["executable_path"] = discovered

        slow_mo = adapter_config.get("slow_mo")
        if slow_mo is not None:
            options["slow_mo"] = slow_mo

        return options

    def selectors(self, name):
        configured = self.adapter_config().get("selectors", {})
        if isinstance(configured, Mapping) and name in configured:
            value = configured[name]
            return value if isinstance(value, list) else [value]
        return list(self.DEFAULT_SELECTORS.get(name, []))

    def timeout_ms(self, key, default):
        try:
            return int(self.adapter_config().get(key, default))
        except (TypeError, ValueError):
            return default

    async def launch(self):
        if self.page is not None:
            return None

        from playwright.async_api import async_playwright

        if not self.adapter_config().get("skip_sanity_checks", False):
            self.run_sanity_checks()
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(**self.launch_options())
        self.context = await self.browser.new_context(**self.context_options())
        self.page = await self.context.new_page()
        self._attach_page_logging(self.page)
        emit_event(self.config, "adapter_launched", {"backend": "playwright"}, self.service_name)
        return None

    async def connect(self, vtc_url, display_name=None):
        if isinstance(vtc_url, (int, float)) and display_name is None:
            duration = vtc_url
            await self.launch()
            await self.connect_to_meeting(
                str(self.config["vtc_url"]),
                self._display_name(),
            )
            await asyncio.sleep(duration * 60)
            await self.leave()
            await self.close()
            return f"{self.config.get('bot_name') or 'client'} connected to {self.service_name}."

        await self.launch()
        await self.connect_to_meeting(
            str(vtc_url),
            str(display_name or self._display_name()),
        )
        return True

    async def connect_to_meeting(self, vtc_url: str, display_name: str):
        await self.launch()
        self._meeting_joined_notified = False
        self._media_ready_notified = False
        await self.page.goto(vtc_url, wait_until="domcontentloaded")
        emit_event(self.config, "webex_page_opened", {"vtc_url": vtc_url, "title": await self.page.title()}, self.service_name)

        await self._click_optional("browser_join", "webex_browser_join_clicked")
        await self._click_optional("guest_join", "webex_guest_join_clicked")
        await self._fill_optional("name_input", display_name, "webex_display_name_entered")
        await self._fill_optional_configured("email_input", "email", "webex_email_entered")
        await self._fill_optional_configured("password_input", "meeting_password", "webex_password_entered")
        await self._click_optional("next_button", "webex_next_clicked")

        if bool(self.adapter_config().get("initial_microphone_enabled", True)):
            await self.unmute_microphone()
        else:
            await self.mute_microphone()

        if bool(self.adapter_config().get("initial_camera_enabled", True)):
            await self.start_camera()
        else:
            await self.stop_camera()

        await self._click_required(
            "join_button",
            "webex_join_clicked",
            timeout=self.timeout_ms("prejoin_timeout_ms", 30000),
        )
        if self.adapter_config().get("verify_joined", True):
            await self._wait_required("joined", timeout=self.timeout_ms("joined_timeout_ms", 45000))

        self._notify_meeting_joined(vtc_url)
        self._notify_media_ready(vtc_url)
        return True

    async def leave(self):
        clicked = await self._click_optional("leave_button", "webex_leave_clicked")
        fallback_success = False
        if clicked:
            await self._click_optional("leave_confirm", "webex_leave_confirmed")
        else:
            fallback_success = await self._fallback_action("leave")
        success = bool(clicked or fallback_success)
        emit_event(self.config, "webex_left_meeting", {"success": success}, self.service_name)
        return success

    async def close(self):
        self._flush_browser_log()
        if self.adapter_config().get("diagnostic_dir") or self.adapter_config().get("diagnostics_dir"):
            try:
                await self.collect_diagnostics(stage="close")
            except Exception:
                pass
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self.page = None
        emit_event(self.config, "adapter_closed", {"backend": "playwright"}, self.service_name)
        return True

    def run_sanity_checks(self):
        strict = bool(self.adapter_config().get("strict_sanity_checks", False))
        warnings = []
        checks = []

        emit_event(self.config, "webex_sanity_check_started", {}, self.service_name)

        def record(name, ok, message="", optional=False):
            item = {"name": name, "ok": bool(ok), "message": message, "optional": bool(optional)}
            checks.append(item)
            if not ok:
                warnings.append(item)
                emit_event(self.config, "webex_sanity_check_warning", item, self.service_name)

        display = self._configured_display()
        record("display", bool(display), "DISPLAY is not configured")
        if shutil.which("xdpyinfo"):
            result = self._run_sanity_command(["xdpyinfo"], env_display=display)
            record("xdpyinfo", result.returncode == 0, result.stderr.strip() or result.stdout.strip(), optional=True)
        else:
            record("xdpyinfo", False, "xdpyinfo not found", optional=True)

        if shutil.which("pactl"):
            pactl_info = self._run_sanity_command(["pactl", "info"])
            record("pactl_info", pactl_info.returncode == 0, pactl_info.stderr.strip(), optional=True)
            sources = self._run_sanity_command(["pactl", "list", "short", "sources"])
            configured_source = self.adapter_config().get("microphone_source") or self.adapter_config().get("audio_source")
            if configured_source:
                record("microphone_source", str(configured_source) in sources.stdout, f"{configured_source} not found")
        else:
            record("pactl_info", False, "pactl not found", optional=True)

        video_expected = bool(self.adapter_config().get("initial_camera_enabled", True) or self.adapter_config().get("video_device") or self.config.get("video_device"))
        if video_expected:
            record("video_device", bool(glob.glob("/dev/video*")), "no /dev/video* devices found", optional=True)

        record("ffmpeg", bool(shutil.which("ffmpeg")), "ffmpeg not found", optional=True)
        executable = self.launch_options().get("executable_path")
        channel = self.launch_options().get("channel") or self.adapter_config().get("browser_channel")
        record("browser_executable_or_channel", bool(executable or channel), "no Chrome/Chromium executable or channel configured/found", optional=True)

        fallback = self.adapter_config().get("fallback", {})
        needs_window_tools = bool(
            self.adapter_config().get("screen_share_target")
            or self.adapter_config().get("auto_select_desktop_capture_source")
            or (isinstance(fallback, Mapping) and fallback.get("enabled"))
        )
        if needs_window_tools:
            record("xdotool", bool(shutil.which("xdotool")), "xdotool not found", optional=True)
            record("wmctrl", bool(shutil.which("wmctrl")), "wmctrl not found", optional=True)

        self.sanity_check_results = checks
        emit_event(
            self.config,
            "webex_sanity_check_completed",
            {"warnings": len(warnings), "checks": checks},
            self.service_name,
        )
        if strict and warnings:
            raise RuntimeError(f"Webex sanity checks failed: {warnings}")
        return {"warnings": warnings, "checks": checks}

    async def collect_diagnostics(self, stage=None, extra=None):
        diag_dir = self._diagnostic_dir()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_stage = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(stage or "diagnostics"))
        prefix = f"{stamp}_{safe_stage}"
        diag_dir.mkdir(parents=True, exist_ok=True)
        saved = {"directory": str(diag_dir), "files": {}, "errors": {}}

        page = self.page
        metadata = {
            "stage": stage,
            "extra": dict(extra or {}),
            "url": None,
            "title": None,
            "states": {
                "mic": self.mic_enabled,
                "camera": self.camera_enabled,
                "screen_share": self.screen_sharing,
                "joined": self._meeting_joined_notified,
            },
            "adapter_config": self._safe_adapter_config(),
            "browser_log": list(self.browser_log),
            "sanity_check_results": list(self.sanity_check_results),
            "commands": {},
        }

        if page is not None:
            try:
                if not getattr(page, "is_closed", lambda: False)():
                    metadata["url"] = getattr(page, "url", None)
                    metadata["title"] = await self._maybe_await(page.title())
                    screenshot_path = diag_dir / f"{prefix}.png"
                    await self._maybe_await(page.screenshot(path=str(screenshot_path)))
                    saved["files"]["screenshot"] = str(screenshot_path)
                    html_path = diag_dir / f"{prefix}.html"
                    html_path.write_text(await self._maybe_await(page.content()), encoding="utf-8")
                    saved["files"]["html"] = str(html_path)
            except Exception as exc:
                saved["errors"]["page"] = repr(exc)

        for name, command in self._diagnostic_commands().items():
            command_path = diag_dir / f"{prefix}.{name}.txt"
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
                metadata["commands"][name] = {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout_path": str(command_path),
                    "stderr": result.stderr,
                }
                command_path.write_text(result.stdout or result.stderr or "", encoding="utf-8", errors="replace")
                saved["files"][name] = str(command_path)
            except Exception as exc:
                metadata["commands"][name] = {"command": command, "error": repr(exc)}
                saved["errors"][name] = repr(exc)

        metadata_path = diag_dir / f"{prefix}.metadata.json"
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True, default=str), encoding="utf-8")
        saved["files"]["metadata"] = str(metadata_path)
        emit_event(self.config, "webex_diagnostics_saved", {"stage": stage, "path": str(metadata_path)}, self.service_name)
        return saved

    async def is_in_meeting(self):
        if self.page is None:
            return False
        try:
            await self.wait_for_any_visible(self.page, self.selectors("joined"), timeout=1000)
            return True
        except RuntimeError:
            return False

    async def mute_microphone(self):
        return await self._set_button_state(
            state_attr="mic_enabled",
            desired=False,
            selector_group="mic_disable",
            fallback_name="mic",
            event_name="webex_mic_muted",
        )

    async def unmute_microphone(self):
        return await self._set_button_state(
            state_attr="mic_enabled",
            desired=True,
            selector_group="mic_enable",
            fallback_name="mic",
            event_name="webex_mic_unmuted",
        )

    async def start_camera(self):
        return await self._set_button_state(
            state_attr="camera_enabled",
            desired=True,
            selector_group="camera_enable",
            fallback_name="camera",
            event_name="webex_camera_started",
        )

    async def stop_camera(self):
        return await self._set_button_state(
            state_attr="camera_enabled",
            desired=False,
            selector_group="camera_disable",
            fallback_name="camera",
            event_name="webex_camera_stopped",
        )

    async def start_screen_share(self):
        clicked = await self._click_optional("share_start", "webex_screen_share_start_clicked")
        fallback_success = False
        if clicked:
            await self._click_optional("share_target", "webex_screen_share_target_clicked")
            await self._click_optional("share_confirm", "webex_screen_share_confirmed")
        else:
            fallback_success = await self._fallback_action("screen_share")
        success = bool(clicked or fallback_success)
        if success:
            self.screen_sharing = True
        return success

    async def stop_screen_share(self):
        clicked = await self._click_optional("share_stop", "webex_screen_share_stop_clicked")
        fallback_success = False
        if not clicked:
            fallback_success = await self._fallback_action("screen_share")
        success = bool(clicked or fallback_success)
        if success:
            self.screen_sharing = False
        return success

    async def _set_button_state(self, state_attr, desired, selector_group, fallback_name, event_name):
        if getattr(self, state_attr) is desired and self.adapter_config().get("use_cached_control_state", False):
            emit_event(self.config, event_name, {"success": True, "cached": True}, self.service_name)
            return True

        clicked = await self._click_optional(selector_group, event_name)
        fallback_success = False
        if not clicked:
            fallback_success = await self._fallback_action(fallback_name)
        success = bool(clicked or fallback_success)
        if success:
            setattr(self, state_attr, desired)
        return success

    async def _click_required(self, selector_group, event_name, timeout):
        selector = await self.click_first_visible(self.page, self.selectors(selector_group), timeout=timeout)
        emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
        return selector

    async def _click_optional(self, selector_group, event_name, timeout=None):
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000) if timeout is None else timeout
        selector = await self.click_if_visible(self.page, self.selectors(selector_group), timeout=timeout)
        if selector:
            emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
        return selector

    async def _fill_optional(self, selector_group, value, event_name):
        if value in (None, ""):
            return None
        timeout = self.timeout_ms("optional_selector_timeout_ms", 1000)
        for selector in self.selectors(selector_group):
            locator = self.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.fill(str(value))
                emit_event(self.config, event_name, {"selector": selector, "success": True}, self.service_name)
                return selector
            except PlaywrightTimeoutError:
                continue
        return None

    async def _fill_optional_configured(self, selector_group, config_key, event_name):
        return await self._fill_optional(selector_group, self.adapter_config().get(config_key), event_name)

    async def _wait_required(self, selector_group, timeout):
        selector = await self.wait_for_any_visible(self.page, self.selectors(selector_group), timeout=timeout)
        emit_event(self.config, "webex_join_verified", {"selector": selector, "success": True}, self.service_name)
        return selector

    async def _fallback_action(self, action_name):
        fallback = self.adapter_config().get("fallback", {})
        if not isinstance(fallback, Mapping) or not fallback.get("enabled", False):
            return False

        key = fallback.get(f"{action_name}_key")
        if key:
            return self._run_command(["xdotool", "key", str(key)], action_name)

        coordinates = fallback.get("coordinates", {})
        point = coordinates.get(action_name) if isinstance(coordinates, Mapping) else None
        if point and len(point) == 2:
            return self._run_command(["xdotool", "mousemove", str(point[0]), str(point[1]), "click", "1"], action_name)

        return False

    def _run_command(self, command, action_name):
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
        emit_event(
            self.config,
            "webex_fallback_action",
            {
                "action": action_name,
                "command": command,
                "returncode": result.returncode,
                "success": result.returncode == 0,
                "stderr": result.stderr.strip(),
            },
            self.service_name,
        )
        return result.returncode == 0

    def _has_browser_arg(self, args, desired):
        if "=" in desired:
            desired_key = desired.split("=", 1)[0]
            return any(str(arg) == desired or str(arg).startswith(f"{desired_key}=") for arg in args)
        return desired in args

    def _configured_display(self):
        return str(self.adapter_config().get("display") or self.config.get("display") or os.environ.get("DISPLAY") or "")

    def _discover_browser_executable(self):
        for binary in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(binary)
            if path:
                return path
        return None

    def _run_sanity_command(self, command, env_display=None):
        env = None
        if env_display:
            env = dict(os.environ)
            env["DISPLAY"] = env_display
        try:
            return subprocess.run(command, capture_output=True, text=True, timeout=5, check=False, env=env)
        except Exception as exc:
            return subprocess.CompletedProcess(command, returncode=127, stdout="", stderr=repr(exc))

    def _diagnostic_dir(self):
        adapter_config = self.adapter_config()
        configured = (
            adapter_config.get("diagnostic_dir")
            or adapter_config.get("diagnostics_dir")
            or self.config.get("diagnostic_dir")
            or self.config.get("diagnostics_dir")
        )
        return Path(str(configured or "artifacts/webex_diagnostics")).expanduser()

    def _diagnostic_commands(self):
        return {
            "wmctrl_windows": ["wmctrl", "-lG"],
            "xdotool_visible_windows": ["xdotool", "search", "--onlyvisible", "--name", "."],
            "pactl_info": ["pactl", "info"],
            "pactl_sources": ["pactl", "list", "short", "sources"],
            "pactl_source_outputs": ["pactl", "list", "source-outputs"],
            "video_devices": ["/bin/bash", "-lc", "ls -l /dev/video*"],
            "processes": ["ps", "-ef"],
        }

    def _safe_adapter_config(self):
        safe_keys = {
            "browser_channel",
            "browser_executable_path",
            "chromium_executable_path",
            "display",
            "headless",
            "ignore_certificate_errors",
            "disable_gpu",
            "screen_share_target",
            "auto_select_desktop_capture_source",
            "initial_microphone_enabled",
            "initial_camera_enabled",
            "initial_screen_sharing",
            "verify_joined",
            "prejoin_timeout_ms",
            "joined_timeout_ms",
            "optional_selector_timeout_ms",
            "skip_sanity_checks",
            "strict_sanity_checks",
            "diagnostic_dir",
            "diagnostics_dir",
            "permissions",
            "viewport",
        }
        return {key: value for key, value in self.adapter_config().items() if key in safe_keys}

    async def _maybe_await(self, value):
        if hasattr(value, "__await__"):
            return await value
        return value

    def _attach_page_logging(self, page):
        if page is None:
            return
        try:
            page.on("console", lambda msg: self._record_browser_event("console", msg))
            page.on("pageerror", lambda exc: self._record_browser_event("pageerror", exc))
            page.on("requestfailed", lambda request: self._record_browser_event("requestfailed", request))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _record_browser_event(self, event_type, obj):
        try:
            if event_type == "console":
                message = self._read_attr(obj, "text") or str(obj)
                location = self._read_attr(obj, "location")
                level = self._read_attr(obj, "type")
                self._append_browser_log(event_type, message, location=location, level=level)
            elif event_type == "requestfailed":
                failure = self._read_attr(obj, "failure")
                message = f"{self._read_attr(obj, 'url') or ''} {failure or ''}".strip()
                self._append_browser_log(event_type, message)
            else:
                self._append_browser_log(event_type, str(obj))
        except Exception as exc:
            self._append_browser_log("logging_error", repr(exc))

    def _read_attr(self, obj, name):
        value = getattr(obj, name, None)
        if callable(value):
            try:
                return value()
            except TypeError:
                return None
        return value

    def _append_browser_log(self, event_type, message, location=None, level=None):
        self.browser_log.append(
            {
                "ts": utc_now_iso(),
                "event_type": event_type,
                "message": str(message),
                "location": location,
                "level": level,
            }
        )
        if len(self.browser_log) > 1000:
            self.browser_log = self.browser_log[-1000:]

    def _flush_browser_log(self):
        log_path = self.adapter_config().get("browser_log_path") or self.config.get("browser_log_path")
        if not log_path:
            return None
        try:
            path = Path(str(log_path)).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.browser_log, indent=2, sort_keys=True, default=str), encoding="utf-8")
            return str(path)
        except Exception:
            return None

    def _display_name(self):
        bot = self.config.get("bot")
        if isinstance(bot, Mapping) and bot.get("display_name"):
            return str(bot["display_name"])
        return str(self.adapter_config().get("display_name") or self.config.get("bot_name") or "bot")

    def _notify_meeting_joined(self, vtc_url):
        if self._meeting_joined_notified:
            return
        self._meeting_joined_notified = True
        emit_event(self.config, "meeting_joined", {"vtc_url": vtc_url}, self.service_name)
        callback = self.config.get("_meeting_joined_callback")
        if callback:
            callback(vtc_url)

    def _notify_media_ready(self, vtc_url):
        if self._media_ready_notified:
            return
        self._media_ready_notified = True
        emit_event(self.config, "media_ready", {"vtc_url": vtc_url}, self.service_name)
        callback = self.config.get("_media_ready_callback")
        if callback:
            callback(vtc_url)
