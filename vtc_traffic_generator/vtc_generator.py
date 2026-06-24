import subprocess
import os
import random
import signal
import sys
import time
import json
import socketserver
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer
from pathlib import Path, PurePath
import asyncio
import concurrent.futures
import threading
from datetime import datetime

from vtc_behavior import ICSIReplayPolicy
from vtc_automation.adapters import get_adapter
from vtc_automation.event_log import emit_event, get_service_name
from vtc_automation.packet_capture import PacketCaptureSession

try:
    import ffmpeg
except ImportError:
    ffmpeg = None

try:
    import pulsectl
except ImportError:
    pulsectl = None


speech_lock = threading.Lock()
speech_until = 0
speech_thread_active = False
action_log_lock = threading.Lock()
active_adapter_lock = threading.Lock()
active_adapter = None
active_loop = None
connection_status_lock = threading.Lock()
connection_status = {
    "state": "idle",
    "connected": False,
    "error": None,
    "vtc_url": None,
    "updated_at": None,
}


def append_action_log(log_config, event_name, details=None):
    details = dict(details or {})
    path = action_log_path(log_config)
    timestamp = datetime.now().isoformat(timespec="seconds")
    bot_name = log_config.get("bot_name") or details.get("bot_name") or log_config.get("role", "unknown")
    line = (
        f"{timestamp}\t"
        f"event={event_name}\t"
        f"bot={bot_name}\t"
        f"details={json.dumps(details, sort_keys=True, ensure_ascii=False)}"
        "\n"
    )
    with action_log_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as outfile:
            outfile.write(line)


def action_log_path(log_config):
    adapter_config = log_config.get("adapter_config", {})
    if not isinstance(adapter_config, dict):
        adapter_config = {}

    configured_path = log_config.get("action_log_path") or adapter_config.get("action_log_path")
    if configured_path:
        return Path(str(configured_path)).expanduser()

    if log_config.get("role") == "controller":
        return Path("/tmp/vtc-controller-actions.txt")

    bot_name = str(log_config.get("bot_name") or "bot")
    return Path(f"/tmp/vtc-{bot_name}-actions.txt")


def run_controller():
    num_clients = len(config['vtc_clients'])
    vtc_clients = []
    icsi_policy = None

    if is_icsi_mode():
        icsi_policy = ICSIReplayPolicy.from_config(config, num_clients)
        connect_duration_minutes = max(
            get_duration_minutes(config),
            (configured_icsi_replay_duration_sec(icsi_policy) / 60) + 1,
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
    list(executor.map(VtcClient.initialize_client, vtc_clients))
    executor.shutdown(wait=False)

    if icsi_policy:
        wait_for_clients_before_replay(vtc_clients)
        run_icsi_replay_controller(vtc_clients, icsi_policy)
        return

    run_random_dialog_controller(vtc_clients)


def run_random_dialog_controller(vtc_clients):
    chosen_client = None
    candidate_client = random.choice(vtc_clients)
    start_time = time.time()
    duration_sec = get_duration_minutes(config) * 60
    scenario_thread = None
    scenario_stop = threading.Event()

    append_action_log(
        config,
        "meeting_start",
        {"role": "controller", "client_count": len(vtc_clients), "duration_sec": duration_sec},
    )

    if is_random_scenario_enabled():
        scenario_thread = threading.Thread(
            target=random_scenario_worker,
            args=(vtc_clients, duration_sec, 1.0, scenario_stop),
            daemon=True,
        )
        scenario_thread.start()

    try:
        while time.time() - start_time < duration_sec:
            while candidate_client is chosen_client:
                candidate_client = random.choice(vtc_clients)

            chosen_client = candidate_client
            bot_index = vtc_clients.index(chosen_client)
            uri = 'http://' + chosen_client.ip + ':' + str(chosen_client.port)

            with xmlrpc.client.ServerProxy(uri) as proxy:
                print(proxy.get_name() + " speaking now.")

            ensure_client_microphone(chosen_client, bot_index, True)
            with xmlrpc.client.ServerProxy(uri) as proxy:
                proxy.dialog_cycle()
            maybe_update_microphone_after_speech(chosen_client, bot_index)
    finally:
        scenario_stop.set()
        if scenario_thread:
            scenario_thread.join(timeout=10)

        append_action_log(
            config,
            "meeting_end",
            {"role": "controller", "client_count": len(vtc_clients)},
        )

    print("VTC complete, closing session now.")
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(client.video_pid)


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


def configured_icsi_replay_duration_sec(icsi_policy):
    behavior = config.get("behavior", {})
    icsi_config = behavior.get("icsi", {}) if isinstance(behavior, dict) else {}
    configured_max_duration_sec = icsi_config.get("max_duration_sec")
    if configured_max_duration_sec is None:
        return icsi_policy.end_sec
    return min(float(configured_max_duration_sec), icsi_policy.end_sec)


def wait_for_clients_before_replay(vtc_clients):
    behavior = config.get("behavior", {})
    icsi_config = behavior.get("icsi", {}) if isinstance(behavior, dict) else {}
    timeout_sec = float(icsi_config.get("connect_timeout_sec", 180))
    grace_sec = float(icsi_config.get("connect_grace_sec", 5))
    deadline = time.time() + timeout_sec
    last_statuses = {}

    print(f"Waiting for {len(vtc_clients)} clients to join before ICSI replay.")
    while time.time() < deadline:
        connected_count = 0
        statuses = {}
        for index, client in enumerate(vtc_clients):
            uri = 'http://' + client.ip + ':' + str(client.port)
            try:
                with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
                    status = proxy.get_connection_status()
            except Exception as exc:
                status = {"state": "rpc_error", "connected": False, "error": str(exc)}

            statuses[index] = status
            if status.get("connected"):
                connected_count += 1

        if statuses != last_statuses:
            print("Client join status: " + json.dumps(statuses, sort_keys=True))
            last_statuses = statuses

        if connected_count == len(vtc_clients):
            if grace_sec > 0:
                print(f"All clients joined. Waiting {grace_sec} more seconds before ICSI replay.")
                time.sleep(grace_sec)
            return

        time.sleep(1)

    raise TimeoutError(
        "Timed out waiting for all clients to join before ICSI replay: "
        + json.dumps(last_statuses, sort_keys=True)
    )


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
    append_action_log(
        config,
        "meeting_start",
        {
            "role": "controller",
            "meeting_id": icsi_policy.meeting_id,
            "client_count": len(vtc_clients),
        },
    )

    start_time = time.time()
    scenario_thread = None
    scenario_stop = threading.Event()
    scenario_runtime_sec = min(max_duration_sec, icsi_policy.end_sec) * time_scale
    if is_random_scenario_enabled():
        scenario_thread = threading.Thread(
            target=random_scenario_worker,
            args=(vtc_clients, scenario_runtime_sec, time_scale, scenario_stop),
            daemon=True,
        )
        scenario_thread.start()

    try:
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
                "file_channel": event.file_channel,
                "dialogue_act_type": event.dialogue_act_type,
                "icsi_start_sec": event.start_sec,
                "icsi_end_sec": event.end_sec,
                "duration_sec": duration_sec,
                "audio_start_sec": event.start_sec,
            }

            emit_event(
                config,
                "policy_action_selected",
                {
                    "bot_index": event.bot_index,
                    **metadata,
                },
            )

            client = vtc_clients[event.bot_index]
            ensure_client_microphone(client, event.bot_index, True)
            uri = 'http://' + client.ip + ':' + str(client.port)
            with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
                proxy.start_speech(duration_sec, metadata)

        end_delay = (min(max_duration_sec, icsi_policy.end_sec) * time_scale) - (time.time() - start_time)
        if end_delay > 0:
            time.sleep(end_delay)
    finally:
        scenario_stop.set()
        if scenario_thread:
            scenario_thread.join(timeout=10)

    emit_event(
        config,
        "icsi_replay_done",
        {"meeting_id": icsi_policy.meeting_id},
    )
    append_action_log(
        config,
        "meeting_end",
        {"role": "controller", "meeting_id": icsi_policy.meeting_id},
    )

    print("ICSI replay complete, closing session now.")
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(client.video_pid)


def scenario_config():
    behavior = config.get("behavior", {})
    if not isinstance(behavior, dict):
        return {}
    scenario = behavior.get("scenario", {})
    if isinstance(scenario, dict):
        return scenario
    return {}


def is_random_scenario_enabled():
    scenario = scenario_config()
    return bool(scenario.get("enabled", True))


def random_scenario_worker(vtc_clients, runtime_sec, time_scale, stop_event):
    scenario = scenario_config()
    rng = random.Random(scenario.get("seed"))
    min_interval = float(scenario.get("min_interval_sec", 15)) * time_scale
    max_interval = float(scenario.get("max_interval_sec", 45)) * time_scale
    min_interval = max(1.0, min_interval)
    max_interval = max(min_interval, max_interval)
    screen_share_probability = float(scenario.get("screen_share_probability", 0.2))
    camera_probability = float(scenario.get("camera_probability", 0.35))
    if "mic_probability" in scenario:
        mic_probability = max(0.0, float(scenario.get("mic_probability", 0)))
    else:
        mic_probability = max(0.0, 1.0 - screen_share_probability - camera_probability)
    screen_owner = None
    states = [
        {
            "mic": True,
            "camera": True,
        }
        for _ in vtc_clients
    ]
    deadline = time.time() + max(0, runtime_sec)
    append_action_log(
        config,
        "scenario_start",
        {"runtime_sec": runtime_sec, "client_count": len(vtc_clients)},
    )

    try:
        while time.time() < deadline and not stop_event.is_set():
            interval = rng.uniform(min_interval, max_interval)
            if stop_event.wait(min(interval, max(0, deadline - time.time()))):
                break

            roll = rng.random()
            if roll < screen_share_probability:
                if screen_owner is None:
                    bot_index = rng.randrange(0, len(vtc_clients))
                    success = call_client_action(vtc_clients[bot_index], bot_index, "set_screen_share", True)
                    if success:
                        screen_owner = bot_index
                else:
                    success = call_client_action(vtc_clients[screen_owner], screen_owner, "set_screen_share", False)
                    if success:
                        screen_owner = None
            elif roll < screen_share_probability + camera_probability:
                bot_index = rng.randrange(0, len(vtc_clients))
                desired_state = not states[bot_index]["camera"]
                success = call_client_action(vtc_clients[bot_index], bot_index, "set_camera", desired_state)
                if success:
                    states[bot_index]["camera"] = desired_state
            elif mic_probability > 0:
                bot_index = rng.randrange(0, len(vtc_clients))
                desired_state = not states[bot_index]["mic"]
                success = call_client_action(vtc_clients[bot_index], bot_index, "set_microphone", desired_state)
                if success:
                    states[bot_index]["mic"] = desired_state
    finally:
        if screen_owner is not None:
            call_client_action(vtc_clients[screen_owner], screen_owner, "set_screen_share", False)
        append_action_log(config, "scenario_end", {"runtime_sec": runtime_sec})


def ensure_client_microphone(client, bot_index, enabled):
    return call_client_action(client, bot_index, "set_microphone", enabled)


def maybe_update_microphone_after_speech(client, bot_index):
    speech = speech_behavior_config()
    keep_on_probability = float(speech.get("post_speech_mic_on_probability", 0.65))
    silence_min_sec = float(speech.get("post_speech_silence_min_sec", 2))
    silence_max_sec = float(speech.get("post_speech_silence_max_sec", 12))
    rng = random.Random()

    if rng.random() < keep_on_probability:
        silence_max_sec = max(silence_min_sec, silence_max_sec)
        time.sleep(rng.uniform(silence_min_sec, silence_max_sec))
        return True

    return ensure_client_microphone(client, bot_index, False)


def speech_behavior_config():
    behavior = config.get("behavior", {})
    if not isinstance(behavior, dict):
        return {}
    speech = behavior.get("speech", {})
    if isinstance(speech, dict):
        return speech
    return {}


def call_client_action(client, bot_index, method_name, enabled):
    uri = 'http://' + client.ip + ':' + str(client.port)
    event_name = action_event_name(method_name, enabled)
    details = {
        "bot_index": bot_index,
        "client": uri,
        "method": method_name,
        "enabled": bool(enabled),
    }
    try:
        with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
            success = bool(getattr(proxy, method_name)(bool(enabled)))
        details["success"] = success
        emit_event(config, event_name, details)
        append_action_log(config, event_name, details)
        return success
    except Exception as exc:
        details["success"] = False
        details["error"] = str(exc)
        emit_event(config, "scenario_action_error", details)
        append_action_log(config, event_name, details)
        return False


def action_event_name(method_name, enabled):
    if method_name == "set_microphone":
        return "mic_on" if enabled else "mic_off"
    if method_name == "set_camera":
        return "camera_on" if enabled else "camera_off"
    if method_name == "set_screen_share":
        return "screen_share_start" if enabled else "screen_share_end"
    return method_name

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
    server.register_function(set_microphone, "set_microphone")
    server.register_function(set_camera, "set_camera")
    server.register_function(set_screen_share, "set_screen_share")
    server.register_function(get_name, "get_name")
    server.register_function(get_connection_status, "get_connection_status")
    server.register_function(run_connect, "run_connect")
    server.register_function(stop_video, "stop_video")

    server.serve_forever()


# XMLRPC
# Initialize client
# Set pulse audio devices and device volume
# Check for v4l2 kernel mod
def initialize_vtc_client():
    print("Initializing VTC client")
    if pulsectl is None:
        raise RuntimeError("Client mode requires the pulsectl Python package.")

    # Set up audio devices
    pulse = pulsectl.Pulse()
    sinks = pulse.sink_list()
    sources = pulse.source_list()
    audio_devices = virtual_audio_config()
    sink_name = audio_devices["sink_name"]
    source_name = audio_devices["source_name"]
    sink_description = audio_devices["sink_description"]
    source_description = audio_devices["source_description"]
    use_monitor_source = audio_devices["use_monitor_source"]

    if not pulse_has_device(sinks, sink_name) or not pulse_has_device(sources, source_name):
        print("Creating PulseAudio virtual devices")
        if not pulse_has_device(sinks, sink_name):
            subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={sink_name}",
                    f"sink_properties=device.description={sink_description}",
                ],
                capture_output=True,
            )
        if not use_monitor_source and not pulse_has_device(sources, source_name):
            subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-remap-source",
                    f"master={sink_name}.monitor",
                    f"source_name={source_name}",
                    f"source_properties=device.description={source_description}",
                ],
                capture_output=True,
            )
        sinks = pulse.sink_list()
        sources = pulse.source_list()
        emit_event(
            config,
            "virtual_audio_configured",
            {
                "sink_name": sink_name,
                "source_name": source_name,
                "sink_found": pulse_has_device(sinks, sink_name),
                "source_found": pulse_has_device(sources, source_name),
                "use_monitor_source": use_monitor_source,
            },
        )

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
    append_action_log(config, "speech_start", {"bot_name": config.get("bot_name"), "source": "dialog_cycle"})

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
        append_action_log(config, "speech_end", {"bot_name": config.get("bot_name"), "source": "dialog_cycle"})


def start_speech(duration_sec, metadata=None):
    global speech_thread_active
    global speech_until

    metadata = dict(metadata or {})
    duration_sec = float(duration_sec)
    now = time.time()

    audio_file_path = metadata.get("audio_file_path") or choose_icsi_audio_file(metadata)
    if audio_file_path:
        metadata["audio_file_path"] = audio_file_path
        emit_event(
            config,
            "speech_start",
            {
                "duration_sec": duration_sec,
                **metadata,
            },
        )
        append_action_log(config, "speech_start", {"duration_sec": duration_sec, **metadata})
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
    append_action_log(config, "speech_start", {"duration_sec": duration_sec, **metadata})

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
        append_action_log(config, "speech_end", dict(metadata or {}))


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
        append_action_log(config, "speech_end", dict(metadata or {}))


def choose_icsi_audio_file(metadata):
    meeting_id = metadata.get("meeting_id")
    if not meeting_id:
        return None

    speaker_id = str(metadata.get("speaker_id") or "")
    channel = str(metadata.get("channel") or "")
    file_channel = str(metadata.get("file_channel") or "")
    candidate_names = []
    for value in (speaker_id, channel, file_channel):
        if not value:
            continue
        candidate_names.extend(
            [
                f"{value}.wav",
                f"{meeting_id}.{value}.wav",
                f"chan{value}.wav",
                f"{meeting_id}.chan{value}.wav",
            ]
        )
        if value.lower().startswith("c") and value[1:].isdigit():
            candidate_names.extend(
                [
                    f"chan{value[1:]}.wav",
                    f"{meeting_id}.chan{value[1:]}.wav",
                ]
            )
    candidate_names = list(dict.fromkeys(candidate_names))

    for root in icsi_audio_roots():
        meeting_dir = icsi_meeting_audio_dir(root, str(meeting_id))
        if not meeting_dir.exists():
            continue

        for filename in candidate_names:
            if filename == ".wav":
                continue
            path = meeting_dir / filename
            if path.exists():
                return str(path)

        wav_files = sorted(meeting_dir.glob("*.wav"))
        matching_wav_files = [
            path
            for path in wav_files
            if (speaker_id and speaker_id.lower() in path.stem.lower())
            or (channel and channel.lower() in path.stem.lower())
            or (file_channel and file_channel.lower() in path.stem.lower())
        ]
        if len(matching_wav_files) == 1:
            return str(matching_wav_files[0])

    emit_event(
        config,
        "icsi_audio_not_found",
        {
            "meeting_id": meeting_id,
            "speaker_id": speaker_id,
            "channel": channel,
            "file_channel": file_channel,
            "searched_roots": [str(root) for root in icsi_audio_roots()],
        },
    )
    return None


def icsi_meeting_audio_dir(root, meeting_id):
    meeting_dir = root / meeting_id
    if meeting_dir.exists():
        return meeting_dir

    configured_meeting_dir = config.get("icsi_audio_meeting_dir")
    if configured_meeting_dir:
        configured_path = Path(str(configured_meeting_dir)).expanduser()
        if not configured_path.is_absolute():
            configured_path = root / configured_path
        if configured_path.exists():
            return configured_path

    if root.exists():
        child_dirs = [path for path in root.iterdir() if path.is_dir()]
        if len(child_dirs) == 1:
            emit_event(
                config,
                "icsi_audio_meeting_fallback",
                {
                    "requested_meeting_id": meeting_id,
                    "fallback_dir": str(child_dirs[0]),
                },
            )
            return child_dirs[0]

    return meeting_dir


def icsi_audio_roots():
    roots = []
    icsi_config = config.get("icsi", {})
    if not isinstance(icsi_config, dict):
        icsi_config = {}

    for value in (
        config.get("icsi_audio_root"),
        config.get("icsi_signals_dir"),
        icsi_config.get("audio_root"),
        icsi_config.get("signals_dir"),
    ):
        if value:
            roots.append(resolve_project_path(str(value)))

    roots.extend(
        [
            project_root() / "media" / "icsi" / "Signals",
            project_root() / "media" / "icsi" / "signals",
            project_root() / "media" / "icsi" / "Signal",
            project_root() / "media" / "icsi" / "signal",
        ]
    )

    unique_roots = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            unique_roots.append(root)
            seen.add(key)
    return unique_roots


def resolve_project_path(value):
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return project_root() / path


def project_root():
    return Path(__file__).resolve().parents[1]


def virtual_audio_config():
    virtual_audio = config.get("virtual_audio", {})
    if not isinstance(virtual_audio, dict):
        virtual_audio = {}

    adapter_config = config.get("adapter_config", {})
    if not isinstance(adapter_config, dict):
        adapter_config = {}

    configured_microphone = adapter_config.get("microphone_name")
    source_name = str(
        virtual_audio.get("source_name")
        or config.get("audio_source_name")
        or configured_microphone
        or "VTC_Microphone"
    )
    inferred_sink_name = source_name[:-len(".monitor")] if source_name.endswith(".monitor") else None
    sink_name = str(
        virtual_audio.get("sink_name")
        or config.get("audio_sink_name")
        or inferred_sink_name
        or "VTC_Speaker"
    )
    use_monitor_source = source_name == f"{sink_name}.monitor"
    return {
        "sink_name": sink_name,
        "source_name": source_name,
        "sink_description": str(virtual_audio.get("sink_description") or sink_name),
        "source_description": str(virtual_audio.get("source_description") or source_name),
        "use_monitor_source": use_monitor_source,
    }


def pulse_has_device(devices, name):
    return any(getattr(device, "name", None) == name for device in devices)


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
    subprocess.run(['paplay', '-d', virtual_audio_config()["sink_name"], audio_file_path], capture_output=True)


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
            ['paplay', '-d', virtual_audio_config()["sink_name"]],
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
        if ffmpeg is None:
            raise RuntimeError("Client video playback requires the ffmpeg-python package.")

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


def set_microphone(enabled):
    return run_adapter_action(
        "unmute" if enabled else "mute",
        "mic_on" if enabled else "mic_off",
        {"enabled": bool(enabled)},
    )


def set_camera(enabled):
    return run_adapter_action(
        "camera_on" if enabled else "camera_off",
        "camera_on" if enabled else "camera_off",
        {"enabled": bool(enabled)},
    )


def set_screen_share(enabled):
    return run_adapter_action(
        "start_screen_share" if enabled else "stop_screen_share",
        "screen_share_start" if enabled else "screen_share_end",
        {"enabled": bool(enabled)},
    )


def run_adapter_action(method_name, event_name, details=None):
    details = dict(details or {})
    with active_adapter_lock:
        adapter = active_adapter
        loop = active_loop

    if adapter is None or loop is None or loop.is_closed():
        details.update({"success": False, "error": "active adapter is not available"})
        emit_event(config, event_name, details)
        append_action_log(config, event_name, details)
        return False

    try:
        coroutine = getattr(adapter, method_name)
        future = asyncio.run_coroutine_threadsafe(coroutine(), loop)
        success = bool(future.result(timeout=float(config.get("adapter_action_timeout_sec", 20))))
        details["success"] = success
        emit_event(config, event_name, details)
        append_action_log(config, event_name, details)
        return success
    except Exception as exc:
        details.update({"success": False, "error": str(exc)})
        emit_event(config, event_name, details)
        append_action_log(config, event_name, details)
        return False


# XMLRPC
async def connect_vtc_session(duration):
    global active_adapter
    global active_loop

    service = get_service_name(config)
    packet_capture = PacketCaptureSession.from_config(config)
    adapter = None
    try:
        set_connection_status(
            "connecting",
            connected=False,
            error=None,
            vtc_url=config.get("vtc_url"),
        )
        emit_event(
            config,
            "connect_vtc_session_start",
            {"vtc_url": config.get("vtc_url")},
            service,
        )
        packet_capture.start()
        config["_meeting_joined_callback"] = mark_meeting_joined
        adapter = get_adapter(config)
        with active_adapter_lock:
            active_adapter = adapter
            active_loop = asyncio.get_running_loop()
        result = await adapter.connect(duration)
        set_connection_status(
            "done",
            connected=False,
            error=None,
            vtc_url=config.get("vtc_url"),
        )
        emit_event(
            config,
            "connect_vtc_session_done",
            {"vtc_url": config.get("vtc_url")},
            service,
        )
        append_action_log(
            config,
            "meeting_end",
            {"vtc_url": config.get("vtc_url"), "service": service, "success": True},
        )
        return result
    except Exception as exc:
        set_connection_status(
            "error",
            connected=False,
            error=str(exc),
            vtc_url=config.get("vtc_url"),
        )
        emit_event(
            config,
            "adapter_error",
            {"error": str(exc), "vtc_url": config.get("vtc_url")},
            service,
        )
        raise
    finally:
        packet_capture.stop_and_analyze()
        with active_adapter_lock:
            if active_adapter is adapter:
                active_adapter = None
                active_loop = None

def run_connect(duration):
    thread = threading.Thread(
        target=lambda: asyncio.run(connect_vtc_session(duration)),
        daemon=True,
    )
    thread.start()
    return True


def set_connection_status(state, connected=False, error=None, vtc_url=None):
    status = {
        "state": state,
        "connected": bool(connected),
        "error": error,
        "vtc_url": vtc_url,
        "updated_at": time.time(),
    }
    with connection_status_lock:
        connection_status.update(status)
    config["_connection_status"] = dict(connection_status)
    emit_event(config, "connection_status_updated", status)


def mark_meeting_joined(vtc_url=None):
    set_connection_status(
        "meeting_joined",
        connected=True,
        error=None,
        vtc_url=vtc_url or config.get("vtc_url"),
    )
    append_action_log(
        config,
        "meeting_start",
        {"vtc_url": vtc_url or config.get("vtc_url"), "service": get_service_name(config)},
    )


def get_connection_status():
    with connection_status_lock:
        return dict(connection_status)


# XMLRPC
def stop_video(video_pid):
    print("Stopping video")
    if video_pid:
        os.kill(video_pid, signal.SIGTERM)
    details = {"video_pid": video_pid, "success": bool(video_pid)}
    emit_event(config, "camera_off", details)
    append_action_log(config, "camera_off", details)


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
