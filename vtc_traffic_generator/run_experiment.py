#!/usr/bin/env python3
"""Generate VTC experiment configs and optionally deploy/run them with Ansible."""

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

try:
    from vtc_automation.event_log import resolve_git_sha
except ImportError:
    from vtc_traffic_generator.vtc_automation.event_log import resolve_git_sha


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "generated" / "experiment"
DEFAULT_PLAYBOOK = PROJECT_ROOT / "ansible" / "deploy_experiment.yml"
DEFAULT_STOP_PLAYBOOK = PROJECT_ROOT / "ansible" / "stop_clients.yml"
DEFAULT_UPLOAD_PLAYBOOK = PROJECT_ROOT / "ansible" / "upload_captures.yml"


def load_json(path):
    with Path(path).expanduser().open("r", encoding="utf-8") as infile:
        return json.load(infile)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as outfile:
        json.dump(data, outfile, indent=2, sort_keys=True)
        outfile.write("\n")


def require(value, name):
    if value in (None, ""):
        raise ValueError(f"Missing required experiment field: {name}")
    return value


def normalize_clients(experiment):
    clients = experiment.get("clients")
    if not isinstance(clients, list) or not clients:
        raise ValueError("experiment.clients must be a non-empty list")

    normalized = []
    base_port = int(experiment.get("base_c2_port", 8001))
    base_display = int(str(experiment.get("base_display", "99")).lstrip(":"))
    base_video_device = int(experiment.get("base_video_device", 5))

    for index, client in enumerate(clients):
        if not isinstance(client, dict):
            raise ValueError(f"clients[{index}] must be an object")

        bot_number = index + 1
        name = str(client.get("name") or client.get("bot_name") or f"bot{bot_number}")
        host = require(client.get("host") or client.get("ansible_host"), f"clients[{index}].host")
        c2_host = str(client.get("c2_host") or client.get("private_ip") or host)
        c2_port = int(client.get("c2_port", base_port))
        display = str(client.get("display", f":{base_display + index}"))
        video_device = str(client.get("video_device", f"/dev/video{base_video_device}"))
        sink_name = str(client.get("sink_name", f"{name}_sink"))
        source_name = str(client.get("source_name", f"{sink_name}.monitor"))

        normalized.append(
            {
                **client,
                "name": name,
                "host": str(host),
                "c2_host": c2_host,
                "c2_port": c2_port,
                "display": display,
                "video_device": video_device,
                "sink_name": sink_name,
                "source_name": source_name,
            }
        )

    return normalized


def build_controller_config(experiment, clients):
    service = require(experiment.get("service") or experiment.get("vtc_platform"), "service")
    controller = {
        "role": "controller",
        "vtc_platform": service,
        "experiment_id": experiment.get("experiment_id"),
        "execution_id": experiment.get("execution_id"),
        "git_sha": experiment.get("git_sha"),
        "config_sha256": experiment.get("config_sha256"),
        "duration": experiment.get("duration", 1),
        "videoconference": bool(experiment.get("videoconference", True)),
        "vtc_clients": [[client["c2_host"], client["c2_port"]] for client in clients],
        "version": experiment.get("version", "VTC traffic generator X"),
    }

    controller.setdefault(
        "action_log_path",
        str(experiment.get("action_log_path") or f"/tmp/vtc-controller/actions-{experiment_run_log_id(experiment)}.txt"),
    )

    for optional_key in ("behavior", "behavior_mode", "adapter_action_timeout_sec"):
        if optional_key in experiment:
            controller[optional_key] = experiment[optional_key]

    return controller


def build_remote_config(experiment, client):
    service = require(experiment.get("service") or experiment.get("vtc_platform"), "service")
    defaults = experiment.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("experiment.defaults must be an object when provided")

    adapter_defaults = defaults.get("adapter_config", {})
    if not isinstance(adapter_defaults, dict):
        raise ValueError("experiment.defaults.adapter_config must be an object when provided")

    client_adapter = client.get("adapter_config", {})
    if not isinstance(client_adapter, dict):
        raise ValueError(f"{client['name']}.adapter_config must be an object when provided")

    bot_name = client["name"]
    run_log_id = experiment_run_log_id(experiment)
    virtual_audio = {
        "sink_name": client["sink_name"],
        "source_name": client["source_name"],
        "sink_description": str(client.get("sink_description", client["sink_name"])),
        "source_description": str(client.get("source_description", client["source_name"])),
    }
    virtual_video_defaults = defaults.get("virtual_video", {})
    if not isinstance(virtual_video_defaults, dict):
        virtual_video_defaults = {}
    client_virtual_video = client.get("virtual_video", {})
    if not isinstance(client_virtual_video, dict):
        client_virtual_video = {}
    virtual_video = {
        **virtual_video_defaults,
        **client_virtual_video,
        "device": client["video_device"],
    }
    adapter_config = {
        **adapter_defaults,
        **client_adapter,
        "display": client["display"],
        "display_name": str(client.get("display_name", bot_name)),
        "microphone_name": str(client.get("microphone_name", client["source_name"])),
        "event_log_path": str(client.get("event_log_path", f"/tmp/vtc-{bot_name}/events-{run_log_id}.jsonl")),
        "app_log_path": str(client.get("app_log_path", f"/tmp/vtc-{bot_name}/jitsi-electron-{run_log_id}.log")),
        "adapter_log_path": str(client.get("adapter_log_path", f"/tmp/vtc-{bot_name}/adapter-{run_log_id}.log")),
        "diagnostic_dir": str(client.get("diagnostic_dir", f"/tmp/vtc-{bot_name}/diagnostics/{run_log_id}")),
        "restart_existing": bool(client.get("restart_existing", False)),
    }
    screen_share_window = resolve_screen_share_window(experiment, client, defaults)
    if screen_share_window.get("enabled"):
        adapter_config.setdefault(
            "screen_share_target",
            str(screen_share_window.get("title") or f"VTC Share Window - {bot_name}"),
        )

    executable_path = client.get("executable_path") or defaults.get("executable_path")
    if executable_path:
        adapter_config.setdefault("executable_path", str(executable_path))

    packet_capture = merge_mapping(defaults.get("packet_capture"), client.get("packet_capture"))
    if packet_capture:
        packet_capture.setdefault("client_ip", client["c2_host"])
        parsed_vtc_url = urlparse(str(experiment.get("vtc_url") or experiment.get("room_url") or ""))
        if parsed_vtc_url.hostname:
            packet_capture.setdefault("jvb_ip", parsed_vtc_url.hostname)
        packet_capture.setdefault("jvb_port", 10000)

    remote = {
        "role": "client",
        "vtc_platform": service,
        "service": service,
        "experiment_id": experiment.get("experiment_id"),
        "execution_id": experiment.get("execution_id"),
        "git_sha": experiment.get("git_sha"),
        "config_sha256": experiment.get("config_sha256"),
        "vtc_url": require(experiment.get("vtc_url") or experiment.get("room_url"), "vtc_url"),
        "c2_port": client["c2_port"],
        "bot_name": bot_name,
        "bot": {
            "display_name": str(client.get("display_name", bot_name)),
        },
        "action_log_path": str(
            client.get("action_log_path", f"/tmp/vtc-{bot_name}/actions-{run_log_id}.txt")
        ),
        "videoconference": bool(experiment.get("videoconference", True)),
        "audio_path": str(client.get("audio_path", defaults.get("audio_path", "VTC_AV/VTC_audio_tracks"))),
        "voice_name": str(client.get("voice_name", defaults.get("voice_name", ""))),
        "video_path": str(client.get("video_path", defaults.get("video_path", "VTC_AV/270_resized"))),
        "video_name": str(client.get("video_name", defaults.get("video_name", ""))),
        "virtual_audio": virtual_audio,
        "virtual_video": virtual_video,
        "packet_capture": packet_capture or {"enabled": False},
        "adapter_config": adapter_config,
        "version": experiment.get("version", "VTC traffic generator X"),
    }

    for optional_key in (
        "icsi_audio_root",
        "icsi_audio_meeting_dir",
        "icsi",
        "adapter_action_timeout_sec",
        "audio_loopback_probe",
        "artifact_dir",
    ):
        if optional_key in client:
            remote[optional_key] = client[optional_key]
        elif optional_key in defaults:
            remote[optional_key] = defaults[optional_key]

    missing_media_fields = [
        field
        for field in ("voice_name", "video_name")
        if not remote[field] and remote["videoconference"]
    ]
    if missing_media_fields:
        raise ValueError(
            f"{bot_name} is missing required media field(s): {', '.join(missing_media_fields)}"
        )

    return remote


def render_inventory(experiment, clients, output_dir):
    ansible_config = experiment.get("ansible", {})
    if not isinstance(ansible_config, dict):
        raise ValueError("experiment.ansible must be an object when provided")

    repo = experiment.get("repo", {})
    if not isinstance(repo, dict):
        raise ValueError("experiment.repo must be an object when provided")

    user = ansible_config.get("user")
    ssh_key = ansible_config.get("ssh_private_key_file")
    repo_dir = repo.get("dir", "/home/ubuntu/video-teleconference")
    repo_url = repo.get("url")
    repo_version = repo.get("version", experiment.get("branch", "main"))
    python_bin = repo.get("python", "python3")
    defaults = experiment.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError("experiment.defaults must be an object when provided")
    capture_upload = experiment.get("capture_upload", {})
    if capture_upload is None:
        capture_upload = {}
    if not isinstance(capture_upload, dict):
        raise ValueError("experiment.capture_upload must be an object when provided")

    group_vars = [
        "[vtc_clients:vars]",
        f"repo_dir={quote_inventory_value(repo_dir)}",
        f"repo_version={quote_inventory_value(repo_version)}",
        f"python_bin={quote_inventory_value(python_bin)}",
    ]
    if repo_url:
        group_vars.append(f"repo_url={quote_inventory_value(repo_url)}")
    append_capture_upload_inventory_vars(group_vars, experiment, capture_upload)

    inventory_sections = []
    icsi_data = experiment.get("icsi_data")
    if icsi_data is not None:
        if not isinstance(icsi_data, dict):
            raise ValueError("experiment.icsi_data must be an object when provided")
        inventory_sections.append(render_controller_inventory(icsi_data))

    jitsi_server = experiment.get("jitsi_server")
    if jitsi_server is not None:
        if not isinstance(jitsi_server, dict):
            raise ValueError("experiment.jitsi_server must be an object when provided")
        inventory_sections.append(render_jitsi_server_inventory(jitsi_server, ansible_config))

    host_lines = ["[vtc_clients]"]
    for client in clients:
        config_path = output_dir / f"remote_config_{client['name']}.json"
        remote_config_path = f"{repo_dir}/generated/{client['name']}_remote_config.json"
        client_log_path = f"/tmp/vtc-{client['name']}/client.log"
        video_nr = str(client["video_device"]).removeprefix("/dev/video")
        launcher_path = client.get("executable_path") or defaults.get("executable_path")
        packet_capture = client.get("packet_capture", defaults.get("packet_capture", {}))
        if not isinstance(packet_capture, dict):
            packet_capture = {}
        capture_output_dir = str(packet_capture.get("output_dir") or "/tmp/vtc-captures")
        run_log_id = experiment_run_log_id(experiment)
        action_log_path = str(client.get("action_log_path", f"/tmp/vtc-{client['name']}/actions-{run_log_id}.txt"))
        event_log_path = str(client.get("event_log_path", f"/tmp/vtc-{client['name']}/events-{run_log_id}.jsonl"))
        app_log_path = str(client.get("app_log_path", f"/tmp/vtc-{client['name']}/jitsi-electron-{run_log_id}.log"))
        adapter_log_path = str(client.get("adapter_log_path", f"/tmp/vtc-{client['name']}/adapter-{run_log_id}.log"))
        screen_share_window = resolve_screen_share_window(experiment, client, defaults)
        screen_share_window_enabled = bool(screen_share_window.get("enabled", False))
        screen_share_window_title = str(screen_share_window.get("title") or f"VTC Share Window - {client['name']}")
        screen_share_window_html_path = str(
            screen_share_window.get("html_path")
            or f"/home/ubuntu/vtc_data/screen_share/{client['name']}.html"
        )
        screen_share_window_url = str(
            screen_share_window.get("url")
            or f"file://{screen_share_window_html_path}"
        )
        screen_share_nonce = str(screen_share_window.get("nonce", ""))
        screen_share_run_id = str(screen_share_window.get("run_id", experiment.get("run_id", "")))
        screen_share_marker_color = str(screen_share_window.get("marker_color", "#111111"))
        parts = [
            client["name"],
            f"ansible_host={quote_inventory_value(client['host'])}",
            f"c2_port={quote_inventory_value(client['c2_port'])}",
            f"vtc_display={quote_inventory_value(client['display'])}",
            f"video_device={quote_inventory_value(client['video_device'])}",
            f"video_nr={quote_inventory_value(video_nr)}",
            f"audio_sink_name={quote_inventory_value(client['sink_name'])}",
            f"audio_source_name={quote_inventory_value(client['source_name'])}",
            f"audio_sink_description={quote_inventory_value(client.get('sink_description', client['sink_name']))}",
            f"audio_source_description={quote_inventory_value(client.get('source_description', client['source_name']))}",
            f"bot_config_src={quote_inventory_value(str(config_path))}",
            f"remote_config_path={quote_inventory_value(remote_config_path)}",
            f"client_log_path={quote_inventory_value(client_log_path)}",
            f"capture_output_dir={quote_inventory_value(capture_output_dir)}",
            f"action_log_path={quote_inventory_value(action_log_path)}",
            f"event_log_path={quote_inventory_value(event_log_path)}",
            f"app_log_path={quote_inventory_value(app_log_path)}",
            f"adapter_log_path={quote_inventory_value(adapter_log_path)}",
            f"screen_share_window_enabled={quote_inventory_value(str(screen_share_window_enabled).lower())}",
            f"screen_share_window_title={quote_inventory_value(screen_share_window_title)}",
            f"screen_share_window_html_path={quote_inventory_value(screen_share_window_html_path)}",
            f"screen_share_window_url={quote_inventory_value(screen_share_window_url)}",
            f"screen_share_nonce={quote_inventory_value(screen_share_nonce)}",
            f"screen_share_run_id={quote_inventory_value(screen_share_run_id)}",
            f"screen_share_marker_color={quote_inventory_value(screen_share_marker_color)}",
        ]
        if launcher_path:
            parts.append(f"jitsi_electron_launcher={quote_inventory_value(launcher_path)}")
        if user:
            parts.append(f"ansible_user={quote_inventory_value(user)}")
        if ssh_key:
            parts.append(f"ansible_ssh_private_key_file={quote_inventory_value(str(Path(ssh_key).expanduser()))}")
        host_lines.append(" ".join(parts))

    if icsi_data is not None:
        append_icsi_inventory_vars(group_vars, icsi_data)

    inventory_sections.append("\n".join(host_lines + [""] + group_vars + [""]))
    return "\n".join(section for section in inventory_sections if section.strip())


def render_controller_inventory(icsi_data):
    lines = [
        "[localhost]",
        "localhost ansible_connection=local",
        "",
        "[localhost:vars]",
    ]
    append_icsi_inventory_vars(lines, icsi_data)
    lines.append("")
    return "\n".join(lines)


def render_jitsi_server_inventory(jitsi_server, ansible_config):
    user = jitsi_server.get("user") or ansible_config.get("user")
    ssh_key = jitsi_server.get("ssh_private_key_file") or ansible_config.get("ssh_private_key_file")
    host = require(jitsi_server.get("host") or jitsi_server.get("ansible_host"), "jitsi_server.host")
    name = str(jitsi_server.get("name") or "jitsi-server")

    parts = [
        name,
        f"ansible_host={quote_inventory_value(host)}",
    ]
    if user:
        parts.append(f"ansible_user={quote_inventory_value(user)}")
    if ssh_key:
        parts.append(f"ansible_ssh_private_key_file={quote_inventory_value(str(Path(ssh_key).expanduser()))}")

    server_dir = jitsi_server.get("dir", "/home/ubuntu/docker-jitsi-meet")
    start_command = jitsi_server.get("start_command", "docker compose up -d")
    wait_host = jitsi_server.get("wait_host", "127.0.0.1")
    wait_port = int(jitsi_server.get("wait_port", 443))
    update_repo = bool(jitsi_server.get("update_repo", False))

    vars_lines = [
        "[jitsi_servers:vars]",
        f"jitsi_server_dir={quote_inventory_value(server_dir)}",
        f"jitsi_server_start_command={quote_inventory_value(start_command)}",
        f"jitsi_server_wait_host={quote_inventory_value(wait_host)}",
        f"jitsi_server_wait_port={quote_inventory_value(wait_port)}",
        f"jitsi_server_update_repo={quote_inventory_value(str(update_repo).lower())}",
    ]

    return "\n".join(["[jitsi_servers]", " ".join(parts), "", *vars_lines, ""])


def append_icsi_inventory_vars(lines, icsi_data):
    s3_uri = icsi_data.get("s3_uri")
    audio_s3_uri = icsi_data.get("audio_s3_uri")
    local_root = icsi_data.get("local_root")
    meeting_id = icsi_data.get("meeting_id")
    if s3_uri:
        lines.append(f"icsi_s3_uri={quote_inventory_value(s3_uri)}")
    if audio_s3_uri:
        lines.append(f"icsi_audio_s3_uri={quote_inventory_value(audio_s3_uri)}")
    if local_root:
        lines.append(f"icsi_local_root={quote_inventory_value(local_root)}")
    if meeting_id:
        lines.append(f"icsi_meeting_id={quote_inventory_value(meeting_id)}")


def append_capture_upload_inventory_vars(lines, experiment, capture_upload):
    s3_uri = capture_upload.get("s3_uri")
    if s3_uri:
        lines.append(f"capture_upload_s3_uri={quote_inventory_value(str(s3_uri).rstrip('/'))}")

    run_id = capture_upload.get("run_id") or experiment.get("run_id")
    if run_id:
        lines.append(f"capture_upload_run_id={quote_inventory_value(run_id)}")
        lines.append(f"capture_upload_execution_id={quote_inventory_value(str(experiment.get('execution_id') or run_id))}")

    include_logs = bool(capture_upload.get("include_logs", True))
    lines.append(f"capture_upload_include_logs={quote_inventory_value(str(include_logs).lower())}")


def experiment_run_log_id(experiment):
    value = experiment.get("_run_log_id")
    if value:
        return str(value)

    if experiment.get("execution_id"):
        value = sanitize_log_id(str(experiment["execution_id"]))
        experiment["_run_log_id"] = value
        return value

    base = (
        experiment.get("run_id")
        or experiment.get("capture_upload", {}).get("run_id")
        or "vtc-experiment"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    value = sanitize_log_id(f"{base}-{timestamp}")
    experiment["_run_log_id"] = value
    return value


def sanitize_log_id(value):
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return text or "vtc-experiment"


def capture_upload_configured(experiment):
    capture_upload = experiment.get("capture_upload", {})
    return isinstance(capture_upload, dict) and bool(capture_upload.get("s3_uri"))


def prepare_experiment_metadata(experiment, experiment_path):
    experiment_id = str(
        experiment.get("experiment_id")
        or experiment.get("run_id")
        or Path(experiment_path).expanduser().stem
    )
    execution_id = str(
        experiment.get("execution_id")
        or f"{sanitize_log_id(experiment_id)}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
    )
    experiment["experiment_id"] = experiment_id
    experiment["execution_id"] = execution_id
    experiment["git_sha"] = experiment.get("git_sha") or resolve_git_sha(PROJECT_ROOT)
    experiment["config_sha256"] = stable_config_sha256(experiment)

    capture_upload = experiment.get("capture_upload")
    if isinstance(capture_upload, dict) and capture_upload.get("s3_uri"):
        capture_upload["run_id"] = execution_id


def stable_config_sha256(experiment):
    normalized = normalize_for_config_hash(experiment)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_for_config_hash(value, parent_key=""):
    if isinstance(value, dict):
        normalized = {}
        for key, item in sorted(value.items()):
            if str(key).startswith("_"):
                continue
            if key in {"execution_id", "git_sha", "config_sha256"}:
                continue
            if parent_key == "capture_upload" and key == "run_id":
                continue
            normalized[key] = normalize_for_config_hash(item, str(key))
        return normalized
    if isinstance(value, list):
        return [normalize_for_config_hash(item, parent_key) for item in value]
    return value


def merge_mapping(base, override):
    merged = {}
    if isinstance(base, dict):
        merged.update(base)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def resolve_screen_share_window(experiment, client, defaults):
    screen_share_window = merge_mapping(defaults.get("screen_share_window"), client.get("screen_share_window"))
    if not screen_share_window.get("enabled"):
        return screen_share_window
    if screen_share_window.get("unique_per_run", True) is False:
        return screen_share_window

    windows = experiment.setdefault("_screen_share_windows", {})
    bot_name = client["name"]
    if bot_name not in windows:
        run_log_id = experiment_run_log_id(experiment)
        nonce = secrets.token_hex(16)
        marker_color = "#" + secrets.token_hex(3)
        title = f"VTC Share Window {run_log_id} {bot_name} {nonce[:12]}"
        default_html_path = f"/home/ubuntu/vtc_data/screen_share/{run_log_id}-{bot_name}.html"
        windows[bot_name] = {
            "title": title,
            "html_path": str(screen_share_window.get("html_path") or default_html_path),
            "url": screen_share_window.get("url") or f"file://{screen_share_window.get('html_path') or default_html_path}",
            "nonce": nonce,
            "run_id": str(experiment.get("run_id") or run_log_id),
            "marker_color": marker_color,
        }
    return {**screen_share_window, **windows[bot_name]}


def quote_inventory_value(value):
    text = str(value)
    if not text:
        return '""'
    if any(char.isspace() for char in text) or "#" in text:
        return json.dumps(text)
    return text


def generate(experiment_path, output_dir):
    experiment = load_json(experiment_path)
    prepare_experiment_metadata(experiment, experiment_path)
    experiment_run_log_id(experiment)
    clients = normalize_clients(experiment)
    output_dir = Path(output_dir).expanduser().resolve()

    controller_config = build_controller_config(experiment, clients)
    controller_config_path = output_dir / "controller_config.json"
    write_json(controller_config_path, controller_config)

    remote_paths = []
    for client in clients:
        remote_config = build_remote_config(experiment, client)
        remote_path = output_dir / f"remote_config_{client['name']}.json"
        write_json(remote_path, remote_config)
        remote_paths.append(remote_path)

    inventory_path = output_dir / "inventory.ini"
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(render_inventory(experiment, clients, output_dir), encoding="utf-8")
    manifest_path = output_dir / f"run-manifest-{experiment['execution_id']}.json"
    write_json(
        manifest_path,
        {
            "experiment": str(Path(experiment_path).expanduser().resolve()),
            "experiment_id": experiment["experiment_id"],
            "execution_id": experiment["execution_id"],
            "git_sha": experiment.get("git_sha"),
            "config_sha256": experiment.get("config_sha256"),
            "duration_minutes": experiment.get("duration"),
            "validity": {
                "intended_duration_sec": float(experiment.get("duration", 0)) * 60,
                "minimum_media_ready_duration_sec": None,
                "media_ready_required": True,
                "packet_capture_required": True,
                "s3_upload_run_id": experiment.get("capture_upload", {}).get("run_id")
                if isinstance(experiment.get("capture_upload"), dict)
                else None,
            },
            "clients": [
                {
                    "name": client["name"],
                    "host": client["host"],
                    "c2_host": client["c2_host"],
                    "display": client["display"],
                    "video_device": client["video_device"],
                }
                for client in clients
            ],
        },
    )

    return {
        "experiment": Path(experiment_path).expanduser().resolve(),
        "output_dir": output_dir,
        "controller_config": controller_config_path,
        "inventory": inventory_path,
        "remote_configs": remote_paths,
        "run_manifest": manifest_path,
        "capture_upload_configured": capture_upload_configured(experiment),
        "capture_upload_s3_uri": str(experiment.get("capture_upload", {}).get("s3_uri") or "").rstrip("/"),
        "capture_upload_run_id": str(experiment.get("capture_upload", {}).get("run_id") or ""),
        "controller_action_log_path": controller_config.get("action_log_path"),
    }


def run_ansible(inventory_path, playbook_path):
    command = ["ansible-playbook", "-i", str(inventory_path), str(playbook_path)]
    try:
        return subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
    except FileNotFoundError:
        print("Error: ansible-playbook is not installed or not on PATH.", file=sys.stderr)
        return subprocess.CompletedProcess(command, 127)


def run_local_controller(controller_config_path):
    command = [
        sys.executable,
        str(PROJECT_ROOT / "vtc_traffic_generator" / "vtc_generator.py"),
        str(controller_config_path),
    ]
    return subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)


def upload_controller_artifacts(generated):
    s3_uri = generated.get("capture_upload_s3_uri")
    run_id = generated.get("capture_upload_run_id")
    if not s3_uri or not run_id:
        return subprocess.CompletedProcess(["aws", "s3", "sync"], 0)

    with tempfile.TemporaryDirectory(prefix="vtc-controller-upload-") as tmpdir:
        staging_dir = Path(tmpdir)
        artifact_paths = [
            generated.get("run_manifest"),
            generated.get("controller_config"),
            *generated.get("remote_configs", []),
            generated.get("controller_action_log_path"),
        ]

        staged_count = 0
        for artifact_path in artifact_paths:
            if not artifact_path:
                continue
            path = Path(artifact_path).expanduser()
            if not path.is_file():
                continue
            shutil.copy2(path, staging_dir / path.name)
            staged_count += 1

        if staged_count == 0:
            return subprocess.CompletedProcess(["aws", "s3", "sync"], 0)

        destination = f"{s3_uri}/experiments/{run_id}/controller/"
        command = ["aws", "s3", "sync", str(staging_dir), destination]
        try:
            return subprocess.run(command, cwd=str(PROJECT_ROOT), check=False)
        except FileNotFoundError:
            print("Error: aws CLI is not installed or not on PATH.", file=sys.stderr)
            return subprocess.CompletedProcess(command, 127)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", help="Path to experiment JSON")
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Directory for generated configs and inventory. Default: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Run ansible-playbook after generating configs.",
    )
    parser.add_argument(
        "--playbook",
        default=str(DEFAULT_PLAYBOOK),
        help=f"Ansible playbook to run with --deploy. Default: {DEFAULT_PLAYBOOK}",
    )
    parser.add_argument(
        "--run-controller",
        action="store_true",
        help="Run the controller locally after optional Ansible deployment.",
    )
    parser.add_argument(
        "--upload-captures",
        action="store_true",
        help="Upload client packet captures to S3. This is automatic after --run-controller when capture_upload.s3_uri is set.",
    )
    parser.add_argument(
        "--no-upload-captures",
        action="store_true",
        help="Do not automatically upload captures after --run-controller.",
    )
    parser.add_argument(
        "--upload-playbook",
        default=str(DEFAULT_UPLOAD_PLAYBOOK),
        help=f"Ansible playbook to run with --upload-captures. Default: {DEFAULT_UPLOAD_PLAYBOOK}",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Do not stop client GUI, media, capture, and VNC processes after --run-controller.",
    )
    parser.add_argument(
        "--stop-playbook",
        default=str(DEFAULT_STOP_PLAYBOOK),
        help=f"Ansible playbook to run for automatic cleanup. Default: {DEFAULT_STOP_PLAYBOOK}",
    )
    args = parser.parse_args()

    try:
        generated = generate(args.experiment, args.output_dir)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Generated controller config: {generated['controller_config']}")
    print(f"Generated Ansible inventory: {generated['inventory']}")
    print(f"Generated run manifest: {generated['run_manifest']}")
    for remote_config in generated["remote_configs"]:
        print(f"Generated client config: {remote_config}")

    if args.deploy:
        playbook_path = Path(args.playbook).expanduser().resolve()
        ansible_result = run_ansible(generated["inventory"], playbook_path)
        if ansible_result.returncode != 0:
            return ansible_result.returncode

    controller_returncode = 0
    if args.run_controller:
        controller_result = run_local_controller(generated["controller_config"])
        controller_returncode = controller_result.returncode

    cleanup_returncode = 0
    if args.run_controller and not args.no_cleanup:
        stop_playbook_path = Path(args.stop_playbook).expanduser().resolve()
        cleanup_result = run_ansible(generated["inventory"], stop_playbook_path)
        cleanup_returncode = cleanup_result.returncode

    should_upload_captures = args.upload_captures or (
        args.run_controller
        and not args.no_upload_captures
        and generated["capture_upload_configured"]
    )
    if should_upload_captures:
        upload_playbook_path = Path(args.upload_playbook).expanduser().resolve()
        upload_result = run_ansible(generated["inventory"], upload_playbook_path)
        if upload_result.returncode != 0:
            return upload_result.returncode
        controller_upload_result = upload_controller_artifacts(generated)
        if controller_upload_result.returncode != 0:
            return controller_upload_result.returncode

    if cleanup_returncode != 0:
        return cleanup_returncode

    if controller_returncode != 0:
        return controller_returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
