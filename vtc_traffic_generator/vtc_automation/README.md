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
| Jitsi Electron | `jitsi_electron` | Native Electron | First native-app adapter target. |

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

## Jitsi Adapter

The Jitsi adapter currently automates the Chrome web flow:

1. Open `vtc_url`.
2. Fill the prejoin display name from `adapter_config.display_name` or
   `bot_name`.
3. Match initial microphone and camera state.
4. Click the join button.
5. Wait for the in-meeting UI.

Example config:

```json
{
  "vtc_platform": "jitsi",
  "vtc_url": "https://meet.jit.si/example-room",
  "bot_name": "bot-utm-1",
  "adapter_config": {
    "display_name": "bot-utm-1",
    "initial_microphone_enabled": true,
    "initial_camera_enabled": true,
    "prejoin_timeout_ms": 30000,
    "joined_timeout_ms": 30000,
    "verify_joined": true
  }
}
```

If a self-hosted Jitsi deployment has custom labels or a different prejoin
screen, override `name_selectors`, `join_button_selectors`, or
`joined_selectors` in `adapter_config`.

## Jitsi Electron Adapter

The `jitsi_electron` adapter targets Ubuntu Server VM + Xvfb + openbox +
xdotool + Jitsi Meet Electron AppImage/script.

It currently:

1. Starts Jitsi Electron on `adapter_config.display` (`:99` by default).
2. Tracks `adapter_config.display_backend` (`xvfb` or `xorg_dummy`) so screen
   share failures include the display backend being tested.
3. Passes Electron flags such as `--no-sandbox`, `--disable-gpu`, and
   `--ignore-certificate-errors`.
4. Waits for the app window with `xdotool search`.
5. Enters the full `vtc_url` into the app.
6. Dumps the dogtail/AT-SPI accessibility tree at configured stages.
7. Opens settings and tries to select `camera_name` and `microphone_name` by
   exact accessibility name first, then partial match.
8. Enters the display name and clicks Join with accessibility first, then
   coordinate or keyboard fallback.
9. For in-meeting mic/camera/screen-share toggles, infers current state from
   accessibility labels before acting. If state is known and a change is needed,
   it tries Jitsi shortcuts first: `m` for mic, `v` for camera, and `d` for
   screen share. It avoids blind repeated toggles when state is unknown.
10. Uses dogtail/AT-SPI for runtime tree traversal. Accerciser is only for
    manual inspection during development.
11. Writes UTC JSONL events to `adapter_config.event_log_path`.

Example client config:

```json
{
  "role": "client",
  "vtc_platform": "jitsi_electron",
  "service": "jitsi_electron",
  "vtc_url": "https://192.168.0.7:8443/testroom",
  "bot_name": "bot1",
  "bot": {
    "display_name": "bot1"
  },
  "adapter_config": {
    "display": ":99",
    "display_backend": "xvfb",
    "executable_path": "/home/soyoung/service/jitsi-electron/run-jitsi.sh",
    "ignore_certificate_errors": true,
    "camera_name": "VTC Bot Camera",
    "microphone_name": "VTC_Microphone",
    "screen_share_target": null,
    "window_title_regex": "Jitsi Meet|testroom|jitsi",
    "accessibility_window_regex": "Jitsi Meet|testroom|jitsi",
    "launch_timeout_sec": 20,
    "action_timeout_sec": 10,
    "event_log_path": "/tmp/vtc-events.jsonl",
    "app_log_path": "/tmp/jitsi-electron.log",
    "adapter_log_path": "/tmp/vtc-jitsi-adapter.log",
    "accessibility_dump_path": "/tmp/jitsi-{stage}-tree.json",
    "skip_device_selection": false,
    "accessibility_names": {},
    "accessibility_roles": {},
    "coordinates": {
      "name_input": null,
      "join_button": null,
      "mic_button": null,
      "camera_button": null,
      "hangup_button": null,
      "device_settings_button": null,
      "screen_share_button": null,
      "screen_share_target": null,
      "screen_share_confirm": null
    }
  }
}
```

Smoke-test the adapter without the controller:

```bash
PYTHONPATH=vtc_traffic_generator python3 -m vtc_automation.test_adapter \
  --service jitsi_electron \
  --vtc-url "https://192.168.0.7:8443/testroom" \
  --display-name bot1 \
  --display :99 \
  --display-backend xvfb \
  --executable-path /home/soyoung/service/jitsi-electron/run-jitsi.sh \
  --accessibility-dump-path "/tmp/jitsi-{stage}-tree.json" \
  --leave-after-sec 10
```

Dump the current accessibility tree without joining:

```bash
PYTHONPATH=vtc_traffic_generator python3 -m vtc_automation.test_adapter \
  --service jitsi_electron \
  --vtc-url "https://192.168.0.7:8443/testroom" \
  --display-name bot1 \
  --display :99 \
  --executable-path /home/soyoung/service/jitsi-electron/run-jitsi.sh \
  --accessibility-dump-path /tmp/jitsi-manual-tree.json \
  --dump-accessibility-tree \
  --dump-only
```

Test screen share with a pre-opened target window:

```bash
PYTHONPATH=vtc_traffic_generator python3 -m vtc_automation.test_adapter \
  --service jitsi_electron \
  --vtc-url "https://192.168.0.7:8443/testroom" \
  --display-name bot1 \
  --display :99 \
  --executable-path /home/soyoung/service/jitsi-electron/run-jitsi.sh \
  --screen-share-target "Target Window Title" \
  --leave-after-sec 10
```

If the app does not launch or join:

```bash
tail -n 100 /tmp/jitsi-electron.log
tail -n 100 /tmp/vtc-jitsi-adapter.log
tail -n 100 /tmp/vtc-events.jsonl
DISPLAY=:99 xdotool search --onlyvisible --name "Jitsi Meet|testroom|jitsi"
DISPLAY=:99 xdotool getwindowgeometry <window-id>
python3 -m accerciser
```

To add the next native service adapter:

1. Add `vtc_automation/adapters/<service>.py`.
2. Subclass `ServiceAdapter`.
3. Implement `launch`, `connect`, `leave`, `mute`, `unmute`, `camera_on`,
   `camera_off`, `select_devices`, `is_in_meeting`, and `close`.
4. Register it in `vtc_automation/adapters/registry.py`.

## ICSI Speech Replay

ICSI mode replaces the controller's random speaker selection loop with a replay
of one real ICSI meeting's dialogue-act timeline. It only controls speech
timing. It does not provide microphone toggles, camera toggles, or screen-share
events.

Rules enforced by the replay policy:

- ICSI mode requires at least 3 bots.
- One ICSI meeting is used per run. Different ICSI meetings are never mixed.
- One bot maps to exactly one ICSI active speaker.
- One ICSI active speaker maps to exactly one bot.
- The default selection mode only accepts meetings whose active speaker count
  exactly equals the requested bot count.
- Larger meetings are not downsampled by default.

Controller config example:

```json
{
  "role": "controller",
  "vtc_platform": "jitsi_electron",
  "duration": 60,
  "videoconference": true,
  "behavior": {
    "mode": "icsi",
    "icsi": {
      "corpus_root": "/Users/soyoung/Desktop/ICSI/ICSI",
      "meeting_id": null,
      "min_speaker_duration_sec": 1.0,
      "min_utterance_duration_sec": 0.05,
      "max_duration_sec": null,
      "time_scale": 1.0
    }
  },
  "vtc_clients": [
    ["client1", 8001],
    ["client2", 8001],
    ["client3", 8001]
  ]
}
```

When `meeting_id` is `null`, the policy selects the first meeting whose active
speaker count exactly equals the number of configured clients. When
`meeting_id` is set, the policy validates that the meeting has exactly that
many active speakers and fails otherwise.

The controller schedules non-blocking `start_speech(duration_sec, metadata)`
RPCs at the ICSI dialogue-act start times. This preserves ICSI silence,
overlap, and speaker transitions. Existing `dialog_cycle()` remains available
for the older random-speech mode. By default, the replay runs until the selected
ICSI meeting ends; set `behavior.icsi.max_duration_sec` only for a deliberately
truncated smoke test.
