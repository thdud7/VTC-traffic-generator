#!/usr/bin/env python3
"""Find or delete derived PCAPNG artifacts from an S3 experiment prefix."""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from typing import Iterable


DERIVED_MARKERS = (
    "jitsi-only",
    "jitsi_only",
    ".filtered.",
    "filtered",
    ".postprocessed.",
    "postprocessed",
    ".trimmed.",
    "trimmed",
)


@dataclass
class CleanupSummary:
    candidates_found: int = 0
    deleted_count: int = 0
    skipped_raw_count: int = 0
    skipped_non_pcap_count: int = 0


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def list_s3_keys(prefix: str) -> Iterable[tuple[int, str]]:
    result = run(["aws", "s3", "ls", prefix.rstrip("/") + "/", "--recursive"])
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        try:
            size = int(parts[2])
        except ValueError:
            size = 0
        yield size, parts[3]


def bucket_root(prefix: str) -> str:
    if not prefix.startswith("s3://"):
        raise ValueError("prefix must start with s3://")
    parts = prefix.split("/", 3)
    return "/".join(parts[:3])


def is_raw_pcap(key: str) -> bool:
    return key.endswith("_raw.pcapng")


def is_derived_pcap(key: str) -> bool:
    name = key.rsplit("/", 1)[-1].lower()
    return name.endswith(".pcapng") and any(marker in name for marker in DERIVED_MARKERS)


def cleanup(prefix: str, apply: bool) -> CleanupSummary:
    summary = CleanupSummary()
    root = bucket_root(prefix)
    for size, key in list_s3_keys(prefix):
        if not key.endswith(".pcapng"):
            summary.skipped_non_pcap_count += 1
            continue
        if is_raw_pcap(key):
            summary.skipped_raw_count += 1
            continue
        if not is_derived_pcap(key):
            continue
        summary.candidates_found += 1
        uri = f"{root}/{key}"
        if apply:
            result = run(["aws", "s3", "rm", uri])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
            summary.deleted_count += 1
            print(f"delete {uri} size={size}")
        else:
            print(f"dry-run delete {uri} size={size}")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("s3_prefix", help="Example: s3://vtc-traffic-data/captures/experiments")
    parser.add_argument("--apply", action="store_true", help="Actually delete candidates")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = cleanup(args.s3_prefix, args.apply)
    print(json.dumps(summary.__dict__, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
