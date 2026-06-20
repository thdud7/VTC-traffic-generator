from .bigbluebutton import BigBlueButtonAdapter
from .discord import DiscordAdapter
from .google_meet import GoogleMeetAdapter
from .jitsi import JitsiAdapter
from .messenger import MessengerAdapter
from .teams import TeamsAdapter
from .webex import WebexAdapter
from .zoom import ZoomAdapter


ADAPTERS = {
    "bigbluebutton": BigBlueButtonAdapter,
    "bbb": BigBlueButtonAdapter,
    "discord": DiscordAdapter,
    "google_meet": GoogleMeetAdapter,
    "google-meet": GoogleMeetAdapter,
    "googlemeet": GoogleMeetAdapter,
    "jitsi": JitsiAdapter,
    "messenger": MessengerAdapter,
    "teams": TeamsAdapter,
    "webex": WebexAdapter,
    "zoom": ZoomAdapter,
}


def normalize_service_name(service_name):
    return service_name.strip().lower().replace(" ", "_")


def get_adapter(config):
    service_name = config.get("vtc_platform") or config.get("service")
    if not service_name:
        raise ValueError("Missing service name. Set vtc_platform in the config.")

    normalized = normalize_service_name(service_name)
    adapter_class = ADAPTERS.get(normalized)
    if adapter_class is None:
        supported = ", ".join(list_supported_services())
        raise ValueError(f"Unsupported VTC service '{service_name}'. Supported: {supported}")

    return adapter_class(config)


def list_supported_services():
    return sorted(ADAPTERS.keys())
