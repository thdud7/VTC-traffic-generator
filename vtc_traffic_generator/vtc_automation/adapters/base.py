import abc


class ServiceAdapter(abc.ABC):
    """Base class for VTC service automation adapters."""

    service_name = "base"
    supported_modes = ()

    def __init__(self, config):
        self.config = config

    @abc.abstractmethod
    async def connect(self, duration):
        """Join a meeting and keep the client connected for duration minutes."""
        raise NotImplementedError

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
