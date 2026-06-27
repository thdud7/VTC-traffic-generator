#!/usr/bin/env python3
"""Refresh EC2 host addresses and optionally stop/start unreachable VTC hosts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run(command: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    print(json.dumps({"timestamp_utc": utc_now(), "command": command}), file=sys.stderr)
    return subprocess.run(command, check=check, capture_output=True, text=True)


def parse_inventory(path: Path) -> dict[str, dict[str, str]]:
    hosts: dict[str, dict[str, str]] = {}
    current_group = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1]
            continue
        if current_group != "vtc_clients" and current_group != "controllers":
            continue
        parts = line.split()
        host = parts[0]
        values: dict[str, str] = {}
        for part in parts[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                values[key] = value.strip('"')
        hosts[host] = values
    return hosts


def describe_instance(hostvars: dict[str, str], region: str) -> dict[str, str] | None:
    filters = []
    if hostvars.get("instance_id"):
        command = ["aws", "ec2", "describe-instances", "--region", region, "--instance-ids", hostvars["instance_id"]]
    else:
        for key, aws_name in (
            ("public_dns", "dns-name"),
            ("public_ip", "ip-address"),
            ("private_ip", "private-ip-address"),
        ):
            if hostvars.get(key):
                filters = ["Name=" + aws_name + ",Values=" + hostvars[key]]
                break
        if not filters:
            return None
        command = ["aws", "ec2", "describe-instances", "--region", region, "--filters", *filters]
    result = run(command)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    data = json.loads(result.stdout or "{}")
    for reservation in data.get("Reservations", []):
        for instance in reservation.get("Instances", []):
            return {
                "instance_id": instance.get("InstanceId", ""),
                "public_dns": instance.get("PublicDnsName", ""),
                "public_ip": instance.get("PublicIpAddress", ""),
                "private_ip": instance.get("PrivateIpAddress", ""),
                "state": instance.get("State", {}).get("Name", ""),
            }
    return None


def stop_start(instance_id: str, region: str) -> None:
    if os.environ.get("ALLOW_EC2_STOP_START") != "true":
        raise RuntimeError("Refusing stop/start without ALLOW_EC2_STOP_START=true")
    run(["aws", "ec2", "stop-instances", "--region", region, "--instance-ids", instance_id], check=True)
    run(["aws", "ec2", "wait", "instance-stopped", "--region", region, "--instance-ids", instance_id], check=True)
    run(["aws", "ec2", "start-instances", "--region", region, "--instance-ids", instance_id], check=True)
    run(["aws", "ec2", "wait", "instance-running", "--region", region, "--instance-ids", instance_id], check=True)
    run(["aws", "ec2", "wait", "instance-status-ok", "--region", region, "--instance-ids", instance_id], check=True)


def ssh_ping(hostvars: dict[str, str], user: str | None, key: str | None) -> bool:
    target = hostvars.get("ansible_host") or hostvars.get("public_dns") or hostvars.get("public_ip")
    if not target:
        return False
    ssh_target = f"{user}@{target}" if user else target
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if key:
        command.extend(["-i", str(Path(key).expanduser())])
    command.extend([ssh_target, "true"])
    result = run(command)
    return result.returncode == 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--user", default=os.environ.get("VTC_ANSIBLE_USER"))
    parser.add_argument("--ssh-key", default=os.environ.get("VTC_SSH_PRIVATE_KEY"))
    parser.add_argument("--output", type=Path, default=Path("generated/experiment/inventory.ec2-cache.json"))
    parser.add_argument("--stop-start", action="store_true")
    args = parser.parse_args()

    hosts = parse_inventory(args.inventory)
    refreshed = {}
    for host, hostvars in sorted(hosts.items()):
        reachable = ssh_ping(hostvars, args.user, args.ssh_key)
        info = describe_instance(hostvars, hostvars.get("region", args.region)) or {}
        if not reachable and args.stop_start and info.get("instance_id"):
            stop_start(info["instance_id"], hostvars.get("region", args.region))
            info = describe_instance({"instance_id": info["instance_id"]}, hostvars.get("region", args.region)) or info
            refreshed_hostvars = {**hostvars, **info, "ansible_host": info.get("public_dns") or info.get("public_ip") or hostvars.get("ansible_host", "")}
            reachable = ssh_ping(refreshed_hostvars, args.user, args.ssh_key)
        else:
            refreshed_hostvars = {**hostvars, **info}
        if refreshed_hostvars.get("private_ip"):
            refreshed_hostvars["rpc_host"] = refreshed_hostvars["private_ip"]
        refreshed[host] = {**refreshed_hostvars, "reachable": reachable}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"timestamp_utc": utc_now(), "hosts": refreshed}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if all(host["reachable"] for host in refreshed.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
