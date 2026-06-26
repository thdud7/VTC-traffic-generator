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
from datetime import datetime, timezone
from concurrent.futures import wait as wait_futures

from vtc_behavior import ICSIReplayPolicy
from vtc_automation.adapters import get_adapter
from vtc_automation.event_log import emit_event, get_service_name, utc_now_iso
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
video_lock = threading.Lock()
video_process = None
video_supervisor_thread = None
video_supervisor_stop = threading.Event()
session_stop_requested = threading.Event()
active_adapter_lock = threading.Lock()
active_adapter = None
active_loop = None
connection_status_lock = threading.Lock()
connection_status = {
    "state": "idle",
    "connected": False,
    "ready": False,
    "media_ready": False,
    "error": None,
    "error_code": None,
    "stage": None,
    "reason": None,
    "vtc_url": None,
    "updated_at": None,
    "transition_history": [],
}
TERMINAL_CONNECTION_STATES = {"error"}
MEDIA_READY_CONNECTION_STATES = {"media_ready", "running"}


def append_action_log(log_config, event_name, details=None):
    details = dict(details or {})
    path = action_log_path(log_config)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
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
        statuses = poll_client_connection_statuses(vtc_clients)
        ready_count = count_media_ready_clients(statuses)
        terminal_errors = terminal_client_errors(statuses)
        if terminal_errors:
            raise RuntimeError(
                "Client reached terminal connection error before ICSI replay: "
                + json.dumps(terminal_errors, sort_keys=True)
            )

        if statuses != last_statuses:
            print("Client media readiness status: " + json.dumps(statuses, sort_keys=True))
            last_statuses = statuses

        if ready_count == len(vtc_clients):
            if grace_sec > 0:
                print(f"All clients media-ready. Waiting {grace_sec} more seconds before ICSI replay.")
                grace_deadline = time.time() + grace_sec
                while time.time() < grace_deadline:
                    time.sleep(min(1, max(0, grace_deadline - time.time())))
                    statuses = poll_client_connection_statuses(vtc_clients)
                    terminal_errors = terminal_client_errors(statuses)
                    if terminal_errors:
                        raise RuntimeError(
                            "Client reached terminal connection error during ICSI grace period: "
                            + json.dumps(terminal_errors, sort_keys=True)
                        )
                    if count_media_ready_clients(statuses) != len(vtc_clients):
                        print("Client media readiness changed during grace period.")
                        break
                else:
                    emit_event(
                        config,
                        "client_media_barrier_ready",
                        {"statuses": statuses, "grace_sec": grace_sec},
                    )
                    return
                continue
            emit_event(
                config,
                "client_media_barrier_ready",
                {"statuses": statuses, "grace_sec": grace_sec},
            )
            return

        time.sleep(1)

    raise TimeoutError(
        "Timed out waiting for all clients to join before ICSI replay: "
        + json.dumps(last_statuses, sort_keys=True)
    )


def poll_client_connection_statuses(vtc_clients):
    statuses = {}
    for index, client in enumerate(vtc_clients):
        uri = 'http://' + client.ip + ':' + str(client.port)
        try:
            with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
                status = proxy.get_connection_status()
        except Exception as exc:
            status = {"state": "rpc_error", "connected": False, "ready": False, "media_ready": False, "error": str(exc)}
        statuses[index] = status
    return statuses


def client_is_media_ready(status):
    state = status.get("state")
    return bool(status.get("media_ready") or status.get("ready") or state in MEDIA_READY_CONNECTION_STATES)


def count_media_ready_clients(statuses):
    return sum(1 for status in statuses.values() if client_is_media_ready(status))


def terminal_client_errors(statuses):
    return {
        index: status
        for index, status in statuses.items()
        if status.get("state") in TERMINAL_CONNECTION_STATES
    }


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
    if bool(icsi_config.get("strict_timing", False)):
        run_strict_icsi_replay_controller(vtc_clients, icsi_policy, icsi_config, max_duration_sec)
        return

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
    scenario_state = make_scenario_state(vtc_clients)
    scenario_runtime_sec = min(max_duration_sec, icsi_policy.end_sec) * time_scale
    if is_random_scenario_enabled():
        scenario_thread = threading.Thread(
            target=random_scenario_worker,
            args=(vtc_clients, scenario_runtime_sec, time_scale, scenario_stop, scenario_state),
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
            mark_speech_active(scenario_state, event.bot_index, duration_sec)
            if ensure_client_microphone(client, event.bot_index, True):
                set_scenario_state(scenario_state, event.bot_index, "mic", True)
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
    request_clients_to_stop_sessions(vtc_clients)
    wait_for_clients_to_finish_sessions(vtc_clients, float(icsi_config.get("client_stop_timeout_sec", 90)))
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(client.video_pid)


def run_strict_icsi_replay_controller(vtc_clients, icsi_policy, icsi_config, max_duration_sec):
    scenario_offset_sec = float(icsi_config.get("scenario_offset_sec", 0))
    mic_pre_roll_sec = float(icsi_config.get("mic_pre_roll_sec", 4.0))
    mic_action_timeout_sec = float(icsi_config.get("mic_action_timeout_sec", 3.0))
    mic_state_cache_ttl_sec = float(icsi_config.get("mic_state_cache_ttl_sec", 10.0))
    post_speech_mic_off_probability = float(icsi_config.get("post_speech_mic_off_probability", 0.5))
    min_safe_gap_sec = float(icsi_config.get("min_safe_gap_for_post_speech_mic_off_sec", 6.0))
    random_seed = int(icsi_config.get("random_seed", 12345))
    rng = random.Random(random_seed)

    utterances = build_icsi_strict_schedule(
        icsi_policy,
        max_duration_sec=max_duration_sec,
        scenario_offset_sec=scenario_offset_sec,
    )
    scenario_state = make_scenario_state(vtc_clients)
    scenario_state["random_mic_off_disallowed"] = True
    scenario_runtime_sec = min(max_duration_sec, icsi_policy.end_sec - scenario_offset_sec)
    scenario_thread = None
    scenario_stop = threading.Event()
    if is_random_scenario_enabled():
        scenario_thread = threading.Thread(
            target=random_scenario_worker,
            args=(vtc_clients, scenario_runtime_sec, 1.0, scenario_stop, scenario_state),
            daemon=True,
        )
        scenario_thread.start()

    scenario_start_utc_dt = datetime.now(timezone.utc)
    scenario_start_utc = scenario_start_utc_dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    scenario_start_monotonic_ns = time.monotonic_ns()
    emit_event(
        config,
        "scenario_start",
        {
            **icsi_policy.summary(),
            "strict_timing": True,
            "scenario_start_utc": scenario_start_utc,
            "scenario_start_monotonic_ns": scenario_start_monotonic_ns,
            "scenario_offset_sec": scenario_offset_sec,
            "max_duration_sec": max_duration_sec,
            "scheduled_utterance_count": len(utterances),
            "random_seed": random_seed,
        },
    )
    append_action_log(
        config,
        "scenario_start",
        {
            "role": "controller",
            "meeting_id": icsi_policy.meeting_id,
            "strict_timing": True,
            "scheduled_utterance_count": len(utterances),
        },
    )

    scheduled_events = []
    for utterance in utterances:
        prepare_t = max(0.0, utterance["scheduled_start_sec"] - mic_pre_roll_sec)
        scheduled_events.append((prepare_t, 0, "prepare_mic_on", utterance))
        scheduled_events.append((utterance["scheduled_start_sec"], 1, "speech_start", utterance))
        if not utterance["clipped"]:
            scheduled_events.append((utterance["scheduled_end_sec"], 2, "speech_end", utterance))
    scheduled_events.sort(key=lambda item: (item[0], item[1], item[3]["speech_id"]))

    futures = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(8, len(vtc_clients) * 4))
    try:
        for scheduled_t_rel_sec, _, event_type, utterance in scheduled_events:
            target_ns = scenario_start_monotonic_ns + seconds_to_ns(scheduled_t_rel_sec)
            sleep_until_monotonic_ns(target_ns)
            now_ns = time.monotonic_ns()
            drift_ms = ns_to_ms(now_ns - target_ns)
            if event_type == "prepare_mic_on":
                mark_pre_roll_active(scenario_state, utterance["bot_index"], utterance["scheduled_start_sec"], scenario_start_monotonic_ns)
                futures.append(
                    submit_mic_prepare(
                        executor,
                        vtc_clients[utterance["bot_index"]],
                        utterance,
                        scenario_state,
                        scenario_start_utc_dt,
                        scenario_start_monotonic_ns,
                        scheduled_t_rel_sec,
                        target_ns,
                        mic_state_cache_ttl_sec,
                        mic_action_timeout_sec,
                        drift_ms,
                    )
                )
            elif event_type == "speech_start":
                mark_pre_roll_inactive(scenario_state, utterance["bot_index"])
                mark_speech_active(scenario_state, utterance["bot_index"], utterance["playback_duration_sec"])
                mic_verified = mic_state_is_fresh_on(
                    scenario_state,
                    utterance["bot_index"],
                    mic_state_cache_ttl_sec,
                    now_ns,
                )
                request_details = strict_speech_event_details(
                    utterance,
                    scenario_start_utc_dt,
                    scenario_start_monotonic_ns,
                    scheduled_t_rel_sec,
                    target_ns,
                    drift_ms,
                    {
                        "mic_verified_before_start": mic_verified,
                        "random_seed": random_seed,
                    },
                )
                emit_event(config, "speech_start_request", request_details)
                append_action_log(config, "speech_start_request", request_details)
                if not mic_verified:
                    emit_event(config, "speech_started_with_mic_unverified", request_details)
                    append_action_log(config, "speech_started_with_mic_unverified", request_details)
                futures.append(
                    executor.submit(
                        send_start_speech_request,
                        vtc_clients[utterance["bot_index"]],
                        utterance,
                        scenario_start_utc_dt,
                        scenario_start_monotonic_ns,
                    )
                )
            elif event_type == "speech_end":
                futures.append(
                    handle_post_speech_mic_decision(
                        executor,
                        vtc_clients,
                        utterance,
                        scenario_state,
                        scenario_start_utc_dt,
                        scenario_start_monotonic_ns,
                        rng,
                        post_speech_mic_off_probability,
                        min_safe_gap_sec,
                        mic_action_timeout_sec,
                        random_seed,
                        drift_ms,
                    )
                )

        sleep_until_monotonic_ns(scenario_start_monotonic_ns + seconds_to_ns(scenario_runtime_sec))
        if futures:
            wait_futures([future for future in futures if future is not None], timeout=mic_action_timeout_sec + 15)
    finally:
        scenario_stop.set()
        if scenario_thread:
            scenario_thread.join(timeout=10)
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    emit_event(
        config,
        "icsi_replay_done",
        {"meeting_id": icsi_policy.meeting_id, "strict_timing": True},
    )
    append_action_log(
        config,
        "meeting_end",
        {"role": "controller", "meeting_id": icsi_policy.meeting_id, "strict_timing": True},
    )

    print("ICSI replay complete, closing session now.")
    request_clients_to_stop_sessions(vtc_clients)
    wait_for_clients_to_finish_sessions(vtc_clients, float(icsi_config.get("client_stop_timeout_sec", 90)))
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        with xmlrpc.client.ServerProxy(uri) as proxy:
            proxy.stop_video(client.video_pid)


def build_icsi_strict_schedule(icsi_policy, max_duration_sec, scenario_offset_sec=0.0):
    utterances = []
    per_bot_counts: dict[int, int] = {}
    for event in icsi_policy.events:
        scheduled_start_sec = float(event.start_sec) - scenario_offset_sec
        scheduled_end_sec = float(event.end_sec) - scenario_offset_sec
        if scheduled_start_sec < 0:
            continue
        if scheduled_start_sec >= max_duration_sec:
            break
        clipped = scheduled_end_sec > max_duration_sec
        playback_end_sec = min(scheduled_end_sec, max_duration_sec)
        playback_duration_sec = max(0.05, playback_end_sec - scheduled_start_sec)
        bot_count = per_bot_counts.get(event.bot_index, 0)
        per_bot_counts[event.bot_index] = bot_count + 1
        speech_id = (
            f"{event.meeting_id}:{event.speaker_id}:{event.bot_index}:"
            f"{scheduled_start_sec:.3f}:{scheduled_end_sec:.3f}:{bot_count}"
        )
        utterances.append(
            {
                "speech_id": speech_id,
                "meeting_id": event.meeting_id,
                "icsi_meeting_id": event.meeting_id,
                "speaker_id": event.speaker_id,
                "icsi_participant": event.speaker_id,
                "bot_index": event.bot_index,
                "channel": event.channel,
                "icsi_channel": event.channel,
                "file_channel": event.file_channel,
                "dialogue_act_type": event.dialogue_act_type,
                "annotation_start_sec": float(event.start_sec),
                "annotation_end_sec": float(event.end_sec),
                "scheduled_start_sec": scheduled_start_sec,
                "scheduled_end_sec": scheduled_end_sec,
                "playback_end_sec": playback_end_sec,
                "playback_duration_sec": playback_duration_sec,
                "audio_start_sec": float(event.start_sec),
                "clipped": clipped,
                "next_same_bot_start_sec": None,
            }
        )

    next_start_by_bot: dict[int, float] = {}
    for utterance in reversed(utterances):
        utterance["next_same_bot_start_sec"] = next_start_by_bot.get(utterance["bot_index"])
        next_start_by_bot[utterance["bot_index"]] = utterance["scheduled_start_sec"]
    return utterances


def seconds_to_ns(value):
    return int(float(value) * 1_000_000_000)


def ns_to_ms(value):
    return float(value) / 1_000_000.0


def sleep_until_monotonic_ns(target_ns):
    while True:
        remaining_ns = target_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            return
        time.sleep(min(remaining_ns / 1_000_000_000.0, 0.05))


def scheduled_utc(scenario_start_utc_dt, scheduled_t_rel_sec):
    return (
        scenario_start_utc_dt.timestamp() + float(scheduled_t_rel_sec)
    )


def scheduled_utc_iso(scenario_start_utc_dt, scheduled_t_rel_sec):
    return datetime.fromtimestamp(
        scheduled_utc(scenario_start_utc_dt, scheduled_t_rel_sec),
        timezone.utc,
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")


def strict_speech_event_details(
    utterance,
    scenario_start_utc_dt,
    scenario_start_monotonic_ns,
    scheduled_t_rel_sec,
    scheduled_monotonic_ns,
    drift_ms,
    extra=None,
):
    return {
        "meeting_id": utterance["meeting_id"],
        "speech_id": utterance["speech_id"],
        "bot_index": utterance["bot_index"],
        "icsi_meeting_id": utterance["icsi_meeting_id"],
        "icsi_participant": utterance["icsi_participant"],
        "icsi_channel": utterance["icsi_channel"],
        "scheduled_t_rel_sec": float(scheduled_t_rel_sec),
        "scheduled_utc": scheduled_utc_iso(scenario_start_utc_dt, scheduled_t_rel_sec),
        "scheduled_monotonic_ns": scheduled_monotonic_ns,
        "actual_utc": utc_now_iso(),
        "actual_monotonic_ns": time.monotonic_ns(),
        "drift_ms": drift_ms,
        "clipped": bool(utterance.get("clipped")),
        "scenario_start_monotonic_ns": scenario_start_monotonic_ns,
        **dict(extra or {}),
    }


def drift_from_scheduled_utc_ms(scheduled_utc_value, actual_utc_value):
    if not scheduled_utc_value:
        return None
    try:
        scheduled = datetime.fromisoformat(str(scheduled_utc_value).replace("Z", "+00:00"))
        actual = datetime.fromisoformat(str(actual_utc_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (actual - scheduled).total_seconds() * 1000.0


def add_seconds_to_utc_iso(utc_value, seconds):
    if not utc_value:
        return None
    try:
        timestamp = datetime.fromisoformat(str(utc_value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    return datetime.fromtimestamp(timestamp + float(seconds), timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def mark_pre_roll_active(scenario_state, bot_index, scheduled_start_sec, scenario_start_monotonic_ns):
    with scenario_state["lock"]:
        scenario_state["pre_roll_until_monotonic_ns"][bot_index] = (
            scenario_start_monotonic_ns + seconds_to_ns(scheduled_start_sec)
        )


def mark_pre_roll_inactive(scenario_state, bot_index):
    with scenario_state["lock"]:
        scenario_state["pre_roll_until_monotonic_ns"][bot_index] = 0


def mic_state_is_fresh_on(scenario_state, bot_index, ttl_sec, now_ns=None):
    now_ns = now_ns or time.monotonic_ns()
    with scenario_state["lock"]:
        is_on = scenario_state["states"][bot_index].get("mic") is True
        verified_at = int(scenario_state["mic_verified_at_monotonic_ns"][bot_index])
    if not is_on or verified_at <= 0:
        return False
    return now_ns - verified_at <= seconds_to_ns(ttl_sec)


def set_mic_verified_state(scenario_state, bot_index, enabled, verified):
    with scenario_state["lock"]:
        scenario_state["states"][bot_index]["mic"] = bool(enabled) if verified else scenario_state["states"][bot_index].get("mic")
        scenario_state["mic_verified_at_monotonic_ns"][bot_index] = time.monotonic_ns() if verified and enabled else 0


def submit_mic_prepare(
    executor,
    client,
    utterance,
    scenario_state,
    scenario_start_utc_dt,
    scenario_start_monotonic_ns,
    prepare_t_rel_sec,
    prepare_monotonic_ns,
    mic_state_cache_ttl_sec,
    mic_action_timeout_sec,
    drift_ms,
):
    now_ns = time.monotonic_ns()
    details = strict_speech_event_details(
        utterance,
        scenario_start_utc_dt,
        scenario_start_monotonic_ns,
        prepare_t_rel_sec,
        prepare_monotonic_ns,
        drift_ms,
        {
            "requested_state": True,
            "mic_state_cache_ttl_sec": mic_state_cache_ttl_sec,
        },
    )
    if mic_state_is_fresh_on(scenario_state, utterance["bot_index"], mic_state_cache_ttl_sec, now_ns):
        details.update({"success": True, "suppressed": True, "suppression_reason": "mic_already_verified_on"})
        emit_event(config, "speech_prepare_mic_on_result", details)
        append_action_log(config, "speech_prepare_mic_on_result", details)
        return None

    emit_event(config, "speech_prepare_mic_on_start", details)
    append_action_log(config, "speech_prepare_mic_on_start", details)
    return executor.submit(
        call_client_action_with_result,
        client,
        utterance["bot_index"],
        "set_microphone",
        True,
        "speech_prepare_mic_on_result",
        details,
        scenario_state,
        mic_action_timeout_sec,
    )


def send_start_speech_request(client, utterance, scenario_start_utc_dt, scenario_start_monotonic_ns):
    uri = 'http://' + client.ip + ':' + str(client.port)
    scheduled_t_rel_sec = utterance["scheduled_start_sec"]
    scheduled_monotonic_ns = scenario_start_monotonic_ns + seconds_to_ns(scheduled_t_rel_sec)
    send_ns = time.monotonic_ns()
    metadata = {
        "meeting_id": utterance["meeting_id"],
        "speech_id": utterance["speech_id"],
        "speaker_id": utterance["speaker_id"],
        "channel": utterance["channel"],
        "file_channel": utterance["file_channel"],
        "dialogue_act_type": utterance["dialogue_act_type"],
        "icsi_start_sec": utterance["annotation_start_sec"],
        "icsi_end_sec": utterance["annotation_end_sec"],
        "duration_sec": utterance["playback_duration_sec"],
        "audio_start_sec": utterance["audio_start_sec"],
        "scheduled_t_rel_sec": scheduled_t_rel_sec,
        "scheduled_utc": scheduled_utc_iso(scenario_start_utc_dt, scheduled_t_rel_sec),
        "scheduled_monotonic_ns": scheduled_monotonic_ns,
        "scenario_start_monotonic_ns": scenario_start_monotonic_ns,
        "icsi_meeting_id": utterance["icsi_meeting_id"],
        "icsi_participant": utterance["icsi_participant"],
        "icsi_channel": utterance["icsi_channel"],
        "clipped": utterance["clipped"],
    }
    details = strict_speech_event_details(
        utterance,
        scenario_start_utc_dt,
        scenario_start_monotonic_ns,
        scheduled_t_rel_sec,
        scheduled_monotonic_ns,
        ns_to_ms(send_ns - scheduled_monotonic_ns),
        {
            "rpc_send_utc": utc_now_iso(),
            "rpc_send_monotonic_ns": send_ns,
        },
    )
    try:
        with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
            success = bool(proxy.start_speech(utterance["playback_duration_sec"], metadata))
    except Exception as exc:
        success = False
        details["failure_reason"] = str(exc)
    receive_ns = time.monotonic_ns()
    details.update(
        {
            "success": success,
            "rpc_response_utc": utc_now_iso(),
            "rpc_response_monotonic_ns": receive_ns,
            "rpc_latency_ms": ns_to_ms(receive_ns - send_ns),
        }
    )
    emit_event(config, "speech_start_response", details)
    append_action_log(config, "speech_start_response", details)
    return success


def handle_post_speech_mic_decision(
    executor,
    vtc_clients,
    utterance,
    scenario_state,
    scenario_start_utc_dt,
    scenario_start_monotonic_ns,
    rng,
    post_speech_mic_off_probability,
    min_safe_gap_sec,
    mic_action_timeout_sec,
    random_seed,
    drift_ms,
):
    decision_off = rng.random() < post_speech_mic_off_probability
    next_start = utterance.get("next_same_bot_start_sec")
    gap_to_next = None if next_start is None else float(next_start) - float(utterance["scheduled_end_sec"])
    details = strict_speech_event_details(
        utterance,
        scenario_start_utc_dt,
        scenario_start_monotonic_ns,
        utterance["scheduled_end_sec"],
        scenario_start_monotonic_ns + seconds_to_ns(utterance["scheduled_end_sec"]),
        drift_ms,
        {
            "random_seed": random_seed,
            "random_decision": "mic_off" if decision_off else "keep_on",
            "post_speech_mic_off_probability": post_speech_mic_off_probability,
            "next_same_bot_start_sec": next_start,
            "gap_to_next_same_bot_sec": gap_to_next,
        },
    )
    emit_event(config, "post_speech_mic_decision", details)
    append_action_log(config, "post_speech_mic_decision", details)
    if not decision_off:
        return None

    suppression_reason = None
    if gap_to_next is not None and gap_to_next < min_safe_gap_sec:
        suppression_reason = "insufficient_gap_to_preserve_icsi_timing"
    elif bot_is_speaking_or_in_pre_roll(scenario_state, utterance["bot_index"]):
        suppression_reason = "bot_speaking_or_in_mic_pre_roll"
    if suppression_reason:
        suppressed_details = {
            **details,
            "suppressed": True,
            "suppression_reason": suppression_reason,
            "success": False,
        }
        emit_event(config, "post_speech_mic_action_result", suppressed_details)
        append_action_log(config, "post_speech_mic_action_result", suppressed_details)
        return None

    start_details = {
        **details,
        "requested_state": False,
        "suppressed": False,
    }
    emit_event(config, "post_speech_mic_action_start", start_details)
    append_action_log(config, "post_speech_mic_action_start", start_details)
    return executor.submit(
        call_client_action_with_result,
        vtc_clients[utterance["bot_index"]],
        utterance["bot_index"],
        "set_microphone",
        False,
        "post_speech_mic_action_result",
        start_details,
        scenario_state,
        mic_action_timeout_sec,
    )


def bot_is_speaking_or_in_pre_roll(scenario_state, bot_index):
    now_ns = time.monotonic_ns()
    with scenario_state["lock"]:
        return (
            scenario_state["speaking"][bot_index] > 0
            or int(scenario_state["pre_roll_until_monotonic_ns"][bot_index]) > now_ns
        )


def call_client_action_with_result(
    client,
    bot_index,
    method_name,
    enabled,
    result_event_name,
    details,
    scenario_state=None,
    timeout_sec=None,
):
    start_ns = time.monotonic_ns()
    uri = 'http://' + client.ip + ':' + str(client.port)
    result_details = {
        **dict(details or {}),
        "bot_index": bot_index,
        "client": uri,
        "method": method_name,
        "requested_state": bool(enabled),
        "rpc_send_utc": utc_now_iso(),
        "rpc_send_monotonic_ns": start_ns,
    }
    try:
        with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
            success = bool(getattr(proxy, method_name)(bool(enabled)))
    except Exception as exc:
        success = False
        result_details["failure_reason"] = str(exc)
    end_ns = time.monotonic_ns()
    result_details.update(
        {
            "success": success,
            "verified_state": bool(enabled) if success else None,
            "state_after": bool(enabled) if success else None,
            "rpc_response_utc": utc_now_iso(),
            "rpc_response_monotonic_ns": end_ns,
            "rpc_latency_ms": ns_to_ms(end_ns - start_ns),
            "timeout_sec": timeout_sec,
        }
    )
    if scenario_state is not None and method_name == "set_microphone":
        set_mic_verified_state(scenario_state, bot_index, bool(enabled), success)
    emit_event(config, result_event_name, result_details)
    append_action_log(config, result_event_name, result_details)
    return success


def request_clients_to_stop_sessions(vtc_clients):
    for client in vtc_clients:
        uri = 'http://' + client.ip + ':' + str(client.port)
        try:
            with xmlrpc.client.ServerProxy(uri, allow_none=True) as proxy:
                proxy.stop_vtc_session()
        except Exception as exc:
            emit_event(
                config,
                "client_stop_session_error",
                {"client": client.ip, "port": client.port, "error": str(exc)},
            )


def wait_for_clients_to_finish_sessions(vtc_clients, timeout_sec):
    deadline = time.time() + max(0, timeout_sec)
    last_statuses = {}
    while time.time() < deadline:
        statuses = poll_client_connection_statuses(vtc_clients)
        if statuses != last_statuses:
            print("Client shutdown status: " + json.dumps(statuses, sort_keys=True))
            last_statuses = statuses

        if all(status.get("state") in {"done", "error"} for status in statuses.values()):
            emit_event(config, "client_sessions_finished", {"statuses": statuses})
            return True
        time.sleep(1)

    emit_event(config, "client_sessions_finish_timeout", {"statuses": last_statuses, "timeout_sec": timeout_sec})
    return False


def scenario_config():
    behavior = config.get("behavior", {})
    if not isinstance(behavior, dict):
        return {}
    random_actions = behavior.get("random_actions")
    if isinstance(random_actions, dict):
        return random_actions
    scenario = behavior.get("scenario", {})
    if isinstance(scenario, dict):
        return scenario
    return {}


def is_random_scenario_enabled():
    scenario = scenario_config()
    return bool(scenario.get("enabled", True))


def random_scenario_worker(vtc_clients, runtime_sec, time_scale, stop_event, scenario_state=None):
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
    action_weights = [
        ("screen_share", max(0.0, screen_share_probability)),
        ("camera", max(0.0, camera_probability)),
        ("mic", max(0.0, mic_probability)),
    ]
    action_weights = [(name, weight) for name, weight in action_weights if weight > 0]
    screen_owner = None
    scenario_state = scenario_state or make_scenario_state(vtc_clients)
    deadline = time.time() + max(0, runtime_sec)
    append_action_log(
        config,
        "scenario_start",
        {
            "runtime_sec": runtime_sec,
            "client_count": len(vtc_clients),
            "action_weights": dict(action_weights),
        },
    )

    try:
        while action_weights and time.time() < deadline and not stop_event.is_set():
            interval = rng.uniform(min_interval, max_interval)
            if stop_event.wait(min(interval, max(0, deadline - time.time()))):
                break

            emit_event(
                config,
                "random_action_candidates",
                {
                    "action_weights": dict(action_weights),
                    "screen_owner": screen_owner,
                    "nonblocking": True,
                },
            )
            action_name = choose_weighted_action(rng, action_weights)
            emit_event(
                config,
                "random_action_selected",
                {"action": action_name, "screen_owner": screen_owner, "nonblocking": True},
            )
            if action_name == "screen_share":
                if screen_owner is None:
                    bot_index = rng.randrange(0, len(vtc_clients))
                    success = call_client_action(vtc_clients[bot_index], bot_index, "set_screen_share", True)
                    if success:
                        screen_owner = bot_index
                else:
                    success = call_client_action(vtc_clients[screen_owner], screen_owner, "set_screen_share", False)
                    if success:
                        screen_owner = None
            elif action_name == "camera":
                bot_index = rng.randrange(0, len(vtc_clients))
                desired_state = not get_scenario_state(scenario_state, bot_index, "camera")
                success = call_client_action(vtc_clients[bot_index], bot_index, "set_camera", desired_state)
                if success:
                    set_scenario_state(scenario_state, bot_index, "camera", desired_state)
            elif action_name == "mic":
                choice = choose_mic_action(rng, scenario_state, len(vtc_clients))
                if choice is None:
                    details = {"reason": "all candidate bots are speaking or in mic pre-roll"}
                    emit_event(config, "random_action_suppressed", details)
                    append_action_log(config, "mic_action_skipped", details)
                    continue
                bot_index, desired_state = choice
                success = call_client_action(vtc_clients[bot_index], bot_index, "set_microphone", desired_state)
                if success:
                    set_scenario_state(scenario_state, bot_index, "mic", desired_state)
    finally:
        if screen_owner is not None:
            call_client_action(vtc_clients[screen_owner], screen_owner, "set_screen_share", False)
        append_action_log(config, "scenario_end", {"runtime_sec": runtime_sec})


def make_scenario_state(vtc_clients):
    return {
        "lock": threading.Lock(),
        "states": [
            {
                "mic": None,
                "camera": True,
            }
            for _ in vtc_clients
        ],
        "speaking": [0 for _ in vtc_clients],
        "pre_roll_until_monotonic_ns": [0 for _ in vtc_clients],
        "mic_verified_at_monotonic_ns": [0 for _ in vtc_clients],
        "random_mic_off_disallowed": False,
    }


def choose_weighted_action(rng, action_weights):
    total = sum(weight for _, weight in action_weights)
    roll = rng.uniform(0, total)
    upto = 0.0
    for name, weight in action_weights:
        upto += weight
        if roll <= upto:
            return name
    return action_weights[-1][0]


def mark_speech_active(scenario_state, bot_index, duration_sec):
    with scenario_state["lock"]:
        scenario_state["speaking"][bot_index] += 1

    timer = threading.Timer(duration_sec, mark_speech_inactive, args=(scenario_state, bot_index))
    timer.daemon = True
    timer.start()


def mark_speech_inactive(scenario_state, bot_index):
    with scenario_state["lock"]:
        scenario_state["speaking"][bot_index] = max(0, scenario_state["speaking"][bot_index] - 1)


def get_scenario_state(scenario_state, bot_index, key):
    with scenario_state["lock"]:
        return bool(scenario_state["states"][bot_index][key])


def set_scenario_state(scenario_state, bot_index, key, value):
    with scenario_state["lock"]:
        scenario_state["states"][bot_index][key] = bool(value)


def choose_mic_action(rng, scenario_state, client_count):
    candidates = list(range(client_count))
    rng.shuffle(candidates)
    now_ns = time.monotonic_ns()
    with scenario_state["lock"]:
        for bot_index in candidates:
            desired_state = not bool(scenario_state["states"][bot_index]["mic"])
            in_speech_or_pre_roll = (
                scenario_state["speaking"][bot_index] > 0
                or int(scenario_state["pre_roll_until_monotonic_ns"][bot_index]) > now_ns
            )
            if desired_state or not in_speech_or_pre_roll:
                return bot_index, desired_state
    return None


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
    if client_config.get("videoconference"):
        ensure_video_supervisor()

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
    server.register_function(stop_vtc_session, "stop_vtc_session")
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

    set_default_pulse_devices(sink_name, source_name)
    run_audio_loopback_probe()

    # Check for v4l2 virtual webcam kernel module
    if 'v4l2loopback' not in str(subprocess.run(['lsmod'], capture_output=True)):
        print(
            "Error: v4l2loopback kernel module not loaded. Try: sudo modprobe v4l2loopback video_nr=5 exclusive_caps=1")

    print(config['bot_name'] + " configured.")


def set_default_pulse_devices(sink_name, source_name):
    results = {}
    for device_type, command in (
        ("sink", ["pactl", "set-default-sink", sink_name]),
        ("source", ["pactl", "set-default-source", source_name]),
    ):
        result = subprocess.run(command, capture_output=True, text=True)
        results[device_type] = {
            "name": command[-1],
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        }

    emit_event(
        config,
        "pulse_default_devices_configured",
        {
            "sink_name": sink_name,
            "source_name": source_name,
            "results": results,
        },
    )
    append_action_log(
        config,
        "pulse_default_devices_configured",
        {
            "sink_name": sink_name,
            "source_name": source_name,
            "results": results,
        },
    )
    return results


def audio_artifact_dir():
    bot_name = str(config.get("bot_name") or config.get("role") or "bot")
    configured_dir = config.get("artifact_dir")
    if configured_dir:
        return Path(str(configured_dir)).expanduser()
    return Path(f"/tmp/vtc-{bot_name}/artifacts")


def run_audio_loopback_probe():
    probe_config = config.get("audio_loopback_probe", {})
    if probe_config is None:
        probe_config = {}
    if not isinstance(probe_config, dict):
        probe_config = {}
    if not bool(probe_config.get("enabled", False)):
        return True

    audio_devices = virtual_audio_config()
    sink_name = audio_devices["sink_name"]
    source_name = audio_devices["source_name"]
    duration_sec = float(probe_config.get("duration_sec", 2.5))
    threshold_db = float(probe_config.get("peak_threshold_db", -50.0))
    artifact_dir = audio_artifact_dir() / "preflight"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    tone_path = artifact_dir / "audio-loopback-tone.wav"
    recorded_path = artifact_dir / "audio-loopback-recorded.wav"
    result_path = artifact_dir / "audio-loopback-probe.json"

    emit_event(
        config,
        "audio_loopback_probe_start",
        {
            "sink": sink_name,
            "source": source_name,
            "duration_sec": duration_sec,
            "recorded_path": str(recorded_path),
        },
    )

    result = {
        "sink": sink_name,
        "source": source_name,
        "duration_sec": duration_sec,
        "tone_path": str(tone_path),
        "recorded_path": str(recorded_path),
        "peak_threshold_db": threshold_db,
        "success": False,
    }

    try:
        tone = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=1000:duration={duration_sec}",
                "-ac",
                "1",
                "-ar",
                "48000",
                str(tone_path),
            ],
            capture_output=True,
            text=True,
            timeout=max(10, int(duration_sec) + 5),
        )
        result["tone_returncode"] = tone.returncode
        result["tone_stderr"] = tone.stderr.strip()
        if tone.returncode != 0:
            raise RuntimeError(f"failed to generate loopback tone: {tone.stderr.strip()}")

        recorder = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-f",
                "pulse",
                "-i",
                source_name,
                "-t",
                str(duration_sec + 0.5),
                "-ac",
                "1",
                "-ar",
                "48000",
                str(recorded_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.35)
        player = subprocess.Popen(
            ["paplay", "-d", sink_name, str(tone_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        result["record_pid"] = recorder.pid
        result["play_pid"] = player.pid
        play_stdout, play_stderr = player.communicate(timeout=max(10, int(duration_sec) + 5))
        record_stdout, record_stderr = recorder.communicate(timeout=max(10, int(duration_sec) + 8))
        result.update(
            {
                "play_returncode": player.returncode,
                "play_stdout": play_stdout.strip(),
                "play_stderr": play_stderr.strip(),
                "record_returncode": recorder.returncode,
                "record_stdout": record_stdout.strip(),
                "record_stderr": record_stderr.strip(),
            }
        )
        if player.returncode != 0:
            raise RuntimeError(f"loopback paplay failed: {play_stderr.strip()}")
        if recorder.returncode != 0:
            raise RuntimeError(f"loopback record failed: {record_stderr.strip()}")

        volume = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                str(recorded_path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        volume_text = "\n".join([volume.stdout, volume.stderr])
        result["volumedetect_returncode"] = volume.returncode
        result["volumedetect_output"] = volume_text
        max_volume = parse_volumedetect_value(volume_text, "max_volume")
        mean_volume = parse_volumedetect_value(volume_text, "mean_volume")
        result["max_volume_db"] = max_volume
        result["mean_volume_db"] = mean_volume
        result["recorded_size_bytes"] = recorded_path.stat().st_size if recorded_path.exists() else 0

        if max_volume is None:
            raise RuntimeError("loopback probe could not parse max_volume")
        if max_volume <= threshold_db:
            raise RuntimeError(f"loopback probe peak too low: {max_volume} dB")

        result["success"] = True
        event_name = "audio_loopback_probe_passed"
        return True
    except Exception as exc:
        result["error"] = str(exc)
        event_name = "audio_loopback_probe_failed"
        if bool(probe_config.get("required", True)):
            raise
        return False
    finally:
        result_path.write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
        emit_event(config, event_name, result)
        append_action_log(config, event_name, result)


def parse_volumedetect_value(text, key):
    marker = f"{key}:"
    for line in text.splitlines():
        if marker not in line:
            continue
        value = line.split(marker, 1)[1].strip().split(" ", 1)[0]
        try:
            return float(value)
        except ValueError:
            return None
    return None


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
        audio_path = Path(str(audio_file_path)).expanduser()
        if not audio_path.exists():
            emit_event(config, "audio_playback_failed", {"error": "audio file does not exist", **metadata})
            append_action_log(config, "audio_playback_failed", {"error": "audio file does not exist", **metadata})
            return False
        if duration_sec <= 0:
            emit_event(config, "audio_playback_failed", {"error": "invalid duration", **metadata})
            append_action_log(config, "audio_playback_failed", {"error": "invalid duration", **metadata})
            return False
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
            play_audio_segment(audio_file_path, audio_start_sec, duration_sec, metadata)
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
    audio_devices = virtual_audio_config()
    details = {
        "audio_file_path": str(audio_file_path),
        "sink": audio_devices["sink_name"],
        "source": audio_devices["source_name"],
    }
    emit_event(config, "audio_playback_start", details)
    append_action_log(config, "audio_playback_start", details)
    try:
        process = subprocess.Popen(
            ["paplay", "-d", audio_devices["sink_name"], audio_file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        emit_event(config, "audio_playback_process_started", {**details, "pid": process.pid})
        stdout, stderr = process.communicate()
        done_details = {
            **details,
            "pid": process.pid,
            "returncode": process.returncode,
            "stdout": stdout.strip(),
            "stderr": stderr.strip(),
        }
        event_name = "audio_playback_done" if process.returncode == 0 else "audio_playback_failed"
        emit_event(config, event_name, done_details)
        append_action_log(config, event_name, done_details)
        return process.returncode == 0
    except Exception as exc:
        failed_details = {**details, "error": str(exc)}
        emit_event(config, "audio_playback_failed", failed_details)
        append_action_log(config, "audio_playback_failed", failed_details)
        return False


def play_audio_segment(audio_file_path, start_sec, duration_sec, metadata=None):
    metadata = dict(metadata or {})
    audio_devices = virtual_audio_config()
    playback_start_utc = utc_now_iso()
    playback_start_monotonic_ns = time.monotonic_ns()
    details = {
        **metadata,
        "audio_file_path": str(audio_file_path),
        "audio_start_sec": float(start_sec),
        "duration_sec": float(duration_sec),
        "sink": audio_devices["sink_name"],
        "source": audio_devices["source_name"],
        "actual_utc": playback_start_utc,
        "actual_monotonic_ns": playback_start_monotonic_ns,
        "drift_ms": drift_from_scheduled_utc_ms(metadata.get("scheduled_utc"), playback_start_utc),
    }
    emit_event(config, "audio_playback_start", details)
    emit_event(config, "speech_playback_start", details)
    append_action_log(config, "audio_playback_start", details)
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
        stderr=subprocess.PIPE,
        text=False,
    )
    paplay_process = None
    try:
        paplay_process = subprocess.Popen(
            ['paplay', '-d', audio_devices["sink_name"]],
            stdin=ffmpeg_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        emit_event(
            config,
            "audio_playback_process_started",
            {**details, "ffmpeg_pid": ffmpeg_process.pid, "paplay_pid": paplay_process.pid},
        )
        if ffmpeg_process.stdout:
            ffmpeg_process.stdout.close()
        paplay_stdout, paplay_stderr = paplay_process.communicate()
        ffmpeg_stderr = ffmpeg_process.stderr.read() if ffmpeg_process.stderr else b""
        ffmpeg_process.wait()
        done_details = {
            **details,
            "ffmpeg_pid": ffmpeg_process.pid,
            "paplay_pid": paplay_process.pid,
            "ffmpeg_returncode": ffmpeg_process.returncode,
            "paplay_returncode": paplay_process.returncode,
            "ffmpeg_stderr": ffmpeg_stderr.decode("utf-8", errors="replace").strip() if ffmpeg_stderr else "",
            "paplay_stdout": paplay_stdout.decode("utf-8", errors="replace").strip() if paplay_stdout else "",
            "paplay_stderr": paplay_stderr.decode("utf-8", errors="replace").strip() if paplay_stderr else "",
            "actual_utc": utc_now_iso(),
            "actual_monotonic_ns": time.monotonic_ns(),
        }
        if not metadata.get("clipped"):
            scheduled_end_utc = add_seconds_to_utc_iso(metadata.get("scheduled_utc"), metadata.get("duration_sec", duration_sec))
            done_details["scheduled_end_utc"] = scheduled_end_utc
            done_details["end_drift_ms"] = drift_from_scheduled_utc_ms(scheduled_end_utc, done_details["actual_utc"])
        success = ffmpeg_process.returncode == 0 and paplay_process.returncode == 0
        event_name = "audio_playback_done" if success else "audio_playback_failed"
        emit_event(config, event_name, done_details)
        emit_event(config, "speech_playback_end", {**done_details, "success": success})
        append_action_log(config, event_name, done_details)
        return success
    finally:
        if paplay_process is not None and paplay_process.poll() is None:
            paplay_process.terminate()
        if ffmpeg_process.poll() is None:
            ffmpeg_process.terminate()


def video_stream_config():
    video_filepath = str(PurePath(config["video_path"], config["video_name"]))
    virtual_video_config = config.get("virtual_video", {})
    if not isinstance(virtual_video_config, dict):
        virtual_video_config = {}
    video_device = config.get("video_device") or virtual_video_config.get("device") or "/dev/video5"
    width = int(virtual_video_config.get("width", 640))
    height = int(virtual_video_config.get("height", 360))
    fps = int(virtual_video_config.get("fps", 15))
    source = str(virtual_video_config.get("source") or "file")
    default_font_size = 50 if "270" in str(config.get("video_name", "")) else 200
    font_size = int(virtual_video_config.get("font_size", default_font_size))
    y_position = str(virtual_video_config.get("y_position") or ("h-th-20" if "270" in str(config.get("video_name", "")) else "h-th-50"))
    return video_filepath, video_device, width, height, fps, font_size, y_position, source


def build_video_stream_process():
    if ffmpeg is None:
        raise RuntimeError("Client video playback requires the ffmpeg-python package.")

    video_filepath, video_device, width, height, fps, font_size, y_position, source = video_stream_config()
    if source in {"testsrc", "testsrc2", "lavfi"}:
        stream = ffmpeg.input(f"testsrc2=size={width}x{height}:rate={fps}", f="lavfi", re=None)
    else:
        stream = (
            ffmpeg
            .input(video_filepath, re=None, stream_loop=-1)
            .filter("fps", fps=fps)
            .filter("scale", width, -2)
        )

    return (
        stream
        .filter("format", "yuv420p")
        .drawtext(text=config["bot_name"], x="(w-text_w)/2", y=y_position, fontcolor="red", fontsize=font_size)
        .output(video_device, format="v4l2")
    )


def start_video_stream():
    global video_process

    if not config.get("videoconference"):
        return False

    with video_lock:
        if video_process is not None and video_process.poll() is None:
            return video_process.pid

        video_filepath, video_device, width, height, fps, _, _, source = video_stream_config()
        print(f"Launching video playback: {video_filepath} -> {video_device}")
        process = build_video_stream_process().run_async(pipe_stdin=True)
        video_process = process

    emit_event(
        config,
        "camera_stream_started",
        {
            "video_path": video_filepath,
            "video_device": video_device,
            "video_pid": process.pid,
            "width": width,
            "height": height,
            "fps": fps,
            "source": source,
        },
    )
    append_action_log(
        config,
        "camera_stream_started",
        {
            "video_path": video_filepath,
            "video_device": video_device,
            "video_pid": process.pid,
            "width": width,
            "height": height,
            "fps": fps,
            "source": source,
        },
    )
    return process.pid


def video_supervisor_worker():
    while not video_supervisor_stop.is_set():
        try:
            start_video_stream()
        except Exception as exc:
            emit_event(config, "camera_stream_error", {"error": str(exc)})
            append_action_log(config, "camera_stream_error", {"error": str(exc)})
        video_supervisor_stop.wait(2)


def ensure_video_supervisor():
    global video_supervisor_thread

    if not config.get("videoconference"):
        return False

    with video_lock:
        if video_supervisor_thread is not None and video_supervisor_thread.is_alive():
            return True
        video_supervisor_stop.clear()
        video_supervisor_thread = threading.Thread(target=video_supervisor_worker, daemon=True)
        video_supervisor_thread.start()
    return True


# XMLRPC
def play_video():
    ensure_video_supervisor()
    return start_video_stream()


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
    requested_state = details.get("enabled")
    start_ns = time.monotonic_ns()
    details.setdefault("requested_state", requested_state)
    details.setdefault("rpc_received_utc", utc_now_iso())
    details.setdefault("rpc_received_monotonic_ns", start_ns)
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
        details["verified_state"] = bool(requested_state) if success and requested_state is not None else None
        details["state_after"] = bool(requested_state) if success and requested_state is not None else None
        details["latency_ms"] = ns_to_ms(time.monotonic_ns() - start_ns)
        emit_event(config, event_name, details)
        if event_name.startswith("mic_"):
            emit_event(config, "mic_state_change", {**details, "automation_method": method_name})
        elif event_name.startswith("camera_"):
            emit_event(config, "camera_state_change", {**details, "automation_method": method_name})
        elif event_name.startswith("screen_share_"):
            emit_event(config, "screen_share_state_change", {**details, "automation_method": method_name})
        append_action_log(config, event_name, details)
        return success
    except Exception as exc:
        details.update({"success": False, "error": str(exc)})
        details["failure_reason"] = str(exc)
        details["latency_ms"] = ns_to_ms(time.monotonic_ns() - start_ns)
        emit_event(config, event_name, details)
        if event_name.startswith("mic_"):
            emit_event(config, "mic_state_change", {**details, "automation_method": method_name})
        elif event_name.startswith("camera_"):
            emit_event(config, "camera_state_change", {**details, "automation_method": method_name})
        elif event_name.startswith("screen_share_"):
            emit_event(config, "screen_share_state_change", {**details, "automation_method": method_name})
        append_action_log(config, event_name, details)
        return False


# XMLRPC
async def connect_vtc_session(duration):
    global active_adapter
    global active_loop

    service = get_service_name(config)
    packet_capture = PacketCaptureSession.from_config(config)
    adapter = None
    closed = False
    stop_reason = "unknown"
    try:
        set_connection_status(
            "initializing",
            connected=False,
            ready=False,
            media_ready=False,
            error=None,
            vtc_url=config.get("vtc_url"),
            stage="connect_vtc_session",
            reason="session_start",
        )
        emit_event(
            config,
            "connect_vtc_session_start",
            {"vtc_url": config.get("vtc_url")},
            service,
        )
        packet_capture.start()
        config["_meeting_joined_callback"] = mark_meeting_joined
        config["_media_ready_callback"] = mark_media_ready
        adapter = get_adapter(config)
        with active_adapter_lock:
            active_adapter = adapter
            active_loop = asyncio.get_running_loop()

        if hasattr(adapter, "launch") and hasattr(adapter, "connect_to_meeting"):
            set_connection_status(
                "launching",
                connected=False,
                ready=False,
                media_ready=False,
                error=None,
                vtc_url=config.get("vtc_url"),
                stage="adapter_launch",
                reason="launch_start",
            )
            await adapter.launch()
            set_connection_status(
                "joining",
                connected=False,
                ready=False,
                media_ready=False,
                error=None,
                vtc_url=config.get("vtc_url"),
                stage="adapter_join",
                reason="join_start",
            )
            emit_event(config, "meeting_join_start", {"vtc_url": config.get("vtc_url")}, service)
            await adapter.connect_to_meeting(
                vtc_url=str(config["vtc_url"]),
                display_name=adapter._display_name() if hasattr(adapter, "_display_name") else str(config.get("bot_name") or "bot"),
            )
            set_connection_status(
                "running",
                connected=True,
                ready=True,
                media_ready=True,
                error=None,
                vtc_url=config.get("vtc_url"),
                stage="meeting_running",
                reason="media_ready_confirmed",
            )
            emit_event(config, "meeting_join_ready", {"vtc_url": config.get("vtc_url")}, service)
            await wait_for_session_duration_or_stop(duration * 60)
            set_connection_status(
                "leaving",
                connected=True,
                ready=False,
                media_ready=False,
                error=None,
                vtc_url=config.get("vtc_url"),
                stage="adapter_leave",
                reason="duration_complete",
            )
            await adapter.leave()
            await adapter.close()
            closed = True
            success_postroll = postroll_seconds("success_postroll_sec", default=10)
            if success_postroll > 0:
                await asyncio.sleep(success_postroll)
            result = f"{config.get('bot_name') or 'client'} connected to {service}."
        else:
            set_connection_status(
                "connecting",
                connected=False,
                ready=False,
                media_ready=False,
                error=None,
                vtc_url=config.get("vtc_url"),
                stage="adapter_connect",
                reason="legacy_adapter_connect",
            )
            result = await adapter.connect(duration)

        stop_reason = "success"
        emit_event(
            config,
            "meeting_disconnected",
            {"vtc_url": config.get("vtc_url"), "terminal_state": "done"},
            service,
        )
        set_connection_status(
            "done",
            connected=False,
            ready=False,
            media_ready=False,
            error=None,
            vtc_url=config.get("vtc_url"),
            stage="connect_vtc_session",
            reason="session_done",
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
        stop_reason = f"failure:{type(exc).__name__}"
        set_connection_status(
            "error",
            connected=False,
            ready=False,
            media_ready=False,
            error=str(exc),
            vtc_url=config.get("vtc_url"),
            stage="connect_vtc_session",
            reason="exception",
            error_code=type(exc).__name__,
        )
        emit_event(
            config,
            "adapter_error",
            {"error": str(exc), "error_code": type(exc).__name__, "vtc_url": config.get("vtc_url")},
            service,
        )
        if adapter is not None and hasattr(adapter, "collect_diagnostics"):
            try:
                adapter.collect_diagnostics("connect_vtc_session_error", {"error": str(exc), "error_code": type(exc).__name__})
            except Exception as diagnostic_exc:
                emit_event(
                    config,
                    "session_diagnostics_failed",
                    {"stage": "connect_vtc_session_error", "error": str(diagnostic_exc)},
                    service,
                )
        failure_postroll = postroll_seconds("failure_postroll_sec", default=10)
        if failure_postroll > 0:
            await asyncio.sleep(failure_postroll)
        if adapter is not None and not closed and hasattr(adapter, "close"):
            try:
                await adapter.close()
            except Exception:
                pass
        raise
    finally:
        packet_capture.stop_and_analyze_with_reason(stop_reason)
        config.pop("_meeting_joined_callback", None)
        config.pop("_media_ready_callback", None)
        with active_adapter_lock:
            if active_adapter is adapter:
                active_adapter = None
                active_loop = None

def run_connect(duration):
    session_stop_requested.clear()
    thread = threading.Thread(
        target=lambda: asyncio.run(connect_vtc_session(duration)),
        daemon=True,
    )
    thread.start()
    return True


async def wait_for_session_duration_or_stop(duration_sec):
    deadline = time.time() + max(0, duration_sec)
    while time.time() < deadline:
        if session_stop_requested.is_set():
            emit_event(config, "session_stop_requested", {"remaining_sec": max(0, deadline - time.time())})
            return
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.time())))


def stop_vtc_session():
    session_stop_requested.set()
    emit_event(config, "stop_vtc_session_request", {"success": True})
    emit_event(config, "session_stop_request_received", {"success": True})
    append_action_log(config, "session_stop_request_received", {"success": True})
    return True


def postroll_seconds(key, default=10):
    adapter_config = config.get("adapter_config", {})
    if not isinstance(adapter_config, dict):
        adapter_config = {}
    packet_capture = config.get("packet_capture", {})
    if not isinstance(packet_capture, dict):
        packet_capture = {}
    capture = config.get("capture", {})
    if not isinstance(capture, dict):
        capture = {}
    capture_tail = capture.get("tail_after_disconnect_sec") if key == "success_postroll_sec" else None
    value = adapter_config.get(key, packet_capture.get(key, capture_tail if capture_tail is not None else config.get(key, default)))
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


def set_connection_status(
    state,
    connected=False,
    ready=False,
    media_ready=False,
    error=None,
    vtc_url=None,
    stage=None,
    reason=None,
    error_code=None,
):
    updated_at = time.time()
    status = {
        "state": state,
        "connected": bool(connected),
        "ready": bool(ready),
        "media_ready": bool(media_ready),
        "error": error,
        "error_code": error_code,
        "stage": stage,
        "reason": reason,
        "vtc_url": vtc_url,
        "updated_at": updated_at,
    }
    with connection_status_lock:
        previous = dict(connection_status)
        transition = {
            "previous_state": previous.get("state"),
            "new_state": state,
            "connected": bool(connected),
            "ready": bool(ready),
            "media_ready": bool(media_ready),
            "stage": stage,
            "reason": reason,
            "error_code": error_code,
            "updated_at": updated_at,
        }
        history = list(previous.get("transition_history") or [])
        history.append(transition)
        status["transition_history"] = history[-200:]
        connection_status.update(status)
    config["_connection_status"] = dict(connection_status)
    emit_event(
        config,
        "connection_status_updated",
        {
            **{key: value for key, value in status.items() if key != "transition_history"},
            "previous_state": previous.get("state"),
        },
    )


def mark_meeting_joined(vtc_url=None):
    set_connection_status(
        "meeting_joined",
        connected=True,
        ready=False,
        media_ready=False,
        error=None,
        vtc_url=vtc_url or config.get("vtc_url"),
        stage="adapter_join",
        reason="meeting_joined",
    )
    append_action_log(
        config,
        "meeting_start",
        {"vtc_url": vtc_url or config.get("vtc_url"), "service": get_service_name(config)},
    )


def mark_media_ready(vtc_url=None):
    set_connection_status(
        "media_ready",
        connected=True,
        ready=True,
        media_ready=True,
        error=None,
        vtc_url=vtc_url or config.get("vtc_url"),
        stage="media_probe",
        reason="media_ready",
    )
    append_action_log(
        config,
        "media_ready",
        {"vtc_url": vtc_url or config.get("vtc_url"), "service": get_service_name(config)},
    )


def get_connection_status():
    with connection_status_lock:
        return dict(connection_status)


# XMLRPC
def stop_video(video_pid):
    global video_process

    print("Stopping video")
    video_supervisor_stop.set()
    with video_lock:
        process = video_process
        video_process = None

    target_pid = process.pid if process is not None else video_pid
    details = {"video_pid": target_pid, "success": bool(target_pid)}
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    elif target_pid:
        try:
            os.kill(target_pid, signal.SIGTERM)
        except ProcessLookupError:
            details["already_stopped"] = True
        except Exception as exc:
            details.update({"success": False, "error": str(exc)})
            emit_event(config, "camera_off", details)
            append_action_log(config, "camera_off", details)
            return False
    emit_event(config, "camera_off", details)
    append_action_log(config, "camera_off", details)
    return details["success"]


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
