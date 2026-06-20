from .browser import BrowserMeetingAdapter


class DiscordAdapter(BrowserMeetingAdapter):
    service_name = "discord"
    supported_modes = ("web", "native")
