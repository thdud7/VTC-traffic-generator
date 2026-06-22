from .browser import BrowserMeetingAdapter


class JitsiAdapter(BrowserMeetingAdapter):
    service_name = "jitsi"

    DEFAULT_NAME_SELECTORS = [
        'input[aria-label="Enter your name"]',
        'input[placeholder="Enter your name"]',
        'input[aria-label="Name"]',
        'input[placeholder="Name"]',
        'input[name="displayName"]',
    ]
    DEFAULT_JOIN_BUTTON_SELECTORS = [
        'button:has-text("Join meeting")',
        'button:has-text("Join now")',
        'button:has-text("Ask to join")',
        'button:has-text("Join")',
        '[aria-label="Join meeting"]',
    ]
    DEFAULT_JOINED_SELECTORS = [
        '#new-toolbox',
        '#largeVideo',
        '[aria-label="Leave the meeting"]',
        'button:has-text("Leave")',
    ]
    MIC_ON_SELECTORS = [
        '[aria-label="Unmute microphone"]',
    ]
    MIC_OFF_SELECTORS = [
        '[aria-label="Mute microphone"]',
    ]
    CAMERA_ON_SELECTORS = [
        '[aria-label="Start camera"]',
        'button:has-text("Start camera")',
    ]
    CAMERA_OFF_SELECTORS = [
        '[aria-label="Stop camera"]',
        'button:has-text("Stop camera")',
    ]

    async def join_meeting(self, page, meeting_url):
        adapter_config = self.adapter_config()
        prejoin_timeout = adapter_config.get("prejoin_timeout_ms", 30000)
        joined_timeout = adapter_config.get("joined_timeout_ms", 30000)

        await page.goto(meeting_url, wait_until="domcontentloaded")
        print(await page.title())

        if adapter_config.get("skip_prejoin"):
            return

        await self.set_display_name(page)
        await self.ensure_microphone_state(page)
        await self.ensure_camera_state(page)
        await self.click_join(page, prejoin_timeout)

        if adapter_config.get("verify_joined", True):
            await self.wait_for_joined(page, joined_timeout)

    async def set_display_name(self, page):
        adapter_config = self.adapter_config()
        display_name = adapter_config.get("display_name", self.config["bot_name"])
        selectors = adapter_config.get("name_selectors", self.DEFAULT_NAME_SELECTORS)
        selector = await self.click_if_visible(page, selectors)

        if selector:
            await page.locator(selector).first.fill(display_name)

    async def ensure_microphone_state(self, page):
        adapter_config = self.adapter_config()
        enabled = adapter_config.get("initial_microphone_enabled", True)

        if enabled:
            await self.click_if_visible(page, self.MIC_ON_SELECTORS)
        else:
            await self.click_if_visible(page, self.MIC_OFF_SELECTORS)

    async def ensure_camera_state(self, page):
        adapter_config = self.adapter_config()
        enabled = adapter_config.get("initial_camera_enabled", True)

        if enabled:
            await self.click_if_visible(page, self.CAMERA_ON_SELECTORS)
        else:
            await self.click_if_visible(page, self.CAMERA_OFF_SELECTORS)

    async def click_join(self, page, timeout):
        adapter_config = self.adapter_config()
        selectors = adapter_config.get("join_button_selectors", self.DEFAULT_JOIN_BUTTON_SELECTORS)
        await self.click_first_visible(page, selectors, timeout=timeout)

    async def wait_for_joined(self, page, timeout):
        adapter_config = self.adapter_config()
        selectors = adapter_config.get("joined_selectors", self.DEFAULT_JOINED_SELECTORS)
        await self.wait_for_any_visible(page, selectors, timeout=timeout)
