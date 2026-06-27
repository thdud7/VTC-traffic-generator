# Video Teleconferencing Traffic Generator

## Acknowledgement

This research was developed with funding from the Defense Advanced Research
Projects Agency (DARPA). The views, opinions and/or findings expressed are
those of the author and should not be interpreted as representing the official
views or policies of the Department of Defense or the U.S. Government.

## English

### 1. Project Overview

This repository contains a traffic generator for video teleconferencing (VTC)
experiments. It automates multiple meeting participants, feeds controlled audio
and video into each participant, captures network traffic, records behavior
logs, and uploads the resulting experiment artifacts for later analysis.

The current workflow is designed around a controller-and-bot architecture:

- The **controller** reads one experiment JSON file, generates per-bot configs,
  creates an Ansible inventory, starts or updates remote EC2 instances, runs the
  VTC scenario, stops processes after the run, and uploads artifacts.
- Each **bot client** runs a VTC application, exposes an XML-RPC control port to
  the controller, plays audio through a virtual microphone, injects video into a
  virtual camera, performs scheduled or random actions, and captures packets.
- The **VTC service server**, for example the Jitsi server and JVB container,
  hosts the meeting under test.

For ICSI-based experiments, the controller maps ICSI dialogue acts to bot
speakers. The bots then replay the original utterance timing into the meeting so
that speech start/end, microphone state, camera state, screen share actions, and
captured packets can be correlated after the experiment.

The main entry points are:

```text
vtc_traffic_generator/run_experiment.py
  Generates controller/client configs, Ansible inventory, and optionally deploys,
  runs, cleans up, and uploads one experiment.

vtc_traffic_generator/vtc_generator.py
  Runs either the controller role or a client role, depending on the generated
  JSON config.

vtc_traffic_generator/vtc_automation/adapters/
  Contains service-specific automation adapters. Jitsi currently uses the
  jitsi_electron adapter.

ansible/
  Contains EC2 deployment, client startup, cleanup, ICSI sync, Jitsi server
  startup, and capture upload playbooks.

scripts/
  Contains analysis and maintenance helpers, including ICSI run analysis and
  S3 cleanup for postprocessed pcap files.
```

### 2. Tools Used

The project uses the following tools in the current EC2 automation workflow:

- **Python 3**: Core controller/client runtime, config generation, behavior
  scheduling, log rendering, and packet analysis helpers.
- **Ansible**: Remote deployment and operations across the controller, Jitsi
  server, and bot EC2 instances.
- **XML-RPC**: Control channel from the controller to each bot.
- **Jitsi Meet / Jitsi Electron**: Current VTC application target for automated
  Jitsi experiments.
- **Docker Compose**: Starts the self-hosted Jitsi server stack, including JVB,
  on the Jitsi EC2 instance.
- **PulseAudio**: Creates and controls virtual speaker/microphone devices.
- **FFmpeg**: Replays ICSI speech segments, injects camera media, generates
  audio probes, and performs media checks.
- **v4l2loopback**: Provides the Linux virtual camera device used by the bot.
- **Xvfb and openbox**: Provide a headless X11 desktop for each bot session.
- **x11vnc**: Optional viewer-only VNC access to inspect a bot display. The
  configured password is `123456`; VNC traffic is excluded from packet capture
  with `not port 5900`.
- **dumpcap and tshark**: Capture and analyze network packets.
- **AWS CLI and S3**: Sync ICSI input data and upload experiment artifacts.
- **ICSI Meeting Corpus**: Provides dialogue acts and audio signals for
  realistic multi-speaker speech replay.

### 3. Where Results Are Stored

Each experiment produces local temporary artifacts first and then uploads the
run to S3 when capture upload is enabled.

Generated local controller files:

```text
generated/experiment/
  controller_config.json
  remote_config_bot1.json
  remote_config_bot2.json
  remote_config_bot3.json
  inventory.ini
  run-manifest-<execution_id>.json
```

Controller runtime logs:

```text
/tmp/vtc-controller/actions-<execution_id>.txt
/tmp/vtc-controller/events-<execution_id>.jsonl
```

Bot runtime logs and diagnostics:

```text
/tmp/vtc-bot1/
/tmp/vtc-bot2/
/tmp/vtc-bot3/
  actions-<execution_id>.txt
  events-<execution_id>.jsonl
  adapter-<execution_id>.log
  jitsi-electron-<execution_id>.log
  diagnostics/<execution_id>/
```

Bot packet captures are first written locally:

```text
/home/ubuntu/vtc_data/captures/local/
```

When `capture_upload.s3_uri` is configured and `--no-upload-captures` is not
passed, `run_experiment.py --run-controller` automatically uploads the
experiment after cleanup. The default Jitsi configs upload to:

```text
s3://vtc-traffic-data/captures/experiments/<experiment_name>/<execution_id>/
```

The S3 layout is:

```text
experiments/<experiment_name>/<execution_id>/
  configs/
    <experiment>_experiment_config.json
    <experiment>_controller_config.json
    <experiment>_bot1_remote_config.json
    ...
  metadata/
    <experiment>_<execution_id>_metadata.json
    <experiment>_<execution_id>_<bot>_capture_metadata.json
    <experiment>_<execution_id>_<bot>_preflight.json
  dialogue_acts/
    bot1_<meeting_id>_<channel>_dialogue_acts.xml
    ...
  pcaps/raw/
    <experiment>_<execution_id>_<bot>_raw.pcapng
  logs/jsonl/
    <experiment>_<execution_id>_<bot>_actions.jsonl
    <experiment>_<execution_id>_<bot>_actions.txt
    <experiment>_<execution_id>_controller.jsonl
  logs/readable/
    <experiment>_<execution_id>_<bot>_successful_actions.log
    <experiment>_<execution_id>_controller_successful_actions.log
  analysis/
    <experiment>_<execution_id>_<bot>_media-analysis.json
    <experiment>_<execution_id>_<bot>_media-analysis.md
    <experiment>_<execution_id>_<bot>_tshark_analysis.txt
  screenshots/
  webrtc_stats/
  accessibility/
```

Only raw capture files are uploaded under `pcaps/raw/`. Postprocessed files such
as `*.jitsi-only.pcapng` and `*filtered*.pcapng` are intentionally excluded.
After a successful upload, local bot capture artifacts are deleted by the upload
playbook so EC2 storage is not consumed by previous runs.

ICSI input data is synced from S3 before deployment when `icsi_data` is present:

```text
s3://vtc-traffic-data/datasets/icsi/v1
s3://vtc-traffic-data/icsi
```

The expected local ICSI shape is:

```text
<icsi_data.local_root>/
  ICSI/DialogueActs/
    <meeting-id>.<speaker-or-channel>.dialogue-acts.xml
  ICSI/Signals/
    <meeting-id>/
      <speaker-id>.wav
```

## 한국어

### 1. 전체 프로젝트 설명

이 저장소는 화상회의(Video Teleconferencing, VTC) 실험용 트래픽 생성기입니다.
여러 개의 회의 참가자 봇을 자동으로 실행하고, 각 봇에 제어된 오디오와
비디오를 주입하며, 네트워크 패킷과 행동 로그를 수집한 뒤 S3에 업로드합니다.

현재 구조는 controller와 bot client 중심입니다.

- **controller**는 하나의 experiment JSON을 읽고, controller/bot 설정 파일과
  Ansible inventory를 생성합니다. 이후 EC2 배포, Jitsi 서버 시작, bot 시작,
  실험 실행, 프로세스 정리, 결과 업로드까지 담당합니다.
- **bot client**는 실제 VTC 앱을 실행하고, controller가 XML-RPC로 제어할 수
  있는 포트를 엽니다. bot은 가상 마이크로 오디오를 재생하고, 가상 카메라로
  비디오를 주입하며, 마이크/카메라/화면공유 행동을 수행하고 패킷을 캡처합니다.
- **VTC 서비스 서버**는 실험 대상 회의 서버입니다. Jitsi 실험에서는 Jitsi
  서버와 JVB 컨테이너가 이 역할을 합니다.

ICSI 기반 실험에서는 ICSI dialogue acts를 bot 발화자에 매핑합니다. 각 bot은
원본 회의의 발화 타이밍에 맞춰 음성을 재생하고, controller는 이 발화 구간과
마이크 상태, 카메라 상태, 화면공유 이벤트, 패킷 흐름을 함께 분석할 수 있도록
로그를 남깁니다.

주요 진입점은 다음과 같습니다.

```text
vtc_traffic_generator/run_experiment.py
  실험 JSON으로부터 설정 파일과 inventory를 만들고, 배포/실행/정리/업로드를
  선택적으로 수행합니다.

vtc_traffic_generator/vtc_generator.py
  생성된 JSON 설정에 따라 controller 역할 또는 client 역할로 실행됩니다.

vtc_traffic_generator/vtc_automation/adapters/
  서비스별 자동화 어댑터가 들어 있습니다. Jitsi는 jitsi_electron 어댑터를
  사용합니다.

ansible/
  EC2 배포, client 시작, client 정리, ICSI 데이터 동기화, Jitsi 서버 시작,
  캡처 업로드 playbook이 들어 있습니다.

scripts/
  ICSI run 분석, readable action log 생성, S3에 남은 후처리 pcap 정리 같은
  보조 스크립트가 들어 있습니다.
```

### 2. 사용한 도구

현재 EC2 자동화 흐름에서 사용하는 도구는 다음과 같습니다.

- **Python 3**: controller/client 실행, 설정 생성, 행동 스케줄링, 로그 렌더링,
  패킷 분석 보조 도구에 사용합니다.
- **Ansible**: controller, Jitsi 서버, bot EC2 인스턴스에 대한 배포와 원격
  작업을 수행합니다.
- **XML-RPC**: controller가 각 bot을 제어하는 control channel입니다.
- **Jitsi Meet / Jitsi Electron**: 현재 Jitsi 실험의 VTC 앱 대상입니다.
- **Docker Compose**: Jitsi EC2에서 Jitsi 서버와 JVB 컨테이너를 실행합니다.
- **PulseAudio**: bot의 가상 스피커와 가상 마이크를 구성하고 제어합니다.
- **FFmpeg**: ICSI 음성 구간 재생, 카메라 영상 주입, 오디오 probe 생성,
  미디어 검증에 사용합니다.
- **v4l2loopback**: Linux 가상 카메라 장치를 제공합니다.
- **Xvfb와 openbox**: headless 환경에서 bot별 X11 데스크톱을 제공합니다.
- **x11vnc**: bot 화면을 확인하기 위한 viewer-only VNC입니다. 비밀번호는
  `123456`이며, VNC 트래픽은 `not port 5900` 캡처 필터로 수집 대상에서
  제외됩니다.
- **dumpcap과 tshark**: 패킷 캡처와 패킷 분석에 사용합니다.
- **AWS CLI와 S3**: ICSI 입력 데이터 동기화와 실험 결과 업로드에 사용합니다.
- **ICSI Meeting Corpus**: 실제 회의 dialogue acts와 음성 신호를 사용해
  현실적인 다중 발화자 트래픽을 재현합니다.

### 3. 결과 저장 위치와 저장 방식

실험은 먼저 controller와 bot 로컬에 임시 산출물을 만들고, 업로드 설정이
켜져 있으면 실험 종료 후 S3에 한 번의 실험 단위로 묶어서 저장합니다.

controller에서 생성되는 설정 파일:

```text
generated/experiment/
  controller_config.json
  remote_config_bot1.json
  remote_config_bot2.json
  remote_config_bot3.json
  inventory.ini
  run-manifest-<execution_id>.json
```

controller 런타임 로그:

```text
/tmp/vtc-controller/actions-<execution_id>.txt
/tmp/vtc-controller/events-<execution_id>.jsonl
```

bot 런타임 로그와 진단 자료:

```text
/tmp/vtc-bot1/
/tmp/vtc-bot2/
/tmp/vtc-bot3/
  actions-<execution_id>.txt
  events-<execution_id>.jsonl
  adapter-<execution_id>.log
  jitsi-electron-<execution_id>.log
  diagnostics/<execution_id>/
```

bot 패킷 캡처는 먼저 각 bot 로컬에 저장됩니다.

```text
/home/ubuntu/vtc_data/captures/local/
```

`capture_upload.s3_uri`가 설정되어 있고 `--no-upload-captures`를 주지 않으면,
`run_experiment.py --run-controller` 실행 후 cleanup이 끝난 뒤 자동으로 S3에
업로드됩니다. 기본 Jitsi 설정의 업로드 위치는 다음과 같습니다.

```text
s3://vtc-traffic-data/captures/experiments/<experiment_name>/<execution_id>/
```

S3에는 다음 구조로 저장됩니다.

```text
experiments/<experiment_name>/<execution_id>/
  configs/
    <experiment>_experiment_config.json
    <experiment>_controller_config.json
    <experiment>_bot1_remote_config.json
    ...
  metadata/
    <experiment>_<execution_id>_metadata.json
    <experiment>_<execution_id>_<bot>_capture_metadata.json
    <experiment>_<execution_id>_<bot>_preflight.json
  dialogue_acts/
    bot1_<meeting_id>_<channel>_dialogue_acts.xml
    ...
  pcaps/raw/
    <experiment>_<execution_id>_<bot>_raw.pcapng
  logs/jsonl/
    <experiment>_<execution_id>_<bot>_actions.jsonl
    <experiment>_<execution_id>_<bot>_actions.txt
    <experiment>_<execution_id>_controller.jsonl
  logs/readable/
    <experiment>_<execution_id>_<bot>_successful_actions.log
    <experiment>_<execution_id>_controller_successful_actions.log
  analysis/
    <experiment>_<execution_id>_<bot>_media-analysis.json
    <experiment>_<execution_id>_<bot>_media-analysis.md
    <experiment>_<execution_id>_<bot>_tshark_analysis.txt
  screenshots/
  webrtc_stats/
  accessibility/
```

`pcaps/raw/`에는 원본 pcapng만 업로드합니다. `*.jitsi-only.pcapng`,
`*filtered*.pcapng` 같은 후처리 pcap은 중복 저장을 피하기 위해 S3 업로드에서
제외합니다. 업로드가 성공하면 bot 로컬에 남아 있던 수집 pcap과 관련 산출물은
upload playbook에서 삭제하므로 이전 실험 파일이 EC2 디스크를 계속 차지하지
않습니다.

ICSI 입력 데이터는 `icsi_data` 설정이 있을 때 배포 전에 S3에서 동기화됩니다.

```text
s3://vtc-traffic-data/datasets/icsi/v1
s3://vtc-traffic-data/icsi
```

로컬 ICSI 데이터는 다음 구조를 기대합니다.

```text
<icsi_data.local_root>/
  ICSI/DialogueActs/
    <meeting-id>.<speaker-or-channel>.dialogue-acts.xml
  ICSI/Signals/
    <meeting-id>/
      <speaker-id>.wav
```

## Service Workflows / 서비스별 실행 가이드

### Jitsi Meet / Jitsi Electron

#### English: How to run a Jitsi experiment from the controller

Run the following commands on the controller EC2 instance. Replace the public
DNS and key path with the current controller connection information.

```bash
ssh -i ~/.ssh/bootstrap.pem ubuntu@<controller-public-dns>
cd /home/ubuntu/project/VTC-traffic-generator
git fetch origin
git switch dev
git pull --ff-only origin dev
```

Before starting a run, verify the Jitsi experiment JSON for the current EC2
addresses:

- `vtc_url`: should point to the Jitsi room, usually
  `https://172.31.32.200:8443/testroom#config.prejoinConfig.enabled=false&config.startWithAudioMuted=false&config.startWithVideoMuted=false`.
- `jitsi_server.host`: should be the Jitsi server private IP when the controller
  reaches Jitsi over the VPC.
- `clients[].host` and `clients[].c2_host`: should be the current bot private IPs
  if the controller reaches bots over the VPC.
- `repo.version` and top-level `branch`: decide which branch remote bot EC2s
  should pull during deployment. The existing Jitsi experiment files currently
  use `jitsi`; change these to `dev` if the dev branch should be deployed to the
  bots.
- `capture_upload.s3_uri`: should be set if the run should be uploaded
  automatically.

Useful Jitsi experiment commands:

```bash
# 3-minute validation run focused on mic/media readiness.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.3min.mic_guard.json \
  --deploy --run-controller

# 5-minute validation run with camera/random-action coverage.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.camera_actions.json \
  --deploy --run-controller

# 10-minute ICSI Jitsi run with 3 bots.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.10min.json \
  --deploy --run-controller
```

What the command does:

1. Generates controller and bot configs under `generated/experiment/`.
2. Builds an Ansible inventory from the experiment JSON.
3. Syncs ICSI data to the controller and bots when configured.
4. Starts or verifies the Jitsi server when `jitsi_server` is configured.
5. Updates each bot repository, installs/configures runtime pieces, starts
   Xvfb, openbox, x11vnc, PulseAudio, virtual camera injection, and the VTC
   client process.
6. Runs the controller scenario.
7. Stops client GUI, media, capture, and VNC processes after the run unless
   `--no-cleanup` is passed.
8. Uploads captures, logs, configs, dialogue acts, metadata, screenshots, and
   analysis files to S3 unless `--no-upload-captures` is passed.
9. Deletes successfully uploaded local capture artifacts from each bot.

If deployment is already complete and you only need to rerun the controller
with the generated configs, omit `--deploy`. If a previous run did not upload,
run the upload step explicitly:

```bash
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.10min.json \
  --upload-captures
```

To stop remote clients manually:

```bash
ansible-playbook -i generated/experiment/inventory.ini ansible/stop_clients.yml
```

For more Jitsi-specific validation rules, packet acceptance criteria, and
maintenance commands, see
[`docs/jitsi_electron_collection.md`](docs/jitsi_electron_collection.md).

#### 한국어: controller에서 Jitsi 실험 실행 방법

아래 명령은 controller EC2 안에서 실행합니다. public DNS와 key 경로는 현재
controller 접속 정보에 맞게 바꿔야 합니다.

```bash
ssh -i ~/.ssh/bootstrap.pem ubuntu@<controller-public-dns>
cd /home/ubuntu/project/VTC-traffic-generator
git fetch origin
git switch dev
git pull --ff-only origin dev
```

실험을 시작하기 전에 Jitsi experiment JSON에서 현재 EC2 주소가 맞는지
확인합니다.

- `vtc_url`: Jitsi 회의 URL입니다. 보통 다음 형식을 사용합니다.
  `https://172.31.32.200:8443/testroom#config.prejoinConfig.enabled=false&config.startWithAudioMuted=false&config.startWithVideoMuted=false`
- `jitsi_server.host`: controller가 VPC 내부에서 Jitsi 서버에 접근한다면 Jitsi
  서버의 private IP를 넣습니다.
- `clients[].host`, `clients[].c2_host`: controller가 VPC 내부에서 bot에
  접근한다면 각 bot의 private IP를 넣습니다.
- `repo.version`, 최상위 `branch`: 배포 중 bot EC2가 어떤 Git 브랜치를 pull할지
  결정합니다. 현재 저장소의 Jitsi 실험 JSON은 `jitsi`를 사용합니다. dev
  브랜치를 bot에 배포하려면 이 값을 `dev`로 바꿔야 합니다.
- `capture_upload.s3_uri`: 실험 종료 후 S3 자동 업로드가 필요하면 설정되어
  있어야 합니다.

자주 사용하는 Jitsi 실험 명령어:

```bash
# 마이크와 미디어 준비 상태를 빠르게 확인하는 3분 검증 실험.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.3min.mic_guard.json \
  --deploy --run-controller

# 카메라 on/off와 random action 커버리지를 확인하는 5분 검증 실험.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.camera_actions.json \
  --deploy --run-controller

# bot 3개로 실행하는 10분 ICSI Jitsi 실험.
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.10min.json \
  --deploy --run-controller
```

이 명령은 다음 작업을 한 번에 수행합니다.

1. `generated/experiment/` 아래에 controller와 bot 설정을 생성합니다.
2. experiment JSON을 기반으로 Ansible inventory를 생성합니다.
3. 설정된 경우 ICSI 데이터를 controller와 bot에 동기화합니다.
4. `jitsi_server`가 설정되어 있으면 Jitsi 서버를 시작하거나 상태를 확인합니다.
5. 각 bot 저장소를 업데이트하고, 런타임 환경을 준비한 뒤 Xvfb, openbox,
   x11vnc, PulseAudio, 가상 카메라 주입, VTC client 프로세스를 시작합니다.
6. controller 시나리오를 실행합니다.
7. `--no-cleanup`을 주지 않았다면 실험 종료 후 client GUI, media, capture, VNC
   프로세스를 자동으로 정리합니다.
8. `--no-upload-captures`를 주지 않았다면 캡처, 로그, 설정, dialogue acts,
   metadata, screenshot, 분석 파일을 S3에 자동 업로드합니다.
9. 업로드가 성공한 bot 로컬 수집 파일을 삭제합니다.

이미 배포가 끝난 상태에서 controller만 다시 실행하려면 `--deploy`를 빼고
실행할 수 있습니다. 이전 실험이 업로드되지 않았고 업로드만 수행하려면 다음
명령을 사용합니다.

```bash
python3 vtc_traffic_generator/run_experiment.py \
  vtc_traffic_generator/experiment.icsi.jitsi.3bot.10min.json \
  --upload-captures
```

원격 client를 수동으로 정리하려면 다음 명령을 실행합니다.

```bash
ansible-playbook -i generated/experiment/inventory.ini ansible/stop_clients.yml
```

Jitsi 전용 검증 기준, 패킷 acceptance rule, S3 후처리 pcap 정리 명령은
[`docs/jitsi_electron_collection.md`](docs/jitsi_electron_collection.md)를
참고합니다.

### Add future service guides below this section

New service-specific README sections, such as Google Meet, Webex, Zoom, Teams,
or BigBlueButton, should be added below the Jitsi section using the same
bilingual structure:

```text
### <Service Name>
#### English: How to run ...
#### 한국어: ... 실행 방법
```

## Copyright

Copyright (C) 2020-2022 University of Southern California

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

## License

[`GPL-3.0-or-later`](./LICENSE)

**Attribution**: video clips provided by [Videezy.com](https://www.videezy.com/)
under the [Videezy standard license](https://support.videezy.com/hc/en-us/articles/115002135672-Videezy-Standard-License-Usage).
