from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError

from .browser import BrowserMeetingAdapter, DEFAULT_VIEWPORT
from vtc_automation.event_log import emit_event


class GoogleMeetAdapter(BrowserMeetingAdapter):
    service_name = "google_meet"

    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.browser_process: subprocess.Popen[str] | None = None
        self.window_id: str | None = None
        self.display = str(self.adapter_config().get("display") or os.environ.get("DISPLAY") or ":99")
        self.action_timeout_sec = int(float(self.adapter_config().get("action_timeout_sec", 10)))
        self.launch_timeout_sec = float(self.adapter_config().get("launch_timeout_sec", 20))
        self.browser_log: list[dict[str, Any]] = []
        self.mic_enabled = bool(self.adapter_config().get("initial_mic_enabled", True))
        self.camera_enabled = bool(self.adapter_config().get("initial_camera_enabled", True))
        self.screen_sharing = bool(self.adapter_config().get("initial_screen_sharing", False))
        self.joined = False

    def browser_args(self):
        adapter_config = self.adapter_config()
        args = [
            "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1280,720",
            "--lang=en-US",
        ]
        if adapter_config.get("ignore_certificate_errors", True):
            args.append("--ignore-certificate-errors")
        if adapter_config.get("disable_gpu", True):
            args.extend(["--disable-gpu", "--disable-gpu-compositing"])
        capture_source = adapter_config.get("auto_select_desktop_capture_source")
        if capture_source:
            args.append(f"--auto-select-desktop-capture-source={capture_source}")
        args.extend(str(arg) for arg in adapter_config.get("extra_browser_args", []))
        banned = "--use-fake-device-for-media-stream"
        if any(str(arg).split("=", 1)[0] == banned for arg in args):
            raise ValueError(f"{banned} is forbidden for Google Meet collection")
        return args

    def launch_options(self):
        options = super().launch_options()
        display = str(self.adapter_config().get("display") or os.environ.get("DISPLAY") or ":99")
        options["env"] = {**os.environ, "DISPLAY": display}
        if not options.get("executable_path") and not options.get("channel"):
            executable_path = self._find_browser_executable()
            if executable_path:
                options["executable_path"] = executable_path
        return options

    def context_options(self):
        adapter_config = self.adapter_config()
        options = {
            "ignore_https_errors": True,
            "permissions": adapter_config.get("permissions", ["camera", "microphone"]),
            "viewport": adapter_config.get("viewport", DEFAULT_VIEWPORT),
            "locale": adapter_config.get("locale", "en-US"),
            "record_video_dir": adapter_config.get("record_video_dir"),
        }
        return {key: value for key, value in options.items() if value is not None}

    async def launch(self):
        if self._use_gui_keyboard():
            return await self._launch_gui_keyboard()

        from playwright.async_api import async_playwright

        self.run_sanity_checks()
        emit_event(self.config, "adapter_launch_start", {"browser": "chromium"}, self.service_name)
        self.playwright = await async_playwright().start()
        options = self.launch_options()
        user_data_dir = self.adapter_config().get("user_data_dir") or os.environ.get("VTC_MEET_CHROME_USER_DATA_DIR")
        if user_data_dir and "${" in str(user_data_dir):
            user_data_dir = None
        if user_data_dir:
            context_options = {**self.context_options(), **options}
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(Path(user_data_dir).expanduser()),
                **context_options,
            )
            self.browser = self.context.browser
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        else:
            self.browser = await self.playwright.chromium.launch(**options)
            self.context = await self.browser.new_context(**self.context_options())
            self.page = await self.context.new_page()

        self.page.on("console", self._record_console)
        self.page.on("pageerror", self._record_page_error)
        emit_event(self.config, "adapter_launch_done", {"persistent_profile": bool(user_data_dir)}, self.service_name)
        return True

    async def connect(self, duration):
        try:
            await self.launch()
            await self.connect_to_meeting(str(self.config["vtc_url"]), self._display_name())
            await asyncio.sleep(duration * 60)
            await self.leave()
            return f"{self._display_name()} connected to {self.service_name}."
        finally:
            await self.close()

    async def connect_to_meeting(self, vtc_url: str, display_name: str):
        if self._use_gui_keyboard():
            return await self._connect_to_meeting_gui_keyboard(vtc_url, display_name)

        if self.page is None:
            raise RuntimeError("Google Meet browser page is not launched")

        emit_event(self.config, "meeting_join_start", {"vtc_url": vtc_url}, self.service_name)
        await self.page.goto(vtc_url, wait_until="domcontentloaded", timeout=self._timeout_ms("goto_timeout_sec", 60))
        await self.page.wait_for_timeout(int(float(self.adapter_config().get("page_load_wait_sec", 4)) * 1000))
        await self._dismiss_common_prompts()
        await self._fill_display_name(display_name)
        await self._ensure_prejoin_media_state()
        await self._raise_if_join_blocked("pre_join")
        join_selector = await self._click_join_button()
        joined = await self.is_in_meeting()
        if not joined:
            await self._collect_diagnostics_async("meeting_join_failed", {"join_selector": join_selector})
            emit_event(self.config, "meeting_join_failed", {"vtc_url": vtc_url, "join_selector": join_selector}, self.service_name)
            raise RuntimeError("Google Meet join did not reach in-call state")

        self._notify_callback("_meeting_joined_callback", vtc_url)
        self._notify_callback("_media_ready_callback", vtc_url)
        emit_event(self.config, "meeting_join_success", {"vtc_url": vtc_url, "join_selector": join_selector}, self.service_name)

    async def leave(self):
        if self._use_gui_keyboard():
            emit_event(self.config, "meeting_leave_start", {}, self.service_name)
            selector = None
            if self.window_id:
                self._activate_window()
                shortcut = str(self.adapter_config().get("leave_shortcut", "ctrl+w"))
                self._run_xdotool(["key", shortcut], check=False)
                selector = f"keyboard:{shortcut}"
                await asyncio.sleep(float(self.adapter_config().get("leave_wait_sec", 1)))
            self.joined = False
            emit_event(self.config, "meeting_leave_success", {"selector": selector, "success": bool(selector)}, self.service_name)
            return bool(selector)

        emit_event(self.config, "meeting_leave_start", {}, self.service_name)
        selector = await self._click_optional(
            [
                "button[aria-label*='Leave call']",
                "button[aria-label*='Leave']",
                "button:has-text('Leave call')",
                "button:has-text('Leave')",
            ],
            timeout=1500,
        )
        if not selector and self.page:
            await self.page.keyboard.press("Control+Alt+H")
            selector = "keyboard:Control+Alt+H"
            emit_event(self.config, "selector_fallback_used", {"action": "leave", "method": selector}, self.service_name)
        emit_event(self.config, "meeting_leave_success", {"selector": selector, "success": bool(selector)}, self.service_name)
        return bool(selector)

    async def close(self):
        self.collect_browser_log("adapter_close")
        if self._use_gui_keyboard():
            if self.browser:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
            if self.browser_process and self.browser_process.poll() is None:
                self.browser_process.terminate()
                try:
                    self.browser_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.browser_process.kill()
                    self.browser_process.wait(timeout=3)
            return
        if self.context:
            await self.context.close()
        elif self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def mute(self):
        return await self._set_keyboard_toggle("mic", False, "Control+D", "mic_off")

    async def unmute(self):
        return await self._set_keyboard_toggle("mic", True, "Control+D", "mic_on")

    async def camera_on(self):
        return await self._set_keyboard_toggle("camera", True, "Control+E", "camera_on")

    async def camera_off(self):
        return await self._set_keyboard_toggle("camera", False, "Control+E", "camera_off")

    async def start_screen_share(self):
        if self._use_gui_keyboard():
            if self.screen_sharing:
                return True
            shortcut = str(self.adapter_config().get("screen_share_shortcut", "ctrl+alt+t"))
            self._activate_window()
            self._run_xdotool(["key", shortcut])
            await asyncio.sleep(float(self.adapter_config().get("screen_share_wait_sec", 2)))
            target_selector = await self._select_screen_share_target_gui_keyboard()
            self.screen_sharing = True
            emit_event(
                self.config,
                "screenshare_start",
                {
                    "selector": f"keyboard:{shortcut}",
                    "confirm_selector": target_selector,
                    "success": bool(target_selector),
                },
                self.service_name,
            )
            return bool(target_selector)

        if self.screen_sharing:
            return True
        selector = await self._click_optional(
            [
                "button[aria-label*='Present now']",
                "button[aria-label*='Share screen']",
                "button:has-text('Present now')",
                "button:has-text('Share screen')",
            ],
            timeout=2000,
        )
        if not selector:
            emit_event(self.config, "selector_fallback_used", {"action": "screen_share_start", "method": "keyboard"}, self.service_name)
            if self.page:
                await self.page.keyboard.press("Control+Alt+P")
            selector = "keyboard:Control+Alt+P"

        confirm = await self._click_optional(
            [
                "button:has-text('Share')",
                "button:has-text('Start sharing')",
                "button[aria-label*='Share']",
            ],
            timeout=int(float(self.adapter_config().get("screen_share_confirm_timeout_sec", 3)) * 1000),
        )
        self.screen_sharing = True
        emit_event(self.config, "screenshare_start", {"selector": selector, "confirm_selector": confirm, "success": True}, self.service_name)
        return True

    async def stop_screen_share(self):
        if self._use_gui_keyboard():
            if not self.screen_sharing and self.adapter_config().get("trust_screen_share_shortcut_state", True):
                emit_event(
                    self.config,
                    "screenshare_stop",
                    {"selector": "cached", "success": True},
                    self.service_name,
                )
                return True
            shortcut = str(self.adapter_config().get("screen_share_shortcut", "ctrl+alt+t"))
            self._activate_window()
            self._run_xdotool(["key", shortcut], check=False)
            await asyncio.sleep(float(self.adapter_config().get("screen_share_wait_sec", 2)))
            self.screen_sharing = False
            emit_event(self.config, "screenshare_stop", {"selector": f"keyboard:{shortcut}", "success": True}, self.service_name)
            return True

        selector = await self._click_optional(
            [
                "button[aria-label*='Stop sharing']",
                "button:has-text('Stop sharing')",
                "button:has-text('Stop presenting')",
            ],
            timeout=1500,
        )
        self.screen_sharing = False
        emit_event(self.config, "screenshare_stop", {"selector": selector, "success": True}, self.service_name)
        return True

    async def is_in_meeting(self):
        if self._use_gui_keyboard():
            return bool(self.joined and self.window_id)

        return await self._is_in_meeting_dom()

    async def _is_in_meeting_dom(self):
        if self.page is None:
            return False
        selector = await self._visible_optional(
            [
                "button[aria-label*='Leave call']",
                "button[aria-label*='Present now']",
                "button[aria-label*='Turn off microphone']",
                "button[aria-label*='Turn on microphone']",
                "button:has-text('Leave call')",
            ],
            timeout=self._timeout_ms("joined_wait_sec", 30),
        )
        waiting = await self._visible_optional(
            [
                "text=/Ask(ed)? to join/i",
                "text=/You'll join when someone lets you in/i",
                "text=/Can't join/i",
                "text=/You can't join/i",
            ],
            timeout=500,
        )
        if waiting and not selector:
            emit_event(self.config, "meeting_join_waiting_or_failed", {"selector": waiting}, self.service_name)
            return False
        return bool(selector)

    def run_sanity_checks(self):
        adapter_config = self.adapter_config()
        microphone_name = str(adapter_config.get("microphone_name") or "VTC_Microphone")
        camera_name = str(adapter_config.get("camera_name") or "VTC Bot Camera")
        video_device = str(self.config.get("video_device") or self.config.get("virtual_video", {}).get("device") or "/dev/video5")
        checks = {
            "pactl_info": self._run(["pactl", "info"]),
            "pactl_default_source": self._run(["pactl", "get-default-source"]),
            "pactl_sources": self._run(["pactl", "list", "short", "sources"]),
            "v4l2": self._run(["v4l2-ctl", "--device", video_device, "--all"]),
            "ffmpeg": self._run(["ffmpeg", "-version"]),
        }
        source_text = checks["pactl_sources"]["stdout"] + checks["pactl_default_source"]["stdout"]
        audio_ok = microphone_name in source_text
        video_ok = checks["v4l2"]["returncode"] == 0 and (camera_name in checks["v4l2"]["stdout"] or Path(video_device).exists())
        ffmpeg_ok = checks["ffmpeg"]["returncode"] == 0
        details = {
            "microphone_name": microphone_name,
            "camera_name": camera_name,
            "video_device": video_device,
            "audio_ok": audio_ok,
            "video_ok": video_ok,
            "ffmpeg_ok": ffmpeg_ok,
            "gui_keyboard_ok": True,
            "checks": checks,
        }
        if self._use_gui_keyboard():
            executable_path = (
                adapter_config.get("chromium_executable_path")
                or adapter_config.get("executable_path")
                or self._find_browser_executable()
            )
            gui_keyboard_ok = shutil.which("xdotool") is not None and bool(executable_path)
            details["gui_keyboard_ok"] = gui_keyboard_ok
            details["xdotool_path"] = shutil.which("xdotool")
            details["chromium_executable_path"] = executable_path
        emit_event(self.config, "meet_sanity_check", details, self.service_name)
        if not (audio_ok and video_ok and ffmpeg_ok and details["gui_keyboard_ok"]):
            raise RuntimeError("Google Meet sanity check failed: " + json.dumps(details, sort_keys=True))

    def collect_diagnostics(self, reason: str, details: Mapping[str, Any] | None = None):
        if self._use_gui_keyboard():
            self._collect_gui_diagnostics(reason, details)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.collect_browser_log(reason)
            return
        loop.create_task(self._collect_diagnostics_async(reason, details))

    async def _collect_diagnostics_async(self, reason: str, details: Mapping[str, Any] | None = None):
        if self.page is None:
            if self._use_gui_keyboard():
                self._collect_gui_diagnostics(reason, details)
            return
        diagnostic_dir = Path(str(self.adapter_config().get("diagnostic_dir") or f"/tmp/vtc-{self.config.get('bot_name', 'bot')}/diagnostics"))
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "-", reason).strip("-") or "diagnostic"
        timestamp = int(time.time())
        base = diagnostic_dir / f"{timestamp}-{safe_reason}"
        try:
            await self.page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception:
            pass
        try:
            base.with_suffix(".html").write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass
        self.collect_browser_log(reason, base.with_suffix(".browser-log.jsonl"))
        emit_event(self.config, "browser_diagnostics_collected", {"reason": reason, "base_path": str(base), **dict(details or {})}, self.service_name)

    def collect_browser_log(self, reason: str, path: Path | None = None):
        if path is None:
            diagnostic_dir = Path(str(self.adapter_config().get("diagnostic_dir") or f"/tmp/vtc-{self.config.get('bot_name', 'bot')}/diagnostics"))
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            path = diagnostic_dir / f"{int(time.time())}-{reason}.browser-log.jsonl"
        with Path(path).open("a", encoding="utf-8") as outfile:
            for record in self.browser_log:
                outfile.write(json.dumps(record, sort_keys=True) + "\n")
        return str(path)

    async def _dismiss_common_prompts(self):
        await self._click_optional(["button:has-text('Got it')", "button:has-text('Dismiss')", "button:has-text('Allow')"], timeout=750)

    async def _fill_display_name(self, display_name: str):
        selector = await self._fill_optional(
            [
                "input[aria-label*='Your name']",
                "input[aria-label*='name']",
                "input[placeholder*='name']",
                "input[type='text']",
            ],
            display_name,
            timeout=1500,
        )
        if selector:
            emit_event(self.config, "display_name_entered", {"display_name": display_name, "selector": selector}, self.service_name)

    async def _ensure_prejoin_media_state(self):
        # Meet often preserves previous mic/camera state in the browser profile.
        if self.adapter_config().get("prejoin_mic_off"):
            await self.mute()
        if self.adapter_config().get("prejoin_camera_off"):
            await self.camera_off()

    async def _click_join_button(self):
        selectors = [
            "button:has-text('Join now')",
            "button:has-text('Ask to join')",
            "button[aria-label*='Join now']",
            "button[aria-label*='Ask to join']",
            "button:has-text('Join')",
        ]
        deadline = asyncio.get_running_loop().time() + (self._timeout_ms("join_timeout_sec", 45) / 1000)
        while asyncio.get_running_loop().time() < deadline:
            await self._raise_if_join_blocked("join_button_wait")
            selector = await self._click_optional(selectors, timeout=250)
            if selector:
                return selector
            await asyncio.sleep(0.25)
        await self._collect_diagnostics_async("join_button_missing")
        raise RuntimeError("Google Meet join button was not visible")

    async def _raise_if_join_blocked(self, stage: str):
        reason = await self._blocked_join_reason(timeout=250)
        if not reason:
            return
        await self._collect_diagnostics_async("meeting_join_blocked", {"stage": stage, "reason": reason})
        emit_event(self.config, "meeting_join_blocked", {"stage": stage, "reason": reason}, self.service_name)
        raise RuntimeError(f"Google Meet rejected the join attempt: {reason}")

    async def _blocked_join_reason(self, timeout=1000):
        blocked_states = [
            ("text=/You can.t join this video call/i", "You can't join this video call"),
            (
                "text=/No one can join a meeting unless invited or admitted by the host/i",
                "Meeting requires a host invitation or admission",
            ),
            ("text=/You can.t join/i", "You can't join"),
        ]
        for selector, reason in blocked_states:
            try:
                await self.page.locator(selector).first.wait_for(state="visible", timeout=timeout)
                return reason
            except PlaywrightTimeoutError:
                continue
        return None

    async def _set_keyboard_toggle(self, control: str, enabled: bool, shortcut: str, event_name: str):
        current = getattr(self, f"{control}_enabled")
        if current == enabled and self.adapter_config().get(f"trust_{control}_shortcut_state", True):
            emit_event(self.config, event_name, {"enabled": enabled, "method": "cached", "success": True}, self.service_name)
            return True
        if self._use_gui_keyboard():
            xdotool_shortcut = self._xdotool_shortcut(shortcut)
            self._activate_window()
            self._run_xdotool(["key", xdotool_shortcut])
            await asyncio.sleep(float(self.adapter_config().get("state_change_wait_sec", 0.25)))
            setattr(self, f"{control}_enabled", enabled)
            emit_event(
                self.config,
                event_name,
                {"enabled": enabled, "method": "keyboard", "shortcut": xdotool_shortcut, "success": True},
                self.service_name,
            )
            return True
        if self.page is None:
            return False
        await self.page.keyboard.press(shortcut)
        await self.page.wait_for_timeout(250)
        setattr(self, f"{control}_enabled", enabled)
        emit_event(self.config, event_name, {"enabled": enabled, "method": "keyboard", "shortcut": shortcut, "success": True}, self.service_name)
        return True

    async def _click_optional(self, selectors, timeout=1000):
        if self.page is None:
            return None
        for selector in selectors:
            locator = self.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click()
                return selector
            except PlaywrightTimeoutError:
                continue
        return None

    async def _fill_optional(self, selectors, text, timeout=1000):
        if self.page is None:
            return None
        for selector in selectors:
            locator = self.page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.fill(text)
                return selector
            except PlaywrightTimeoutError:
                continue
        return None

    async def _visible_optional(self, selectors, timeout=1000):
        if self.page is None:
            return None
        deadline = asyncio.get_running_loop().time() + (timeout / 1000)
        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                try:
                    await self.page.locator(selector).first.wait_for(state="visible", timeout=250)
                    return selector
                except PlaywrightTimeoutError:
                    continue
            await asyncio.sleep(0.25)
        return None

    def _use_gui_keyboard(self) -> bool:
        mode = str(self.adapter_config().get("automation_mode", "playwright")).lower().replace("-", "_")
        return mode in {"gui", "gui_keyboard", "keyboard", "xdotool"}

    async def _launch_gui_keyboard(self):
        self.run_sanity_checks()
        emit_event(self.config, "adapter_launch_start", {"browser": "chromium", "automation_mode": "gui_keyboard"}, self.service_name)
        command = self._build_gui_browser_command()
        app_log_path = Path(str(self.adapter_config().get("app_log_path") or f"/tmp/vtc-{self.config.get('bot_name', 'bot')}/google-meet.log"))
        app_log_path.parent.mkdir(parents=True, exist_ok=True)
        with app_log_path.open("a", encoding="utf-8") as app_log:
            app_log.write(f"\n--- launch {time.time()} ---\n")
            app_log.write("command=" + json.dumps(command) + "\n")
            self.browser_process = subprocess.Popen(
                command,
                stdout=app_log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                env=self._command_env(),
                start_new_session=True,
            )
        self.window_id = self._wait_for_gui_window()
        self._position_gui_window()
        emit_event(
            self.config,
            "adapter_launch_done",
            {
                "automation_mode": "gui_keyboard",
                "window_id": self.window_id,
                "app_log_path": str(app_log_path),
                "persistent_profile": True,
            },
            self.service_name,
        )
        return True

    async def _connect_to_meeting_gui_keyboard(self, vtc_url: str, display_name: str):
        if not self.window_id:
            raise RuntimeError("Google Meet Chromium window is not launched")

        emit_event(
            self.config,
            "meeting_join_start",
            {"vtc_url": vtc_url, "automation_mode": "gui_keyboard"},
            self.service_name,
        )
        if self.adapter_config().get("gui_cdp_join", True):
            try:
                await self._connect_to_meeting_gui_cdp(vtc_url, display_name)
                return
            except Exception as exc:
                emit_event(
                    self.config,
                    "gui_cdp_join_failed",
                    {"vtc_url": vtc_url, "error": str(exc)},
                    self.service_name,
                )
                self._collect_gui_diagnostics("gui_cdp_join_failed", {"error": str(exc)})
                if not self.adapter_config().get("gui_keyboard_join_fallback", True):
                    raise

        await self._connect_to_meeting_gui_keyboard_fallback(vtc_url, display_name)

    async def _connect_to_meeting_gui_cdp(self, vtc_url: str, display_name: str):
        page = await self._ensure_gui_cdp_page()
        emit_event(self.config, "meeting_join_cdp_start", {"vtc_url": vtc_url}, self.service_name)
        await page.goto(vtc_url, wait_until="domcontentloaded", timeout=self._timeout_ms("goto_timeout_sec", 60))
        await page.wait_for_timeout(int(float(self.adapter_config().get("page_load_wait_sec", 4)) * 1000))
        self._activate_window()
        await self._dismiss_common_prompts()
        await self._fill_display_name(display_name)
        await self._ensure_prejoin_media_state()
        await self._raise_if_join_blocked("pre_join")
        if await self._is_in_meeting_dom():
            join_selector = "cdp:already_in_meeting"
        else:
            join_selector = await self._click_join_button()
        joined = await self._is_in_meeting_dom()
        if not joined:
            await self._collect_diagnostics_async("meeting_join_failed", {"join_selector": join_selector, "method": "cdp"})
            emit_event(
                self.config,
                "meeting_join_failed",
                {"vtc_url": vtc_url, "join_selector": join_selector, "method": "cdp"},
                self.service_name,
            )
            raise RuntimeError("Google Meet join did not reach in-call state")

        self.joined = True
        window_title = self._window_title()
        if not self._is_valid_meeting_window_title(window_title, vtc_url):
            self._collect_gui_diagnostics("meeting_window_invalid_after_join", {"window_title": window_title, "method": "cdp"})
            raise RuntimeError(f"Google Meet join did not land on a meeting window: {window_title}")
        self._notify_callback("_meeting_joined_callback", vtc_url)
        self._notify_callback("_media_ready_callback", vtc_url)
        self._collect_gui_diagnostics("meeting_join_success", {"window_title": window_title, "method": "cdp"})
        emit_event(
            self.config,
            "meeting_join_success",
            {"vtc_url": vtc_url, "join_selector": join_selector, "window_title": window_title, "method": "cdp"},
            self.service_name,
        )

    async def _connect_to_meeting_gui_keyboard_fallback(self, vtc_url: str, display_name: str):
        type_delay = str(int(float(self.adapter_config().get("type_delay_ms", 1))))
        self._activate_window()
        self._run_xdotool(["key", "ctrl+l"])
        self._run_xdotool(["type", "--delay", type_delay, vtc_url])
        self._run_xdotool(["key", "Return"])
        emit_event(self.config, "meeting_url_entered", {"method": "keyboard", "vtc_url": vtc_url}, self.service_name)

        await asyncio.sleep(float(self.adapter_config().get("gui_prejoin_wait_sec", self.adapter_config().get("page_load_wait_sec", 10))))
        if not self._activate_meeting_window(vtc_url):
            window_title = self._window_title()
            self._collect_gui_diagnostics("meeting_window_missing_after_url", {"window_title": window_title})
            raise RuntimeError(f"Google Meet window was not found after URL entry: {window_title}")
        self._run_xdotool(["key", "Escape"], check=False)
        emit_event(self.config, "meet_popup_dismissed", {"method": "keyboard", "shortcut": "Escape"}, self.service_name)

        await asyncio.sleep(float(self.adapter_config().get("gui_after_escape_wait_sec", 0.5)))
        self._run_xdotool(["type", "--delay", type_delay, display_name])
        emit_event(self.config, "display_name_entered", {"display_name": display_name, "method": "keyboard"}, self.service_name)
        self._run_xdotool(["key", "Return"])
        emit_event(self.config, "join_meeting_clicked", {"method": "keyboard", "shortcut": "Return"}, self.service_name)

        await asyncio.sleep(float(self.adapter_config().get("gui_join_wait_sec", self.adapter_config().get("joined_wait_sec", 15))))
        if not self._activate_meeting_window(vtc_url):
            window_title = self._window_title()
            self._collect_gui_diagnostics("meeting_window_missing_after_join", {"window_title": window_title})
            raise RuntimeError(f"Google Meet window was not found after join: {window_title}")
        window_title = self._window_title()
        if not self._is_valid_meeting_window_title(window_title, vtc_url):
            self._collect_gui_diagnostics("meeting_window_invalid_after_join", {"window_title": window_title})
            raise RuntimeError(f"Google Meet join did not land on a meeting window: {window_title}")
        if self.page:
            await self._raise_if_join_blocked("keyboard_join")
            if not await self._is_in_meeting_dom():
                self._collect_gui_diagnostics("meeting_join_failed_after_keyboard", {"window_title": window_title})
                raise RuntimeError("Google Meet keyboard join did not reach in-call state")
        self.joined = True
        self._notify_callback("_meeting_joined_callback", vtc_url)
        self._notify_callback("_media_ready_callback", vtc_url)
        self._collect_gui_diagnostics("meeting_join_success", {"window_title": window_title})
        emit_event(
            self.config,
            "meeting_join_success",
            {"vtc_url": vtc_url, "join_selector": "keyboard:Return", "window_title": window_title},
            self.service_name,
        )

    async def _ensure_gui_cdp_page(self):
        if self.page and not self.page.is_closed():
            return self.page
        from playwright.async_api import async_playwright

        port = int(float(self.adapter_config().get("remote_debugging_port", 9222)))
        endpoint = f"http://127.0.0.1:{port}"
        deadline = time.time() + float(self.adapter_config().get("cdp_connect_timeout_sec", 10))
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.connect_over_cdp(endpoint)
                break
            except Exception as exc:
                last_error = exc
                if self.playwright:
                    try:
                        await self.playwright.stop()
                    except Exception:
                        pass
                    self.playwright = None
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"Could not connect to Chromium CDP at {endpoint}: {last_error}")

        contexts = self.browser.contexts if self.browser else []
        if not contexts:
            raise RuntimeError(f"Chromium CDP at {endpoint} had no browser contexts")
        pages = [page for context in contexts for page in context.pages]
        self.context = contexts[0]
        self.page = pages[0] if pages else await self.context.new_page()
        self.page.on("console", self._record_console)
        self.page.on("pageerror", self._record_page_error)
        return self.page

    def _build_gui_browser_command(self) -> list[str]:
        executable_path = (
            self.adapter_config().get("chromium_executable_path")
            or self.adapter_config().get("executable_path")
            or self._find_browser_executable()
        )
        if not executable_path:
            raise RuntimeError("Chromium executable not found for Google Meet GUI automation")

        args = [
            str(executable_path),
            f"--user-data-dir={self._gui_user_data_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1280,720",
            "--lang=en-US",
        ]
        if self.adapter_config().get("ignore_certificate_errors", True):
            args.append("--ignore-certificate-errors")
        if self.adapter_config().get("disable_gpu", True):
            args.extend(["--disable-gpu", "--disable-gpu-compositing"])
        capture_source = self.adapter_config().get("auto_select_desktop_capture_source") or self.adapter_config().get("screen_share_target")
        if capture_source:
            args.append(f"--auto-select-desktop-capture-source={capture_source}")
        args.extend(str(arg) for arg in self.adapter_config().get("extra_browser_args", []))
        debug_arg = "--remote-debugging-port"
        if self.adapter_config().get("remote_debugging_port", 9222) and not any(
            str(arg).split("=", 1)[0] == debug_arg for arg in args
        ):
            args.append(f"{debug_arg}={int(float(self.adapter_config().get('remote_debugging_port', 9222)))}")
        args.append(str(self.adapter_config().get("initial_browser_url", "about:blank")))
        return args

    def _gui_user_data_dir(self) -> Path:
        configured = self.adapter_config().get("user_data_dir") or os.environ.get("VTC_MEET_CHROME_USER_DATA_DIR")
        if configured and "${" not in str(configured):
            path = Path(str(configured)).expanduser()
        else:
            bot_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(self.config.get("bot_name") or "bot")).strip("-") or "bot"
            run_id = str(self.config.get("execution_id") or self.config.get("run_id") or int(time.time()))
            safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id).strip("-") or "run"
            path = Path(f"/tmp/vtc-{bot_name}/chrome-google-meet-{safe_run_id}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _wait_for_gui_window(self) -> str:
        deadline = time.time() + self.launch_timeout_sec
        last_error = ""
        while time.time() < deadline:
            if self.browser_process and self.browser_process.poll() is not None:
                raise RuntimeError(f"Chromium exited before a window appeared with code {self.browser_process.returncode}")
            for args in self._window_searches():
                result = self._run_xdotool(args, check=False, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    window_id = result.stdout.strip().splitlines()[-1]
                    self.window_id = window_id
                    self._activate_window()
                    return window_id
                last_error = result.stderr or result.stdout
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for Google Meet Chromium window. Last output: {last_error}")

    def _window_searches(self) -> list[list[str]]:
        configured = self.adapter_config().get("window_title_regex")
        searches = [
            ["search", "--onlyvisible", "--class", "chromium"],
            ["search", "--onlyvisible", "--class", "google-chrome"],
            ["search", "--onlyvisible", "--name", "Chromium"],
            ["search", "--onlyvisible", "--name", "Google Chrome"],
        ]
        if configured:
            if isinstance(configured, str):
                configured_patterns = [configured]
            else:
                configured_patterns = [str(item) for item in configured]
            searches = [["search", "--onlyvisible", "--name", pattern] for pattern in configured_patterns] + searches
        return searches

    def _activate_meeting_window(self, vtc_url: str) -> bool:
        meeting_code = vtc_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        patterns = [
            str(self.adapter_config().get("meeting_window_regex") or ""),
            re.escape(meeting_code) if meeting_code else "",
            re.escape(vtc_url.split("#", 1)[0]),
        ]
        deadline = time.time() + float(self.adapter_config().get("meeting_window_wait_sec", 5))
        while time.time() < deadline:
            for pattern in [item for item in patterns if item]:
                result = self._run_xdotool(["search", "--onlyvisible", "--name", pattern], check=False, timeout=2)
                if result.returncode == 0 and result.stdout.strip():
                    self.window_id = result.stdout.strip().splitlines()[-1]
                    self._activate_window()
                    return True
            time.sleep(0.25)
        self._activate_window()
        return False

    def _is_valid_meeting_window_title(self, title: str | None, vtc_url: str) -> bool:
        if not title:
            return False
        normalized = title.lower()
        if "about:blank" in normalized:
            return False
        if "vtc share window" in normalized:
            return False
        meeting_code = vtc_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].lower()
        return bool(meeting_code and meeting_code in normalized) or "meet" in normalized

    def _position_gui_window(self) -> None:
        geometry = self.adapter_config().get("window_geometry")
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

    async def _select_screen_share_target_gui_keyboard(self) -> str | None:
        target_title = str(self.adapter_config().get("screen_share_target") or "VTC Share Window")
        coordinates = self.adapter_config().get("screen_share_target_coordinates")
        if isinstance(coordinates, Mapping):
            x = int(coordinates.get("x", 455))
            y = int(coordinates.get("y", 295))
        else:
            # Chromium's native desktop picker is outside the page DOM. With the
            # fixed 1280x720 Meet window, the first "Chromium Tab" entry lands here.
            x = int(self.adapter_config().get("screen_share_target_x", 455))
            y = int(self.adapter_config().get("screen_share_target_y", 295))
        self._run_xdotool(["mousemove", str(x), str(y), "click", "1"], check=False)
        await asyncio.sleep(float(self.adapter_config().get("screen_share_target_select_wait_sec", 0.5)))
        confirm_key = str(self.adapter_config().get("screen_share_confirm_key", "Return"))
        self._run_xdotool(["key", confirm_key], check=False)
        await asyncio.sleep(float(self.adapter_config().get("screen_share_post_confirm_wait_sec", 2)))
        self._collect_gui_diagnostics(
            "screen_share_target_selected",
            {
                "target_title": target_title,
                "coordinates": {"x": x, "y": y},
                "confirm_key": confirm_key,
            },
        )
        return f"xdotool:{target_title}@{x},{y}+{confirm_key}"

    def _activate_window(self):
        if not self.window_id:
            return
        self._run_xdotool(["windowraise", str(self.window_id)], check=False)
        self._run_xdotool(["windowactivate", "--sync", str(self.window_id)], check=False)

    def _window_title(self) -> str | None:
        if not self.window_id:
            return None
        result = self._run_xdotool(["getwindowname", str(self.window_id)], check=False, timeout=2)
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _collect_gui_diagnostics(self, reason: str, details: Mapping[str, Any] | None = None):
        diagnostic_dir = Path(str(self.adapter_config().get("diagnostic_dir") or f"/tmp/vtc-{self.config.get('bot_name', 'bot')}/diagnostics"))
        diagnostic_dir.mkdir(parents=True, exist_ok=True)
        safe_reason = re.sub(r"[^A-Za-z0-9_.-]+", "-", reason).strip("-") or "diagnostic"
        base = diagnostic_dir / f"{int(time.time())}-{safe_reason}"
        screenshot_path = base.with_suffix(".png")
        if shutil.which("import"):
            self._run_command(["import", "-window", "root", str(screenshot_path)], check=False, timeout=5)
        diagnostics = {
            "reason": reason,
            "details": dict(details or {}),
            "display": self.display,
            "window_id": self.window_id,
            "window_title": self._window_title(),
            "browser_process_pid": self.browser_process.pid if self.browser_process else None,
            "screenshot_path": str(screenshot_path),
            "screenshot_exists": screenshot_path.exists(),
            "commands": {},
        }
        command_map = {
            "wmctrl_windows": ["wmctrl", "-lG"],
            "xdotool_visible_windows": ["xdotool", "search", "--onlyvisible", "--name", "."],
            "processes": ["ps", "-ef"],
            "pactl_sources": ["pactl", "list", "short", "sources"],
            "pactl_sink_inputs": ["pactl", "list", "sink-inputs"],
        }
        for key, command in command_map.items():
            result = self._run_command(command, check=False, timeout=5)
            diagnostics["commands"][key] = {
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout.strip()[:4000],
                "stderr": result.stderr.strip()[:4000],
            }
        base.with_suffix(".json").write_text(json.dumps(diagnostics, indent=2, sort_keys=True), encoding="utf-8")
        self.collect_browser_log(reason, base.with_suffix(".browser-log.jsonl"))
        emit_event(
            self.config,
            "browser_diagnostics_collected",
            {"reason": reason, "base_path": str(base), **dict(details or {})},
            self.service_name,
        )

    def _xdotool_shortcut(self, shortcut: str) -> str:
        return shortcut.replace("Control", "ctrl").replace("+", "+").lower()

    def _command_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        extra_env = self.adapter_config().get("env", {})
        if isinstance(extra_env, Mapping):
            env.update({str(key): str(value) for key, value in extra_env.items()})
        return env

    def _run_xdotool(self, args: list[str], check: bool = True, timeout: int | None = None):
        return self._run_command(["xdotool", *args], check=check, timeout=timeout)

    def _run_command(self, command: list[str], check: bool = True, timeout: int | None = None):
        if shutil.which(command[0]) is None:
            result = subprocess.CompletedProcess(command, 127, "", f"{command[0]} not found")
        else:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout or self.action_timeout_sec,
                env=self._command_env(),
            )
        self.browser_log.append(
            {
                "ts": time.time(),
                "type": "command",
                "command": command,
                "returncode": result.returncode,
                "stdout": result.stdout.strip()[:2000],
                "stderr": result.stderr.strip()[:2000],
            }
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {command}\n"
                f"stdout={result.stdout}\nstderr={result.stderr}"
            )
        return result

    def _timeout_ms(self, key: str, default_sec: float):
        return int(float(self.adapter_config().get(key, default_sec)) * 1000)

    def _display_name(self):
        adapter_display_name = self.adapter_config().get("display_name")
        if adapter_display_name:
            return str(adapter_display_name)
        bot = self.config.get("bot")
        if isinstance(bot, Mapping) and bot.get("display_name"):
            return str(bot["display_name"])
        return str(self.config.get("bot_name") or "bot")

    def _notify_callback(self, callback_key: str, vtc_url: str):
        callback = self.config.get(callback_key)
        if callable(callback):
            callback(vtc_url)

    def _record_console(self, message):
        self.browser_log.append({"ts": time.time(), "type": "console", "level": message.type, "text": message.text})

    def _record_page_error(self, error):
        record = {"ts": time.time(), "type": "pageerror", "text": str(error)}
        self.browser_log.append(record)
        emit_event(self.config, "browser_error", record, self.service_name)

    def _run(self, command):
        if shutil.which(command[0]) is None:
            return {"command": command, "returncode": 127, "stdout": "", "stderr": f"{command[0]} not found"}
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        return {
            "command": command,
            "returncode": result.returncode,
            "stdout": result.stdout.strip()[:4000],
            "stderr": result.stderr.strip()[:4000],
        }

    def _find_browser_executable(self):
        for candidate in ("google-chrome", "chromium", "chromium-browser"):
            path = shutil.which(candidate)
            if path:
                return path
        return None
