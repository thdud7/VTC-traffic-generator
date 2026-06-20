import asyncio

from playwright.async_api import async_playwright

from .base import ServiceAdapter


class BrowserMeetingAdapter(ServiceAdapter):
    """Shared Playwright flow for services that can run in Chrome/Chromium."""

    supported_modes = ("web",)

    def browser_args(self):
        return [
            "--use-fake-ui-for-media-stream",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ]

    async def connect(self, duration):
        meeting_url = self.config["vtc_url"]
        print(f"Connecting to {self.service_name} meeting: {meeting_url}")

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                args=self.browser_args(),
                headless=False,
            )
            page = await browser.new_page(ignore_https_errors=True)
            await self.join_meeting(page, meeting_url)
            await asyncio.sleep(duration * 60)
            await browser.close()

        return f"{self.config['bot_name']} connected to {self.service_name}."

    async def join_meeting(self, page, meeting_url):
        await page.goto(meeting_url)
        print(await page.title())
