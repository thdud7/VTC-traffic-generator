#!/usr/bin/env python3
"""Upload bot video media to S3 and write a VTC media manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as infile:
        for chunk in iter(lambda: infile.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "bots": {}}
    with path.open("r", encoding="utf-8") as infile:
        return json.load(infile)


def parse_bot_video(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("bot video inputs must use bot_id=/path/to/video.mp4")
    bot_id, video_path = value.split("=", 1)
    bot_id = bot_id.strip()
    if not bot_id:
        raise argparse.ArgumentTypeError("bot_id cannot be empty")
    path = Path(video_path).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"video file does not exist: {path}")
    return bot_id, path


def require_s3_base(args: argparse.Namespace) -> str:
    if args.s3_uri:
        return args.s3_uri.rstrip("/")
    bucket = args.s3_bucket or os.environ.get("VTC_S3_BUCKET")
    prefix = args.s3_prefix if args.s3_prefix is not None else os.environ.get("VTC_S3_PREFIX", "")
    if not bucket:
        raise SystemExit("Set --s3-uri or VTC_S3_BUCKET/--s3-bucket")
    return f"s3://{bucket.strip('/')}/{prefix.strip('/')}".rstrip("/")


def aws_cp(source: Path, destination: str, dry_run: bool) -> None:
    command = ["aws", "s3", "cp", str(source), destination]
    if dry_run:
        print("DRY-RUN:", " ".join(command))
        return
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="+", type=parse_bot_video, help="bot_id=/path/to/video.mp4")
    parser.add_argument("--platform", default="google_meet", help="Manifest platform value")
    parser.add_argument("--media-profile", default="test", help="Media profile, for example test or final")
    parser.add_argument("--s3-uri", help="Base S3 URI. Overrides VTC_S3_BUCKET/VTC_S3_PREFIX")
    parser.add_argument("--s3-bucket", help="S3 bucket. Defaults to VTC_S3_BUCKET")
    parser.add_argument("--s3-prefix", help="S3 prefix. Defaults to VTC_S3_PREFIX")
    parser.add_argument(
        "--manifest",
        default="vtc_traffic_generator/media.google_meet.test.json",
        help="Local manifest JSON to create or update",
    )
    parser.add_argument(
        "--local-cache-root",
        default="/home/ubuntu/vtc_data/media/google_meet/test/video",
        help="Client-side cache root recorded in the manifest",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s3_base = require_s3_base(args)
    manifest_path = Path(args.manifest).expanduser()
    manifest = load_manifest(manifest_path)
    manifest.update(
        {
            "version": int(manifest.get("version", 1)),
            "platform": args.platform,
            "media_profile": args.media_profile,
            "media_type": "video",
            "local_cache_root": args.local_cache_root,
        }
    )
    bots = manifest.setdefault("bots", {})
    if not isinstance(bots, dict):
        raise SystemExit("manifest.bots must be an object keyed by bot_id")

    for bot_id, source in args.videos:
        object_key = f"media/{args.platform}/{args.media_profile}/video/{bot_id}/{source.name}"
        destination = f"{s3_base}/{object_key}"
        aws_cp(source, destination, args.dry_run)
        bots[bot_id] = {
            "bot_id": bot_id,
            "platform": args.platform,
            "media_profile": args.media_profile,
            "media_type": "video",
            "s3_uri": destination,
            "local_cache_path": str(Path(args.local_cache_root) / f"{bot_id}-{source.name}"),
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
        }

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
