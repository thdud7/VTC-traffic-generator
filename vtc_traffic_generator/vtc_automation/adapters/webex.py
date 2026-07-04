import asyncio
import subprocess
from typing import Any, Mapping

from vtc_automation.event_log import emit_event

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

    def adapter_config(self):
        return self.config.get("adapter_config", {})

    def browser_args(self):
        args = super().browser_args()
        args.extend(
            [
                "--autoplay-policy=no-user-gesture-required",
                "--disable-gpu",
                "--disable-gpu-compositing",
            ]
        )
        return self.adapter_config().get("browser_args", args)

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

        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(**self.launch_options())
        self.context = await self.browser.new_context(**self.context_options())
        self.page = await self.context.new_page()
        emit_event(self.config, "adapter_launched", {"backend": "playwright"}, self.service_name)
        return None

    async def connect(self, duration):
        await self.launch()
        await self.connect_to_meeting(
            str(self.config["vtc_url"]),
            self._display_name(),
        )
        await asyncio.sleep(duration * 60)
        await self.leave()
        await self.close()
        return f"{self.config.get('bot_name') or 'client'} connected to {self.service_name}."

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
