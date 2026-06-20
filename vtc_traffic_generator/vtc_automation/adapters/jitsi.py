from .browser import BrowserMeetingAdapter


class JitsiAdapter(BrowserMeetingAdapter):
    service_name = "jitsi"

    async def join_meeting(self, page, meeting_url):
        await page.goto(meeting_url)
        print(await page.title())
