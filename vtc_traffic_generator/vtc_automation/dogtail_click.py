import argparse
import sys


def walk(node):
    yield node
    for child in getattr(node, "children", []):
        yield from walk(child)


def main():
    parser = argparse.ArgumentParser(description="Click an accessibility node by name.")
    parser.add_argument("--name", action="append", required=True)
    parser.add_argument("--role", action="append")
    args = parser.parse_args()

    try:
        from dogtail.tree import root
    except Exception as exc:
        print(f"dogtail unavailable: {exc}", file=sys.stderr)
        return 2

    names = set(args.name)
    roles = set(args.role or [])

    for node in walk(root):
        node_name = getattr(node, "name", None)
        role_name = getattr(node, "roleName", None)
        if node_name in names and (not roles or role_name in roles):
            node.click()
            print(f"clicked name={node_name} role={role_name}")
            return 0

    print(f"no node matched names={sorted(names)} roles={sorted(roles)}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
