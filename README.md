# Video Teleconferencing (VTC)

**VTC** is defined as two-way traffic, consisting of separate
audio and video streams, between two or more endpoints. This document explains how to install and operate the Searchlight VTC traffic generator. For technical details explaining how the traffic generator works or how media content is generated, see the [wiki](/wiki/main.md). 

[[_TOC_]]

## Architecture
The VTC traffic generator uses virtual devices to automate the camera and microphone inputs to a VTC application. In the **near future**, VTC applications themselves (e.g. Jitsi Meet, Microsoft Teams, Zoom) will be automated for a complete end-to-end solution for traffic generation.

The VTC traffic generator requires a testbed of at least 2 machines (physical or virtual), preferably 3+. The controller operates on one machine and controls multiple clients on separate machines. Each client machine ought to have 2 network interfaces, one for a control plane (controller traffic and status feedback), and another for a data plane (VTC traffic). The control plane network interface on the clients must be accessible by the controller. Using Virtualbox this is achieved by creating a host network and adding a secondary "Host-only" network adapter to each VTC client machine to serve as the control plane adapter. This is in addition to the primary adapter that creates the data plane and connects to the VTC application. Until full client automation is in place, the VTC clients must each be manually driven to configure and join a VTC session via a VTC application.

The necessary files for operating the VTC traffic generator include:

1. Content library - Audio and video files that the VTC client feeds into the VTC session, generated in advance. Stored at [TBD]. See [Generating a Content Library](#generating-a-content-library) if you would like to create your own content library. This library must be accessible by each VTC client. It is strongly recommended to copy the library to a shared drive that is mounted within each client virtual machine. A sample video and a single dialog conversation are stored in the repository [here](vtc_traffic_generator/sample_media).  
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
|`vtc_platform` | name of VTC platform, used for selecting platform-specific automation scripts - TBD|
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
    
## Generating a Content Library
Audio and visual content is pregenerated and can be found at [TBD]. If you want to create new content, the process is roughly:
1. Find appropriate video files that are free to use for the intended purpose. The existing library was created with video content sourced from [Videezy](https://www.videezy.com/).
1. Generate textual dialog tracks. This is done using the GPT-2 transformer. You can follow the instructions in the Jupyter Notebook [gpt-2_collect_google_colab.ipynb](/text_dialog_generation/gpt-2_collect_google_colab.ipynb). Notes: Tensorflow 1.x is required. Current versions are incompatible with `GPT-2`. A CUDA compatible GPU will dramatically speed up text generation. The notebook demonstrates how to use a free cloud GPU via the [Google Colab service](https://colab.research.google.com/). Warning, the text output might be inappropriate for it's intended purpose. Hand moderating this content for style and content is strongly encouraged. This file must be formatted for later consumption, as shown in [example_dialog](text_to_speech/2021_04_15_gpt2_output_topk-40_1558_moderated_45-convos.txt).    
1. Generate audio files from dialog tracks. This is done using the Google Cloud Platform text to speech service. You can follow the instructions in the Jupyter Notebook [GCP_TTS.ipynb](/text_to_speech/GCP_TTS.ipynb). This requires a Google Cloud account, though the free tier includes up to 1 million characters of voice synthesis per month.  

**Attribution**: video clips provided by [Videezy.com](https://www.videezy.com/) under the [Videezy standard license](https://support.videezy.com/hc/en-us/articles/115002135672-Videezy-Standard-License-Usage).

