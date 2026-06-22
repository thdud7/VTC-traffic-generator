import abc
from typing import Any, Mapping


class ServiceAdapter(abc.ABC):
    """Base class for VTC service automation adapters."""

    service_name = "base"
    supported_modes = ()

    def __init__(self, config: Mapping[str, Any]):
        self.config = config

    async def launch(self):
        """Start the VTC application if the adapter needs an explicit launch."""
        return None

    @abc.abstractmethod
    async def connect(self, duration):
        """Join a meeting and keep the client connected for duration minutes."""
        raise NotImplementedError

    async def leave(self):
        raise NotImplementedError(f"{self.service_name} does not support leave yet")

    async def close(self):
        raise NotImplementedError(f"{self.service_name} does not support close yet")

    async def mute(self):
        return await self.mute_microphone()

    async def unmute(self):
        return await self.unmute_microphone()

    async def camera_on(self):
        return await self.start_camera()

    async def camera_off(self):
        return await self.stop_camera()

    async def select_devices(self, camera_name: str, microphone_name: str):
        raise NotImplementedError(f"{self.service_name} does not support device selection yet")

    async def is_in_meeting(self):
        raise NotImplementedError(f"{self.service_name} does not support meeting-state checks yet")

    async def start_screen_share(self):
        raise NotImplementedError(f"{self.service_name} does not support screen share yet")

    async def stop_screen_share(self):
        raise NotImplementedError(f"{self.service_name} does not support screen share yet")

    async def mute_microphone(self):
        raise NotImplementedError(f"{self.service_name} does not support microphone control yet")

    async def unmute_microphone(self):
        raise NotImplementedError(f"{self.service_name} does not support microphone control yet")

    async def start_camera(self):
        raise NotImplementedError(f"{self.service_name} does not support camera control yet")

    async def stop_camera(self):
        raise NotImplementedError(f"{self.service_name} does not support camera control yet")
