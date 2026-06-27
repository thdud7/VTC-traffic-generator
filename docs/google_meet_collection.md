# Google Meet collection

Work from `dev` after the Jitsi work is merged, then create or continue the `meet` branch. The current `meet` branch is based on the same commit as `dev` and `jitsi`.

Google Meet uses Chrome/Chromium web automation. Do not install or use a Meet app, and do not use the Jitsi Electron launcher. The Meet adapter uses Playwright and forbids `--use-fake-device-for-media-stream`; it relies on the real `VTC_Microphone` PulseAudio source and `VTC Bot Camera` v4l2loopback device. `--use-fake-ui-for-media-stream` is allowed to auto-accept browser media permission prompts.

## Current EC2 hosts

Local SSH/Ansible should use public DNS or public IP. Controller-to-bot XML-RPC should use private IP through `rpc_host`/`c2_host`.

| role | public DNS | public IP | private IP |
| --- | --- | --- | --- |
| controller | `ec2-13-125-247-23.ap-northeast-2.compute.amazonaws.com` | `13.125.247.23` | `172.31.37.160` |
| bot1 | `ec2-15-164-217-62.ap-northeast-2.compute.amazonaws.com` | `15.164.217.62` | `172.31.40.44` |
| bot2 | `ec2-3-34-97-190.ap-northeast-2.compute.amazonaws.com` | `3.34.97.190` | `172.31.36.50` |
| bot3 | `ec2-13-209-72-146.ap-northeast-2.compute.amazonaws.com` | `13.209.72.146` | `172.31.36.99` |

No bot4-bot6 hosts are defined yet. Add them only when real EC2 public/private addresses exist.

## Required configuration

Set these before generating or running a Meet experiment:

```bash
export VTC_MEET_URL='https://meet.google.com/...'
export VTC_S3_BUCKET='your-bucket'
export VTC_S3_PREFIX='your-prefix'
export VTC_ANSIBLE_USER='ubuntu'
export VTC_SSH_PRIVATE_KEY="$HOME/.ssh/bootstrap.pem"
```

Optional:

```bash
export VTC_MEET_CHROME_USER_DATA_DIR='/home/ubuntu/.config/vtc-meet-profile'
```

Use a persistent Chrome profile when Meet requires a logged-in Google account. Do not commit accounts, passwords, tokens, cookies, or browser profiles.

## Media upload and manifest

Upload temporary test videos from explicit bot paths:

```bash
scripts/upload_video_media.py \
  --platform google_meet \
  --media-profile test \
  --manifest vtc_traffic_generator/media.google_meet.test.json \
  bot1=/path/to/bot1.mp4 \
  bot2=/path/to/bot2.mp4 \
  bot3=/path/to/bot3.mp4
```

The script writes a manifest keyed by `bot_id`, with S3 URI, local cache path, SHA-256, and size. Large video files, local media caches, browser profiles, logs, and pcaps are ignored by git.

The example manifest is [media.google_meet.test.example.json](/Users/soyoung/Documents/projects/video-teleconference/vtc_traffic_generator/media.google_meet.test.example.json). The current test manifest contains bot1-bot3 only. Later, add bot4-bot6 entries to the manifest without changing code.

At runtime each client downloads its own S3 video to `local_cache_path`, validates size/checksum when present, then uses the existing `play_video()` ffmpeg loop into v4l2.

## Generate and dry-run

Generate configs and inventory:

```bash
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.google_meet.3bot.5min.json \
  --output-dir generated/experiment
```

The generated controller config uses private IPs for XML-RPC:

```json
"vtc_clients": [["172.31.40.44", 8001], ["172.31.36.50", 8001], ["172.31.36.99", 8001]]
```

Deploy and run:

```bash
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.google_meet.3bot.5min.json \
  --deploy \
  --run-controller
```

The existing capture upload layout is reused:

```text
<capture_upload.s3_uri>/experiments/<experiment_name>/<execution_id>/
```

## Extending to bot4-bot6

Add only real data:

1. Add EC2 public DNS, public IP, private IP, and optional instance ID to the experiment clients or inventory.
2. Add the bot video to the media manifest.
3. Add the bot ID to `clients` in the experiment config.

Validation checks only selected bots. Missing bot4 media does not fail while bot4 is not selected. Selecting bot4 without inventory/media fails with a bot-specific error.

## EC2 recovery

Refresh host addresses and check SSH:

```bash
scripts/recover_ec2_inventory.py testbed/meet.local.inv \
  --user "$VTC_ANSIBLE_USER" \
  --ssh-key "$VTC_SSH_PRIVATE_KEY"
```

Stop/start is destructive and disabled by default. Use it only during preparation or health-check phases, never during an active experiment:

```bash
ALLOW_EC2_STOP_START=true scripts/recover_ec2_inventory.py testbed/meet.local.inv \
  --stop-start \
  --user "$VTC_ANSIBLE_USER" \
  --ssh-key "$VTC_SSH_PRIVATE_KEY"
```

The helper writes a generated inventory cache JSON and refreshes `rpc_host` from the latest private IP.

## Google Meet limitations

Meet may require account login or host approval. The adapter logs `meeting_join_failed`, `meeting_join_waiting_or_failed`, `browser_error`, screenshots, page HTML, and browser console logs under the existing diagnostics directory when automation cannot complete.

Google Meet has no Jitsi Videobridge stats. Packet capture, tshark analysis, logs, screenshots, diagnostics, and S3 upload still use the existing structure. Jitsi-specific filtered pcaps or JVB metrics should be disabled or marked unsupported for `google_meet`.
