from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


def safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def node_children(node: Any) -> list[Any]:
    children = safe_getattr(node, "children", [])
    try:
        return list(children)
    except Exception:
        return []


def walk(node: Any, depth: int = 0):
    yield node, depth
    for child in node_children(node):
        yield from walk(child, depth + 1)


def node_states(node: Any) -> list[str]:
    try:
        state = node.getState()
        return sorted(str(item) for item in state.getStates())
    except Exception:
        pass

    states = safe_getattr(node, "states", [])
    try:
        return sorted(str(item) for item in states)
    except Exception:
        return []


def node_bounds(node: Any) -> dict[str, int | None]:
    position = safe_getattr(node, "position")
    size = safe_getattr(node, "size")

    left = top = width = height = None
    if position and len(position) >= 2:
        left, top = int(position[0]), int(position[1])
    if size and len(size) >= 2:
        width, height = int(size[0]), int(size[1])

    return {
        "x": left,
        "y": top,
        "width": width,
        "height": height,
    }


def node_record(node: Any, depth: int) -> dict[str, Any]:
    return {
        "name": safe_getattr(node, "name", ""),
        "role": safe_getattr(node, "roleName", ""),
        "description": safe_getattr(node, "description", ""),
        "states": node_states(node),
        "bounds": node_bounds(node),
        "depth": depth,
    }


def matches_window(record: dict[str, Any], window_regex: str | None) -> bool:
    if not window_regex:
        return True

    haystack = " ".join(
        str(record.get(key, ""))
        for key in ("name", "role", "description")
    )
    return re.search(window_regex, haystack, re.IGNORECASE) is not None


def select_root(root: Any, window_regex: str | None) -> Any:
    if not window_regex:
        return root

    for node, depth in walk(root):
        if matches_window(node_record(node, depth), window_regex):
            return node

    return root


def matches_node(
    record: dict[str, Any],
    names: list[str],
    roles: list[str],
    partial: bool,
) -> bool:
    node_name = str(record.get("name") or "")
    node_role = str(record.get("role") or "")

    if roles and node_role not in roles:
        return False

    if not names:
        return True

    if partial:
        lowered = node_name.lower()
        return any(name.lower() in lowered for name in names)

    return node_name in names


def find_node(root: Any, args: argparse.Namespace):
    subtree = select_root(root, args.window_regex)
    for node, depth in walk(subtree):
        record = node_record(node, depth)
        if matches_node(record, args.name or [], args.role or [], args.partial):
            return node, record
    return None, None


def dump_tree(root: Any, args: argparse.Namespace) -> int:
    subtree = select_root(root, args.window_regex)
    records = [node_record(node, depth) for node, depth in walk(subtree)]
    payload = {
        "window_regex": args.window_regex,
        "nodes": records,
    }

    if args.output:
        with open(args.output, "w", encoding="utf-8") as output_file:
            json.dump(payload, output_file, indent=2, sort_keys=True)
    else:
        print(json.dumps(payload, sort_keys=True))

    return 0


def click_node(root: Any, args: argparse.Namespace) -> int:
    node, record = find_node(root, args)
    if node is None:
        print("no matching accessibility node", file=sys.stderr)
        return 1

    node.click()
    print(json.dumps(record, sort_keys=True))
    return 0


def find_node_command(root: Any, args: argparse.Namespace) -> int:
    _node, record = find_node(root, args)
    if record is None:
        print("no matching accessibility node", file=sys.stderr)
        return 1

    print(json.dumps(record, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or operate dogtail accessibility nodes.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("click", "find"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--name", action="append")
        subparser.add_argument("--role", action="append")
        subparser.add_argument("--partial", action="store_true")
        subparser.add_argument("--window-regex")

    dump_parser = subparsers.add_parser("dump")
    dump_parser.add_argument("--output")
    dump_parser.add_argument("--window-regex")

    args = parser.parse_args()

    try:
        from dogtail.tree import root
    except Exception as exc:
        print(f"dogtail unavailable: {exc}", file=sys.stderr)
        return 2

    if args.command == "click":
        return click_node(root, args)
    if args.command == "find":
        return find_node_command(root, args)
    if args.command == "dump":
        return dump_tree(root, args)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
