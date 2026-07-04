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

## Webex Adapter

The `webex` adapter uses the same controller/client XML-RPC, packet capture,
event logging, media playback, and ICSI behavior paths as the other services.
Only the meeting UI automation lives in `vtc_automation/adapters/webex.py`.

The adapter targets Webex in Chrome/Chromium through Playwright:

1. Open `vtc_url`.
2. Prefer "Join from your browser" and guest join controls when present.
3. Fill `adapter_config.display_name` or `bot_name`.
4. Optionally fill `adapter_config.email` and
   `adapter_config.meeting_password`.
5. Set initial microphone and camera state.
6. Click Join and wait for an in-meeting control.
7. Support runtime microphone, camera, screen-share, and leave actions through
   the existing adapter action methods.

Selectors are grouped under `adapter_config.selectors` so Webex UI changes can
be handled without touching common framework code. If DOM automation is not
stable for a specific deployment, enable `adapter_config.fallback.enabled` and
provide xdotool keys or coordinates for only the failing controls.

Example experiment config:

```bash
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.webex.example.json \
  --output-dir generated/webex-smoke
```

Smoke-test the adapter without the controller:

```bash
PYTHONPATH=vtc_traffic_generator python3 -m vtc_automation.test_adapter \
  --service webex \
  --vtc-url "https://example.webex.com/meet/YOUR_ROOM_OR_MEETING_ID" \
  --display-name bot1 \
  --leave-after-sec 10
```

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
  "virtual_audio": {
    "sink_name": "bot1_sink",
    "source_name": "bot1_sink.monitor",
    "sink_description": "bot1_sink",
    "source_description": "bot1_sink.monitor"
  },
  "virtual_video": {
    "device": "/dev/video5"
  },
  "adapter_config": {
    "display": ":99",
    "display_backend": "xvfb",
    "executable_path": "/home/soyoung/service/jitsi-electron/run-jitsi.sh",
    "restart_existing": false,
    "ignore_certificate_errors": true,
    "camera_name": "VTC Bot Camera",
    "microphone_name": "bot1_sink.monitor",
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

When multiple bots run on the same Ubuntu host, each bot needs distinct
resources. Use separate `c2_port`, `bot_name`, `adapter_config.display`, log
paths, `virtual_audio.sink_name`, `virtual_audio.source_name`, and
`virtual_video.device` values. Keep `adapter_config.restart_existing` set to
`false`; otherwise one bot can terminate another bot's Jitsi Electron process.
The configured `microphone_name` must match the PulseAudio source name or
description created by `virtual_audio.source_name` /
`virtual_audio.source_description`. The configured `camera_name` must match the
v4l2loopback card label shown to Jitsi.

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

## Packet Capture

Packet capture runs in the shared client path around `adapter.connect()`, so it
works across service adapters. It uses `dumpcap` to write pcapng files and runs
`tshark` after the meeting connection finishes to generate a text analysis.

Example client config:

```json
{
  "packet_capture": {
    "enabled": true,
    "interface": "any",
    "output_dir": "/tmp/vtc-captures",
    "capture_filter": null,
    "display_filter": null,
    "dumpcap_path": "dumpcap",
    "tshark_path": "tshark"
  }
}
```

Outputs are written with a UTC timestamp, service, and bot id:

```text
/tmp/vtc-captures/<timestamp>-<service>-<bot>.pcapng
/tmp/vtc-captures/<timestamp>-<service>-<bot>.analysis.txt
/tmp/vtc-captures/<timestamp>-<service>-<bot>.metadata.json
```

The tshark analysis includes one-second I/O stats, IP endpoints, and IP
conversations. If `dumpcap` or `tshark` is unavailable, the client logs a
`packet_capture_error` or `packet_analysis_error` event and continues the VTC
session.

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
      "corpus_root": "media/icsi",
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

When controller `corpus_root` is omitted, it defaults to `media/icsi` under the
controller project root. The controller only needs dialogue-act XML:

```text
media/icsi/
  ICSI/DialogueActs/
    <meeting-id>.<speaker-or-channel>.dialogue-acts.xml
```

For compatibility with older local data, `corpus_root/DialogueActs` is also
accepted.

Each client resolves ICSI wav files locally, so controller and client absolute
paths do not need to match. By default, clients look under
`media/icsi/Signals` relative to their own project root, with lowercase
`signals`, singular `Signal`, and lowercase `signal` accepted as fallbacks.
Override the client root with `icsi_audio_root` if needed:

```json
{
  "icsi_audio_root": "media/icsi/Signals"
}
```

Client wav files should be organized by meeting:

```text
media/icsi/Signals/
  <meeting-id>/
    <speaker-id>.wav
    <channel>.wav
    <meeting-id>.<speaker-or-channel>.wav
```

If a matching wav file exists, ICSI speech replay plays the corresponding time
segment from that wav. If no wav is found, the client falls back to its
configured `audio_path` and `voice_name` audio clips.

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
