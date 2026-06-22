import ffmpeg
import subprocess
import pulsectl
import os
import random
import signal
import sys
import time
import json
import socketserver
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer
from pathlib import PurePath
import asyncio
import concurrent.futures
import threading

from vtc_behavior import ICSIReplayPolicy
from vtc_automation.adapters import get_adapter
from vtc_automation.event_log import emit_event, get_service_name
from vtc_automation.packet_capture import PacketCaptureSession


speech_lock = threading.Lock()
speech_until = 0
speech_thread_active = False

def run_controller():
    num_clients = len(config['vtc_clients'])
    vtc_clients = []
    icsi_policy = None

    if is_icsi_mode():
        icsi_policy = ICSIReplayPolicy.from_config(config, num_clients)
        connect_duration_minutes = max(
            get_duration_minutes(config),
            (icsi_policy.end_sec / 60) + 1,
        )
        config["_connect_duration_minutes"] = connect_duration_minutes
        print("ICSI replay mode enabled.")
        print("ICSI policy: " + json.dumps(icsi_policy.summary(), sort_keys=True))

    # Instantiate clients
    for x in range(num_clients):

        # Create VTC client
        vtc_clients.append(VtcClient(config['vtc_clients'][x][0], config['vtc_clients'][x][1]))

    # Threaded VTC client initialization
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(vtc_clients))
    executor.map(VtcClient.initialize_client, vtc_clients)
    executor.shutdown(wait=False)

    if icsi_policy:
        run_icsi_replay_controller(vtc_clients, icsi_policy)
        return

    # Begin client dialog
    # Randomly select VTC_client as long as it wasn't the last one picked.
    chosen_client = None
    candidate_client = random.choice(vtc_clients)

    # VTC conversation loop
    start_time = time.time()
    elapsed_time = 0

    # Converse for VTC duration
    while elapsed_time < config['duration'] * 60:
        while candidate_client is chosen_client:
            candidate_client = random.choice(vtc_clients)

        chosen_client = candidate_client
        uri = 'http://' + chosen_client.ip + ':' + str(chosen_client.port)

        # Print bot name on controller STDOUT for debugging / manual bot admittance
        with xmlrpc.client.ServerProxy(uri) as proxy:
            print(proxy.get_name() + " speaking now.")

        # Command selected VTC client to take a dialog cycle
        with xmlrpc.client.ServerProxy(uri) as proxy:
            dialog_complete = False
            dialog_complete = proxy.dialog_cycle()

        elapsed_time = time.time() - start_time

    # Tear down VTC
    print("VTC complete, closing session now.")
    for x in range(num_clients):
        uri = 'http://' + vtc_clients[x].ip + ':' + str(vtc_clients[x].port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(vtc_clients[x].video_pid)


def is_icsi_mode():
    behavior = config.get("behavior", {})
    if isinstance(behavior, dict) and behavior.get("mode") == "icsi":
        return True
    return config.get("behavior_mode") == "icsi"


def get_duration_minutes(controller_config):
    try:
        return float(controller_config.get("duration", 0))
    except (TypeError, ValueError):
        return 0


def run_icsi_replay_controller(vtc_clients, icsi_policy):
    behavior = config.get("behavior", {})
    icsi_config = behavior.get("icsi", {}) if isinstance(behavior, dict) else {}
    time_scale = float(icsi_config.get("time_scale", 1.0))
    configured_max_duration_sec = icsi_config.get("max_duration_sec")
    max_duration_sec = (
        float(configured_max_duration_sec)
        if configured_max_duration_sec is not None
        else icsi_policy.end_sec
    )

    emit_event(
        config,
        "icsi_replay_start",
        {
            **icsi_policy.summary(),
            "time_scale": time_scale,
            "max_duration_sec": max_duration_sec,
        },
    )

    start_time = time.time()
    for event in icsi_policy.events:
        if event.start_sec > max_duration_sec:
            break

        target_time = start_time + (event.start_sec * time_scale)
        delay = target_time - time.time()
        if delay > 0:
            time.sleep(delay)

        duration_sec = max(0.05, min(event.end_sec, max_duration_sec) - event.start_sec)
        duration_sec *= time_scale
        metadata = {
            "meeting_id": event.meeting_id,
            "speaker_id": event.speaker_id,
            "channel": event.channel,
            "dialogue_act_type": event.dialogue_act_type,
            "icsi_start_sec": event.start_sec,
            "icsi_end_sec": event.end_sec,
            "duration_sec": duration_sec,
        }
        if event.audio_file_path:
            metadata["audio_file_path"] = event.audio_file_path
            metadata["audio_start_sec"] = event.start_sec

        emit_event(
            config,
            "policy_action_selected",
            {
                "bot_index": event.bot_index,
                **metadata,
            },
        )

        client = vtc_clients[event.bot_index]
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
            proxy.start_speech(duration_sec, metadata)

    end_delay = (min(max_duration_sec, icsi_policy.end_sec) * time_scale) - (time.time() - start_time)
    if end_delay > 0:
        time.sleep(end_delay)

    emit_event(
        config,
        "icsi_replay_done",
        {"meeting_id": icsi_policy.meeting_id},
    )

    print("ICSI replay complete, closing session now.")
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(client.video_pid)

class AsyncXMLRPCServer(socketserver.ThreadingMixIn,SimpleXMLRPCServer): pass

class VtcClient:
    def __init__(self, ip='0.0.0.0', port=100):
        self.ip = ip
        self.port = int(port)
        self.video_pid = 0

    def initialize_client(self):
        uri = 'http://' + self.ip + ':' + str(self.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.initialize_vtc_client()

        # Start client video stream to virtual camera device
        if config['videoconference']:
            with xmlrpc.client.ServerProxy(uri) as proxy:
                self.video_pid = proxy.play_video()

        # Connect to VTC session
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.run_connect(config.get("_connect_duration_minutes", config['duration']))


def run_client(client_config):
    # Register functions and respond to calls indefinitely
    #server = SimpleXMLRPCServer(("0.0.0.0", client_config['c2_port']), allow_none=True)
    server = AsyncXMLRPCServer(("0.0.0.0", client_config['c2_port']), allow_none=True)
    print("Listening on port: " + str(client_config['c2_port']))

    server.register_function(initialize_vtc_client, "initialize_vtc_client")
    server.register_function(play_video, "play_video")
    server.register_function(stop_video, "stop_video")
    server.register_function(dialog_cycle, "dialog_cycle")
    server.register_function(start_speech, "start_speech")
    server.register_function(get_name, "get_name")
    server.register_function(run_connect, "run_connect")
    server.register_function(stop_video, "stop_video")

    server.serve_forever()


# XMLRPC
# Initialize client
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print("Initializing VTC client")

    # Set up audio devices
    pulse = pulsectl.Pulse()
    sinks = pulse.sink_list()
    sources = pulse.source_list()

    if "name='virtual_speaker'" not in str(sinks) or "name='virtual_mic'" not in str(sources):
        print("Creating PulseAudio virtual devices")
        subprocess.run(
            'pactl load-module module-null-sink sink_name="virtual_speaker" '
            'sink_properties=device.description="virtual_speaker"',
            capture_output=True, shell=True)
        subprocess.run(
            'pactl load-module module-remap-source master="virtual_speaker.monitor" source_name="virtual_mic" '
            'source_properties=device.description="virtual_mic"',
            capture_output=True, shell=True)

    # Set volume levels and unmute devices
    for sink in sinks:
        pulse.volume_set_all_chans(sink, .8)
        pulse.mute(sink, False)

    for source in sources:
        pulse.volume_set_all_chans(source, .8)
        pulse.mute(source, False)

    # Check for v4l2 virtual webcam kernel module
    if 'v4l2loopback' not in str(subprocess.run(['lsmod'], capture_output=True)):
        print(
            "Error: v4l2loopback kernel module not loaded. Try: sudo modprobe v4l2loopback video_nr=5 exclusive_caps=1")

    print(config['bot_name'] + " configured.")


# XMLRPC
def dialog_cycle():
    print(config['bot_name'] + " speaking now.")
    emit_event(config, "speech_start", {"bot_name": config.get("bot_name")})

    try:
        # Calculate the number of sentences a VTC client speaks in a single turn
        # Left skewed, lower bound at 1, upper bound at number of sentences in a given conversation
        num_sentences = round(abs(random.gauss(0, 2))) + 1

        # Select conversation
        convo_root = str(PurePath(config['audio_path'], config['voice_name']))
        convo_list = os.listdir(convo_root)
        convo_index = random.randrange(0, len(convo_list))
        convo_path = str(PurePath(convo_root, convo_list[convo_index]))
        print("CONVOPATH:" + convo_path)

        # Select dialog lines and play them
        audio_filenames = os.listdir(convo_path)
        if num_sentences > len(audio_filenames):
            num_sentences = len(audio_filenames)

        print("Number of lines:" + str(num_sentences))

        index = 0
        while index < num_sentences:
            # Play file
            filename = str(index) + ".flac"
            audiofile_fullpath = str(PurePath(convo_path, filename))

            # Play audio
            play_audio(audiofile_fullpath)
            index += 1

        return True
    finally:
        emit_event(config, "speech_end", {"bot_name": config.get("bot_name")})


def start_speech(duration_sec, metadata=None):
    global speech_thread_active
    global speech_until

    metadata = dict(metadata or {})
    duration_sec = float(duration_sec)
    now = time.time()

    if metadata.get("audio_file_path"):
        emit_event(
            config,
            "speech_start",
            {
                "duration_sec": duration_sec,
                **metadata,
            },
        )
        thread = threading.Thread(
            target=speech_audio_segment_worker,
            args=(duration_sec, metadata,),
            daemon=True,
        )
        thread.start()
        return True

    with speech_lock:
        speech_until = max(speech_until, now + duration_sec)
        should_start_thread = not speech_thread_active
        if should_start_thread:
            speech_thread_active = True

    emit_event(
        config,
        "speech_start",
        {
            "duration_sec": duration_sec,
            **metadata,
        },
    )

    if should_start_thread:
        thread = threading.Thread(
            target=speech_playback_worker,
            args=(duration_sec, metadata,),
            daemon=True,
        )
        thread.start()

    return True


def speech_playback_worker(duration_sec, metadata):
    global speech_thread_active
    global speech_until

    try:
        while True:
            with speech_lock:
                remaining_sec = speech_until - time.time()

            if remaining_sec <= 0:
                break

            audio_file_path = choose_audio_file()
            if audio_file_path:
                play_audio(audio_file_path)
            else:
                time.sleep(min(remaining_sec, 0.25))
    finally:
        with speech_lock:
            speech_thread_active = False
            speech_until = 0

        emit_event(
            config,
            "speech_end",
            dict(metadata or {}),
        )


def speech_audio_segment_worker(duration_sec, metadata):
    try:
        audio_file_path = metadata.get("audio_file_path")
        audio_start_sec = float(metadata.get("audio_start_sec", 0))
        if audio_file_path:
            play_audio_segment(audio_file_path, audio_start_sec, duration_sec)
    finally:
        emit_event(
            config,
            "speech_end",
            dict(metadata or {}),
        )


def choose_audio_file():
    convo_root = str(PurePath(config['audio_path'], config['voice_name']))
    convo_list = os.listdir(convo_root)
    if not convo_list:
        return None

    convo_path = str(PurePath(convo_root, random.choice(convo_list)))
    audio_filenames = [
        filename
        for filename in os.listdir(convo_path)
        if filename.endswith(".flac")
    ]
    if not audio_filenames:
        return None

    return str(PurePath(convo_path, random.choice(audio_filenames)))


# No XMLRPC needed, simply a local function on the remote VTC client
def play_audio(audio_file_path):
    subprocess.run(['paplay', '-d', 'virtual_speaker', audio_file_path], capture_output=True)


def play_audio_segment(audio_file_path, start_sec, duration_sec):
    ffmpeg_process = subprocess.Popen(
        [
            'ffmpeg',
            '-hide_banner',
            '-loglevel',
            'error',
            '-ss',
            str(max(0, start_sec)),
            '-t',
            str(max(0.05, duration_sec)),
            '-i',
            audio_file_path,
            '-f',
            'wav',
            'pipe:1',
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        subprocess.run(
            ['paplay', '-d', 'virtual_speaker'],
            stdin=ffmpeg_process.stdout,
            capture_output=True,
        )
    finally:
        if ffmpeg_process.stdout:
            ffmpeg_process.stdout.close()
        ffmpeg_process.wait()


# XMLRPC
def play_video():
    # Setup streaming from file to v4l2 device
    if config['videoconference']:
        try:
            video_filepath = str(PurePath(config['video_path'], config['video_name']))
            virtual_video_config = config.get("virtual_video", {})
            video_device = config.get("video_device") or virtual_video_config.get("device") or "/dev/video5"
            print(video_filepath)
            # time.sleep(5)

            if "270" in config['video_name']:
                process = (
                    ffmpeg
                        .input(video_filepath, re=None, stream_loop=-1)
                        .filter('format', 'yuv420p')
                        .drawtext(text=config['bot_name'], x='(w-text_w)/2', y='h-th-20', fontcolor='red', fontsize=50)
                        .output(video_device, format='v4l2')
                )
            else:
                process = (
                    ffmpeg
                        .input(video_filepath, re=None, stream_loop=-1)
                        .filter('format', 'yuv420p')
                        .drawtext(text=config['bot_name'], x='(w-text_w)/2', y='h-th-50', fontcolor='red', fontsize=200)
                        .output(video_device, format='v4l2')
                )

            # Launch video playback
            print("Launching video playback")
            # process = process.run_async(pipe_stdin=True, quiet=True)
            process = process.run_async(pipe_stdin=True)
            emit_event(
                config,
                "camera_on",
                {"video_path": video_filepath, "video_device": video_device},
            )

        except ffmpeg.Error as e:
            print('stdout:', e.stdout.decode('utf8'))
            print('stderr:', e.stderr.decode('utf8'))
            raise e

        return process.pid

    else:
        return False


# XMLRPC
async def connect_vtc_session(duration):
    service = get_service_name(config)
    packet_capture = PacketCaptureSession.from_config(config)
    try:
        emit_event(
            config,
            "connect_vtc_session_start",
            {"vtc_url": config.get("vtc_url")},
            service,
        )
        packet_capture.start()
        adapter = get_adapter(config)
        result = await adapter.connect(duration)
        emit_event(
            config,
            "connect_vtc_session_done",
            {"vtc_url": config.get("vtc_url")},
            service,
        )
        return result
    except Exception as exc:
        emit_event(
            config,
            "adapter_error",
            {"error": str(exc), "vtc_url": config.get("vtc_url")},
            service,
        )
        raise
    finally:
        packet_capture.stop_and_analyze()

def run_connect(duration):
    thread = threading.Thread(
        target=lambda: asyncio.run(connect_vtc_session(duration)),
        daemon=True,
    )
    thread.start()
    return True


# XMLRPC
def stop_video(video_pid):
    print("Stopping video")
    os.kill(video_pid, signal.SIGTERM)
    emit_event(config, "camera_off", {"video_pid": video_pid})


# XMLRPC
def get_name():
    return config['bot_name']


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Error: must specify configuration JSON file.")
        exit()

    # read config
    with open(sys.argv[1], 'r') as infile:
        try:
            config = json.load(infile)
        except json.decoder.JSONDecodeError as err:
            print(f"Invalid JSON: {err}")  # in case json is invalid

    print(config['version'])
    print("Role: " + config['role'])
    print("VTC Platform: " + get_service_name(config))

    if config['role'] == 'controller':
        print("Duration: " + str(config['duration']) + " minutes")
        run_controller()

    elif config['role'] == 'client':
        run_client(config)
