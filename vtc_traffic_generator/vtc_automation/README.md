# VTC Service Automation

Service-specific meeting automation is isolated behind adapters in
`vtc_automation/adapters`. Common client work, such as virtual audio/video
setup, controller RPC, media playback, packet capture, and event logging should
stay outside the adapters.

## Adapter Selection

The client selects an adapter from `vtc_platform` in the remote config.

```json
{
  "role": "client",
  "vtc_platform": "jitsi",
  "vtc_url": "https://meet.jit.si/example-room",
  "adapter_config": {}
}
```

Supported adapter keys are registered in `adapters/registry.py`.

## Current Services

| Service | Adapter key | Initial mode | Notes |
| --- | --- | --- | --- |
| Jitsi | `jitsi` | Chrome web | First smoke-test target. |
| Webex | `webex` | Chrome web | Service branch should add join selectors and login handling. |
| Google Meet | `google_meet` | Chrome web | Chrome web is the planned path. |
| BigBlueButton | `bigbluebutton`, `bbb` | Chrome web | Replaces the earlier Linphone target. |
| Teams | `teams` | Chrome web | Keep native/PWA differences inside this adapter. |
| Zoom | `zoom` | Chrome web | Native client support can be added in the same adapter later. |
| Messenger | `messenger` | Chrome web | Login/session handling will be adapter-specific. |
| Discord | `discord` | Chrome web, native candidate | Discord currently advertises Linux desktop downloads; verify the native client in UTM before relying on accessibility automation. |

## Branching Model

- Keep shared interfaces, controller/client plumbing, logging, capture, and
  environment setup on `dev`.
- Create service branches for adapter implementation work, for example:
  `service/jitsi`, `service/zoom`, or `service/discord`.
- Service branches should mostly touch files under
  `vtc_automation/adapters/<service>.py` and any service-specific fixtures.

## Adapter Contract

Each adapter subclasses `ServiceAdapter` and implements:

```python
async def connect(self, duration):
    ...
```

Optional action methods are already defined on the base class for future
state-driven orchestration:

- `start_screen_share`
- `stop_screen_share`
- `mute_microphone`
- `unmute_microphone`
- `start_camera`
- `stop_camera`

Adapters should translate those high-level actions into service-specific UI
automation. The controller should not know whether an action is implemented via
Playwright, dogtail, xdotool, or another backend.
