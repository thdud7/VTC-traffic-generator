# Acknowledgement
This research was developed with funding from the Defense Advanced Research Projects Agency
(DARPA). The views, opinions and/or findings expressed are those of the
author and should not be interpreted as representing the official views or
policies of the Department of Defense or the U.S. Government.

# Video Teleconferencing (VTC)

**VTC** is defined as two-way traffic, consisting of separate
audio and video streams, between two or more endpoints. This document explains how to install and operate the Searchlight VTC traffic generator. For technical details explaining how the traffic generator works or how media content is generated, see the [wiki](/wiki/main.md).

[[_TOC_]]

## Source Code
```
Video Teleconference (VTC)
├── README.md: documentation about VTC (this file)
├── testbed: Merge testbed-specific experiment model files
├── text_dialog_generation: GPT-2 based text dialog generator
├── text_to_speech: Speech dialog generator using Google Cloud Platform text to speech
├── VTC_AV: Pregenerated audio and video media files
├── vtc_traffic_generator: VTC traffic generation from pregenerated media
│   ├── controller_config_template.json: VTC Controller configuration template
│   ├── remote_config_template.json: VTC Client configuration template
│   ├── run_client.sh: helper script for running clients in headless mode
│   ├── sample_media: Sample A/V media for debugging
│   ├── vtc_automation: Service adapters for VTC application automation
│   └── vtc_generator.py: Core VTC generator controller and client program
└── wiki: Wiki documentation
```

## Architecture
The VTC traffic generator uses virtual devices to automate the camera and microphone inputs to a VTC application. In the **near future**, VTC applications themselves (e.g. Jitsi Meet, Microsoft Teams, Zoom) will be automated for a complete end-to-end solution for traffic generation.

The VTC traffic generator requires a testbed of at least 2 machines (physical or virtual), preferably 3+. The controller operates on one machine and controls multiple clients on separate machines. Each client machine ought to have 2 network interfaces, one for a control plane (controller traffic and status feedback), and another for a data plane (VTC traffic). The control plane network interface on the clients must be accessible by the controller. Using Virtualbox this is achieved by creating a host network and adding a secondary "Host-only" network adapter to each VTC client machine to serve as the control plane adapter. This is in addition to the primary adapter that creates the data plane and connects to the VTC application. Until full client automation is in place, the VTC clients must each be manually driven to configure and join a VTC session via a VTC application.

The necessary files for operating the VTC traffic generator include:

1. Content library - Audio and video files that the VTC client feeds into the VTC session, generated in advance. Stored at `users.isi.deterlab.net:/proj/searchlight/traffic-generators/vtc/VTC_media/`. See [Generating a Content Library](#generating-a-content-library) if you would like to create your own content library. This library must be accessible by each VTC client. It is strongly recommended to copy the library to a shared drive that is mounted within each client virtual machine. A sample video and a single dialog conversation are stored in the repository [here](vtc_traffic_generator/sample_media).  
1. `vtc_generator.py` python program - This file contains both the VTC controller component and the VTC client. Confusingly, the VTC client actually operates as a server that listens for commands from the controller. This file must be on the controller machine and all VTC client machines.
1. `controller_config_template.json` - This file is a template to construct the config file for the controller. This must be on the controller machine.
1. `remote_config_template.json` - This file is a template to construct the config file for each VTC client. These configuration files must be on each VTC client machine.
1. Various `.ipynb` files - Not necessary to operate the VTC traffic generator, they contain example code demonstrating how to construct a [Content Library](#generating-a-content-library).

## Configuring the VTC controller

The controller needs:
1. python3 environment (native or virtual) with only the standard packages.
1. `vtc_generator.py` containing the controller code.
1. A JSON controller configuration file based on `controller_config_template.json`. Copy this file and change the configuration parameters as necessary. Save it as a separate file, e.g. `controller_config.json`.  

| configuration parameter| description|
|---      |---|
|`role`   |must be set to `controller`|
|`vtc_platform` | name of VTC platform, used for selecting platform-specific automation scripts|
|`duration` | Duration of the entire VTC session, in minutes|
|`videoconference` | boolean (case sensitive) value, `true` for a video plus audio conference, `false` for audio-only|
|`vtc_clients`| ip address and port number for each VTC client. Represented as a list of lists, with ip address as a string and port number as an integer|
|`version` | controller software version number printed at runtime, useful for keeping controller and client configurations in sync |

## Configuring the VTC client(s)

Each client needs a nearly identical configuration. The only difference is that the controller must be able to reach each client at a different IP address. Therefore, when configuring multiple clients, it is often easiest to configure one, then clone that first machine as many times as necessary, specifying the control plane IP address for each.

Each client needs:
1. Linux operating system with the following packages installed:
    * ffmpeg
    * pulseaudio (installed by default on Ubuntu)
1. [`v4l2loopback`](https://github.com/umlaeute/v4l2loopback) kernel module installed and running.
    * Build and install kernel module as described [here](https://github.com/umlaeute/v4l2loopback).
    * Load kernel module with `# modprobe v4l2loopback video_nr=5 exclusive_caps=1`
    * Confirm `v4l2loopback` module is loaded with `$ lsmod|grep v4l2`
1. python3 environment (native or virtual) with the following packages installed:
    * [ffmpeg-python](https://pypi.org/project/ffmpeg-python/)
    * [pulsectl](https://pypi.org/project/pulsectl/)
1. Media library containing properly formatted audio and video files, mounted to the local filesystem.
1. `vtc_generator.py` containing the VTC client (server) code.
1. A JSON controller configuration file based on `remote_config_template.json`. Copy this file and change the configuration parameters as necessary. Save it as separate files for each of *n* VTC clients, e.g. `remote_config_n.json`.  

| configuration parameter| description|
|---      |---|
|`role`   |must be set to `client`|
|`vtc_platform` | name of VTC platform, used for selecting platform-specific automation scripts - TBD|
|`c2_port` | port to run XMLRPC server. Must match the port specified in the controller configuration. Default = 8001|
|`bot_name` | string for bot name superimposed over video feed into VTC session |
|`videoconference` | boolean (case sensitive) value, `true` to enable video for this client, `false` for audio-only|
|`audio_path` | full system path to the root of the audio tracks |
|`voice_name` | name of voice to use for this VTC client dialog. Must match a directory in `audio_path`|
|`video_path` | full system path to the root of the video files |
|`video_name` | name of video file to use for this VTC client. Must match a file in `video_path`|
|`version` | VTC client software version number printed at runtime, useful for keeping controller and client configurations in sync |

Service-specific automation is implemented through adapters under
[`vtc_traffic_generator/vtc_automation/adapters`](vtc_traffic_generator/vtc_automation/adapters).
Use `vtc_platform` to select the adapter. Current adapter keys include `jitsi`,
`webex`, `google_meet`, `bigbluebutton`, `teams`, `zoom`, `messenger`, and
`discord`.

## Executing a VTC session
1. Ensure each VTC client machine is [configured properly](#configuring-the-vtc-clients).
1. Start the VTC client software on each client *n* with: `python vtc_generator.py <remote_config_n.json>`
1. Start the VTC controller software on the controller with: `python vtc_generator.py <controller_config.json>`
    * This will start media playback into the virtual devices on each client, starting the conversation.
1. On each client, manually connect to the active VTC session.
    * You must select `virtual_mic` and `Dummy video device 0x0005` as the camera. It is strongly recommended to mute the speaker audio on each VTC client machine. This can be done with the virtual machine audio controls or by simply disabling audio feeding back to the host via the hypervisor UI.
    * Note, some VTC applications do not allow guest participation and require a login. Microsoft Teams allows guest participation only through the web client. Google Meet does not allow guest participation. Jitsi and Zoom allow guest participation through their web clients and their native applications.
1. Admit the VTC bots (if necessary) into the VTC conversation.
1. To shut down, kill the process (ctrl-C) on the controller and clients and close the VTC application.

## EC2 experiment automation

For EC2-based experiments, use the Python harness to generate controller/client
configs and the Ansible playbooks to update and start remote clients.

1. Copy the example experiment file and edit it for your EC2 environment:

   ```bash
   cp vtc_traffic_generator/experiment.example.json experiment.json
   ```

1. Fill in:

   - `vtc_url` with the Jitsi room URL.
   - `jitsi_server` with the Jitsi server EC2 host and start command, if the
     harness should start or restart that server before clients run.
   - `icsi_data.s3_uri` with the S3 prefix that stores ICSI data, if running
     ICSI replay collection.
   - `icsi_data.local_root` with the local path where each EC2 should sync
     ICSI data.
   - `repo.url` with the Git repository URL the EC2 clients can pull.
   - `repo.dir` with the repository path on each client EC2.
   - `ansible.user` and `ansible.ssh_private_key_file`.
   - `capture_upload.s3_uri` with the S3 prefix where client capture artifacts
     should be uploaded after the run, if packet capture upload is needed.
   - `capture_upload.run_id` with a stable experiment identifier for grouping
     one run's capture artifacts.
   - `clients[].host` with each client EC2 private or public IP.
   - media paths and names under `defaults` or per client.

1. Generate configs and inventory locally:

   ```bash
   python3 vtc_traffic_generator/run_experiment.py experiment.json
   ```

   This writes files under `generated/experiment/`, including:

   - `controller_config.json`
   - `remote_config_bot1.json`, `remote_config_bot2.json`, etc.
   - `inventory.ini`

1. Deploy the latest Git code, copy each bot config, and start the clients:

   ```bash
   python3 vtc_traffic_generator/run_experiment.py experiment.json --deploy
   ```

   By default, `--deploy` runs `ansible/deploy_experiment.yml`, which starts
   S3 ICSI data sync first, then the optional `jitsi_server`, and finally client
   deployment. The Jitsi server playbook assumes a preconfigured server
   directory and runs
   `jitsi_server.start_command`, defaulting to `docker compose up -d`.

   For ICSI replay collection, the S3 prefix should sync to this local shape on
   the controller and every client:

   ```text
   <icsi_data.local_root>/
     ICSI/DialogueActs/
       <meeting-id>.<speaker-or-channel>.dialogue-acts.xml
     Signals/
       <meeting-id>/
         <speaker-id>.wav
   ```

   The EC2 instances need AWS credentials or an instance profile that can read
   `icsi_data.s3_uri`.

   Client deployment currently:

   - Ensures the repository parent, generated config, and log directories exist.
   - Clones or updates the client repository.
   - Loads `v4l2loopback` for the configured video device.
   - Starts PulseAudio.
   - Starts Xvfb and openbox for the configured display.
   - Optionally creates and opens a browser window for screen sharing when
     `screen_share_window.enabled` is true.
   - Verifies the Jitsi Electron launcher exists.
   - Copies the generated bot config.
   - Restarts the VTC client process and waits for its control port.

   To enable screen-share scenarios, configure a stable share target window:

   ```json
   {
     "defaults": {
       "screen_share_window": {
         "enabled": true,
         "title": "VTC Share Window",
         "html_path": "/home/ubuntu/vtc_data/screen_share/share.html"
       },
       "adapter_config": {
         "screen_share_target": "VTC Share Window"
       }
     }
   }
   ```

   When `screen_share_window.enabled` is true, the generated client config sets
   `adapter_config.screen_share_target` to the same title if it was not already
   configured. Deployment writes the HTML page and opens it on the configured
   Xvfb display before launching Jitsi Electron.

1. Run the controller locally after deployment:

   ```bash
   python3 vtc_traffic_generator/run_experiment.py experiment.json --deploy --run-controller
   ```

1. Upload packet captures from each client to S3 after the controller exits:

   ```bash
   python3 vtc_traffic_generator/run_experiment.py experiment.json --upload-captures
   ```

   You can also run deployment, controller execution, and post-run upload in one
   command:

   ```bash
   python3 vtc_traffic_generator/run_experiment.py experiment.json --deploy --run-controller --upload-captures
   ```

   `--upload-captures` runs `ansible/upload_captures.yml`. It expects
   `capture_upload.s3_uri` and `capture_upload.run_id` in the experiment JSON.
   S3 prefixes do not need to be created ahead of time; S3 has object keys, not
   real directories, so `aws s3 sync` creates the needed prefixes when it uploads
   objects. The bucket itself must already exist, and each client EC2 needs AWS
   credentials or an instance profile that can write to that bucket.

   Recommended capture upload configuration:

   ```json
   {
     "run_id": "hcrc-2p-jitsi-20260624-001",
     "capture_upload": {
       "s3_uri": "s3://vtc-traffic-data/captures",
       "run_id": "hcrc-2p-jitsi-20260624-001",
       "include_logs": true
     },
     "defaults": {
       "packet_capture": {
         "enabled": true,
         "interface": "any",
         "output_dir": "/home/ubuntu/vtc_data/captures/local",
         "capture_filter": null,
         "display_filter": null,
         "dumpcap_path": "dumpcap",
         "tshark_path": "tshark"
       }
     }
   }
   ```

   The upload playbook writes this S3 layout:

   ```text
   s3://vtc-traffic-data/captures/
     raw/<run_id>/<bot>/*.pcapng
     analysis/<run_id>/<bot>/*.analysis.txt
     analysis/<run_id>/<bot>/*.metadata.json
     logs/<run_id>/<bot>/
   ```

1. Stop remote clients:

   ```bash
   ansible-playbook -i generated/experiment/inventory.ini ansible/stop_clients.yml
   ```

The harness owns experiment logic such as bot names, ports, display values,
media selection, and generated configs. Ansible owns remote EC2 work such as
`git pull`, file copy, process start, and process stop.

## Generating a Content Library
Audio and visual content is pregenerated and can be found at [TBD]. If you want to create new content, the process is roughly:
1. Find appropriate video files that are free to use for the intended purpose. The existing library was created with video content sourced from [Videezy](https://www.videezy.com/).
1. Generate textual dialog tracks. This is done using the GPT-2 transformer. You can follow the instructions in the Jupyter Notebook [gpt-2_collect_google_colab.ipynb](/text_dialog_generation/gpt-2_collect_google_colab.ipynb). Notes: Tensorflow 1.x is required. Current versions are incompatible with `GPT-2`. A CUDA compatible GPU will dramatically speed up text generation. The notebook demonstrates how to use a free cloud GPU via the [Google Colab service](https://colab.research.google.com/). Warning, the text output might be inappropriate for it's intended purpose. Hand moderating this content for style and content is strongly encouraged. This file must be formatted for later consumption, as shown in [example_dialog](text_to_speech/2021_04_15_gpt2_output_topk-40_1558_moderated_45-convos.txt).    
1. Generate audio files from dialog tracks. This is done using the Google Cloud Platform text to speech service. You can follow the instructions in the Jupyter Notebook [GCP_TTS.ipynb](/text_to_speech/GCP_TTS.ipynb). This requires a Google Cloud account, though the free tier includes up to 1 million characters of voice synthesis per month.  

## copyright

Copyright (C) 2020–2022  University of Southern California

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

## license

[`GPL-3.0-or-later`](./LICENSE)

**Attribution**: video clips provided by [Videezy.com](https://www.videezy.com/) under the [Videezy standard license](https://support.videezy.com/hc/en-us/articles/115002135672-Videezy-Standard-License-Usage).
