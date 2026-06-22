import asyncio

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except ModuleNotFoundError:
    PlaywrightTimeoutError = TimeoutError

from .base import ServiceAdapter


DEFAULT_VIEWPORT = {"width": 1280, "height": 720}


class BrowserMeetingAdapter(ServiceAdapter):
    """Shared Playwright flow for services that can run in Chrome/Chromium."""

    supported_modes = ("web",)

    def browser_args(self):
        return [
            "--use-fake-ui-for-media-stream",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]

    def adapter_config(self):
        return self.config.get("adapter_config", {})

    def launch_options(self):
        options = {
            "args": self.browser_args(),
            "headless": False,
        }

        adapter_config = self.adapter_config()
        executable_path = adapter_config.get("chromium_executable_path")
        if executable_path:
            options["executable_path"] = executable_path

        browser_channel = adapter_config.get("browser_channel")
        if browser_channel:
            options["channel"] = browser_channel

        slow_mo = adapter_config.get("slow_mo")
        if slow_mo is not None:
            options["slow_mo"] = slow_mo

        return options

    def context_options(self):
        adapter_config = self.adapter_config()
        return {
            "ignore_https_errors": True,
            "permissions": adapter_config.get("permissions", ["camera", "microphone"]),
            "viewport": adapter_config.get("viewport", DEFAULT_VIEWPORT),
        }

    async def connect(self, duration):
        from playwright.async_api import async_playwright

        meeting_url = self.config["vtc_url"]
        print(f"Connecting to {self.service_name} meeting: {meeting_url}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(**self.launch_options())
            context = await browser.new_context(**self.context_options())
            page = await context.new_page()
            try:
                await self.join_meeting(page, meeting_url)
                await asyncio.sleep(duration * 60)
            finally:
                await browser.close()

        return f"{self.config['bot_name']} connected to {self.service_name}."

    async def join_meeting(self, page, meeting_url):
        await page.goto(meeting_url)
        print(await page.title())

    async def click_first_visible(self, page, selectors, timeout=1000):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click()
                return selector
            except PlaywrightTimeoutError:
                continue

        raise RuntimeError(f"No visible selector matched: {selectors}")

    async def fill_first_visible(self, page, selectors, text, timeout=1000):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.fill(text)
                return selector
            except PlaywrightTimeoutError:
                continue

        raise RuntimeError(f"No visible selector matched: {selectors}")

    async def click_if_visible(self, page, selectors, timeout=500):
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click()
                return selector
            except PlaywrightTimeoutError:
                continue

        return None

    async def wait_for_any_visible(self, page, selectors, timeout=10000):
        deadline = asyncio.get_running_loop().time() + (timeout / 1000)
        last_error = None

        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                locator = page.locator(selector).first
                try:
                    await locator.wait_for(state="visible", timeout=250)
                    return selector
                except PlaywrightTimeoutError as exc:
                    last_error = exc
            await asyncio.sleep(0.25)

        raise RuntimeError(f"No visible selector matched before timeout: {selectors}") from last_error
