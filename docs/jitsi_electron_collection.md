# Jitsi Electron Collection

This document records the current EC2 Jitsi Electron collection workflow.

## Experiment Commands

Run from the controller repository checkout:

```bash
cd /home/ubuntu/project/VTC-traffic-generator
git switch jitsi
git pull origin jitsi
python3 vtc_traffic_generator/run_experiment.py vtc_traffic_generator/experiment.icsi.jitsi.3bot.3min.mic_guard.json --deploy --run-controller
python3 vtc_traffic_generator/run_experiment.py vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.camera_actions.json --deploy --run-controller
python3 vtc_traffic_generator/run_experiment.py vtc_traffic_generator/experiment.icsi.jitsi.3bot.10min.json --deploy --run-controller
```

The harness automatically uploads captures after `--run-controller` when
`capture_upload.s3_uri` is configured and `--no-upload-captures` is not passed.
The 10 minute command is the final collection run after the 3 minute and
5 minute validation runs pass.

## S3 Layout

Every run is grouped by stable experiment name and generated execution id:

```text
s3://vtc-traffic-data/captures/
  experiments/
    <experiment_name>/
      <execution_id>/
        controller/
        metadata/
        configs/
        dialogue_acts/
        pcaps/raw/<experiment_name>_<run_id>_<bot_id>_raw.pcapng
        logs/jsonl/
        logs/readable/
        analysis/
        screenshots/
        webrtc_stats/
        accessibility/
```

Only raw capture files are uploaded to `pcaps/raw/`. Postprocessed files such as
`*.jitsi-only.pcapng` and `*filtered*.pcapng` are excluded from S3 upload. Local
capture artifacts are deleted from the clients after a successful upload.

## Readable Logs

Readable logs are derived from action/event logs and include only successful
high-signal events:

- `meeting_join_ready`
- `scenario_start`
- `speech_playback_start`
- `speech_playback_done`
- `mic_on`
- `mic_off`
- `camera_on`
- `camera_off`
- `screen_share_start`
- `screen_share_end`
- `meeting_end`
- `terminal_disconnect`
- `capture_started`
- `capture_stopped`

The renderer normalizes `speech_playback_end` and `audio_playback_done` to
`speech_playback_done`, and normalizes `meeting_disconnected` to
`terminal_disconnect`. Logs are sorted by UTC timestamp.

## Packet Acceptance

The media analyzer must not pass a capture only because UDP/10000 exists.
Packet validation requires:

- PCAP lifecycle evidence from capture start through meeting disconnect and
  capture stop.
- JVB UDP media flow.
- STUN/ICE packets.
- DTLS packets.
- RTP/SRTP-like packets.
- RTCP packets.
- Bidirectional RTP/SRTP-like streams.
- Persistent media flow in both directions.
- No major RTP sequence gaps on long streams.

The analyzer writes SSRC classification with direction, SSRC, payload type,
packet count, byte count, first/last seen timestamps, duration, packets/sec,
mean bitrate, likely media kind, and sequence gap count.

Screen-share packets are analyzed for evidence, but screen-share packet effects
are not a gating criterion for accepting the run. Speech, microphone, camera,
and base WebRTC media checks remain gating.

## Cleanup Existing S3 Postprocessed PCAPs

Dry-run:

```bash
python3 scripts/cleanup_s3_postprocessed_pcaps.py s3://vtc-traffic-data/captures/experiments
```

Apply:

```bash
python3 scripts/cleanup_s3_postprocessed_pcaps.py s3://vtc-traffic-data/captures/experiments --apply
```
