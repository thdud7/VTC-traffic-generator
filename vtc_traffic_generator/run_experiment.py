#!/usr/bin/env python3
"""Generate VTC experiment configs and optionally deploy/run them with Ansible."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "generated" / "experiment"
DEFAULT_PLAYBOOK = PROJECT_ROOT / "ansible" / "deploy_clients.yml"


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
        "duration": experiment.get("duration", 1),
        "videoconference": bool(experiment.get("videoconference", True)),
        "vtc_clients": [[client["host"], client["c2_port"]] for client in clients],
        "version": experiment.get("version", "VTC traffic generator X"),
    }

    for optional_key in ("behavior", "behavior_mode", "adapter_action_timeout_sec", "action_log_path"):
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
    virtual_audio = {
        "sink_name": client["sink_name"],
        "source_name": client["source_name"],
        "sink_description": str(client.get("sink_description", client["sink_name"])),
        "source_description": str(client.get("source_description", client["source_name"])),
    }
    virtual_video = {
        "device": client["video_device"],
    }
    adapter_config = {
        **adapter_defaults,
        **client_adapter,
        "display": client["display"],
        "display_name": str(client.get("display_name", bot_name)),
        "microphone_name": str(client.get("microphone_name", client["source_name"])),
        "event_log_path": str(client.get("event_log_path", f"/tmp/vtc-{bot_name}/events.jsonl")),
        "app_log_path": str(client.get("app_log_path", f"/tmp/vtc-{bot_name}/jitsi-electron.log")),
        "adapter_log_path": str(client.get("adapter_log_path", f"/tmp/vtc-{bot_name}/adapter.log")),
        "restart_existing": bool(client.get("restart_existing", False)),
    }

    executable_path = client.get("executable_path") or defaults.get("executable_path")
    if executable_path:
        adapter_config.setdefault("executable_path", str(executable_path))

    remote = {
        "role": "client",
        "vtc_platform": service,
        "service": service,
        "vtc_url": require(experiment.get("vtc_url") or experiment.get("room_url"), "vtc_url"),
        "c2_port": client["c2_port"],
        "bot_name": bot_name,
        "bot": {
            "display_name": str(client.get("display_name", bot_name)),
        },
        "action_log_path": str(client.get("action_log_path", f"/tmp/vtc-{bot_name}/actions.txt")),
        "videoconference": bool(experiment.get("videoconference", True)),
        "audio_path": str(client.get("audio_path", defaults.get("audio_path", "VTC_AV/VTC_audio_tracks"))),
        "voice_name": str(client.get("voice_name", defaults.get("voice_name", ""))),
        "video_path": str(client.get("video_path", defaults.get("video_path", "VTC_AV/270_resized"))),
        "video_name": str(client.get("video_name", defaults.get("video_name", ""))),
        "virtual_audio": virtual_audio,
        "virtual_video": virtual_video,
        "packet_capture": client.get("packet_capture", defaults.get("packet_capture", {"enabled": False})),
        "adapter_config": adapter_config,
        "version": experiment.get("version", "VTC traffic generator X"),
    }

    for optional_key in ("icsi_audio_root", "icsi_audio_meeting_dir", "icsi", "adapter_action_timeout_sec"):
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

    group_vars = [
        "[vtc_clients:vars]",
        f"repo_dir={quote_inventory_value(repo_dir)}",
        f"repo_version={quote_inventory_value(repo_version)}",
        f"python_bin={quote_inventory_value(python_bin)}",
    ]
    if repo_url:
        group_vars.append(f"repo_url={quote_inventory_value(repo_url)}")

    host_lines = ["[vtc_clients]"]
    for client in clients:
        config_path = output_dir / f"remote_config_{client['name']}.json"
        remote_config_path = f"{repo_dir}/generated/{client['name']}_remote_config.json"
        client_log_path = f"/tmp/vtc-{client['name']}/client.log"
        parts = [
            client["name"],
            f"ansible_host={quote_inventory_value(client['host'])}",
            f"c2_port={quote_inventory_value(client['c2_port'])}",
            f"bot_config_src={quote_inventory_value(str(config_path))}",
            f"remote_config_path={quote_inventory_value(remote_config_path)}",
            f"client_log_path={quote_inventory_value(client_log_path)}",
        ]
        if user:
            parts.append(f"ansible_user={quote_inventory_value(user)}")
        if ssh_key:
            parts.append(f"ansible_ssh_private_key_file={quote_inventory_value(str(Path(ssh_key).expanduser()))}")
        host_lines.append(" ".join(parts))

    return "\n".join(host_lines + [""] + group_vars + [""])


def quote_inventory_value(value):
    text = str(value)
    if not text:
        return '""'
    if any(char.isspace() for char in text):
        return json.dumps(text)
    return text


def generate(experiment_path, output_dir):
    experiment = load_json(experiment_path)
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

    return {
        "experiment": Path(experiment_path).expanduser().resolve(),
        "output_dir": output_dir,
        "controller_config": controller_config_path,
        "inventory": inventory_path,
        "remote_configs": remote_paths,
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
    args = parser.parse_args()

    try:
        generated = generate(args.experiment, args.output_dir)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print(f"Generated controller config: {generated['controller_config']}")
    print(f"Generated Ansible inventory: {generated['inventory']}")
    for remote_config in generated["remote_configs"]:
        print(f"Generated client config: {remote_config}")

    if args.deploy:
        playbook_path = Path(args.playbook).expanduser().resolve()
        ansible_result = run_ansible(generated["inventory"], playbook_path)
        if ansible_result.returncode != 0:
            return ansible_result.returncode

    if args.run_controller:
        controller_result = run_local_controller(generated["controller_config"])
        return controller_result.returncode

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
