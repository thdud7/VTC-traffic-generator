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
        self.browser_log: list[dict[str, Any]] = []
        self.mic_enabled = bool(self.adapter_config().get("initial_mic_enabled", True))
        self.camera_enabled = bool(self.adapter_config().get("initial_camera_enabled", True))
        self.screen_sharing = bool(self.adapter_config().get("initial_screen_sharing", False))

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
        if self.page is None:
            raise RuntimeError("Google Meet browser page is not launched")

        emit_event(self.config, "meeting_join_start", {"vtc_url": vtc_url}, self.service_name)
        await self.page.goto(vtc_url, wait_until="domcontentloaded", timeout=self._timeout_ms("goto_timeout_sec", 60))
        await self.page.wait_for_timeout(int(float(self.adapter_config().get("page_load_wait_sec", 4)) * 1000))
        await self._dismiss_common_prompts()
        await self._fill_display_name(display_name)
        await self._ensure_prejoin_media_state()
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
            "checks": checks,
        }
        emit_event(self.config, "meet_sanity_check", details, self.service_name)
        if not (audio_ok and video_ok and ffmpeg_ok):
            raise RuntimeError("Google Meet sanity check failed: " + json.dumps(details, sort_keys=True))

    def collect_diagnostics(self, reason: str, details: Mapping[str, Any] | None = None):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.collect_browser_log(reason)
            return
        loop.create_task(self._collect_diagnostics_async(reason, details))

    async def _collect_diagnostics_async(self, reason: str, details: Mapping[str, Any] | None = None):
        if self.page is None:
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
        selector = await self._click_optional(selectors, timeout=self._timeout_ms("join_timeout_sec", 45))
        if selector:
            return selector
        await self._collect_diagnostics_async("join_button_missing")
        raise RuntimeError("Google Meet join button was not visible")

    async def _set_keyboard_toggle(self, control: str, enabled: bool, shortcut: str, event_name: str):
        current = getattr(self, f"{control}_enabled")
        if current == enabled and self.adapter_config().get(f"trust_{control}_shortcut_state", True):
            emit_event(self.config, event_name, {"enabled": enabled, "method": "cached", "success": True}, self.service_name)
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

    def _timeout_ms(self, key: str, default_sec: float):
        return int(float(self.adapter_config().get(key, default_sec)) * 1000)

    def _display_name(self):
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
