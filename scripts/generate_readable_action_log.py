#!/usr/bin/env python3
"""Generate compact successful-action logs from VTC JSONL/action logs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from vtc_traffic_generator.tools import render_readable_events


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--experiment-name")
    parser.add_argument("--run-id")
    parser.add_argument("--bot-id")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = render_readable_events.load_records(args.paths)
    lines = render_readable_events.render(
        records,
        experiment_name=args.experiment_name,
        run_id=args.run_id,
        bot_filter=args.bot_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
