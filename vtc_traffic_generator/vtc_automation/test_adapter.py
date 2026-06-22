import argparse
import asyncio
import time

from vtc_automation.adapters import create_vtc_adapter


def build_config(args):
    adapter_config = {
        "display": args.display,
        "camera_name": args.camera_name,
        "microphone_name": args.microphone_name,
        "ignore_certificate_errors": args.ignore_certificate_errors,
        "window_title_regex": args.window_title_regex,
        "launch_timeout_sec": args.launch_timeout_sec,
        "action_timeout_sec": args.action_timeout_sec,
        "event_log_path": args.event_log_path,
        "accessibility_dump_path": args.accessibility_dump_path,
        "screen_share_target": args.screen_share_target,
        "display_backend": args.display_backend,
        "skip_device_selection": args.skip_device_selection,
    }

    if args.executable_path:
        adapter_config["executable_path"] = args.executable_path
    if args.launch_command:
        adapter_config["launch_command"] = args.launch_command

    return {
        "service": args.service,
        "vtc_platform": args.service,
        "vtc_url": args.vtc_url,
        "bot": {"display_name": args.display_name},
        "bot_name": args.display_name,
        "adapter_config": adapter_config,
    }


async def run_adapter(args):
    config = build_config(args)
    adapter = create_vtc_adapter(args.service, config)

    await adapter.launch()

    if args.dump_accessibility_tree:
        if hasattr(adapter, "dump_accessibility_tree"):
            adapter.dump_accessibility_tree("manual", args.accessibility_dump_path)
        if args.dump_only:
            return

    if hasattr(adapter, "connect_to_meeting"):
        await adapter.connect_to_meeting(args.vtc_url, args.display_name)
    else:
        await adapter.connect(args.leave_after_sec / 60)

    status = await adapter.is_in_meeting()
    print(f"in_meeting={status}")

    if args.screen_share_target:
        started = await adapter.start_screen_share()
        print(f"screen_share_started={started}")
        if started:
            await asyncio.sleep(args.screen_share_hold_sec)
            stopped = await adapter.stop_screen_share()
            print(f"screen_share_stopped={stopped}")

    if args.leave_after_sec is None:
        print("Adapter is staying connected. Press Ctrl-C to stop this process.")
        while True:
            await asyncio.sleep(60)

    await asyncio.sleep(args.leave_after_sec)
    await adapter.leave()
    await adapter.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Smoke-test a VTC automation adapter.")
    parser.add_argument("--service", required=True)
    parser.add_argument("--vtc-url", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--display", default=":99")
    parser.add_argument("--executable-path")
    parser.add_argument("--launch-command")
    parser.add_argument("--camera-name", default="VTC Bot Camera")
    parser.add_argument("--microphone-name", default="VTC_Microphone")
    parser.add_argument("--window-title-regex", default="Jitsi Meet|testroom|jitsi")
    parser.add_argument("--launch-timeout-sec", type=int, default=20)
    parser.add_argument("--action-timeout-sec", type=int, default=10)
    parser.add_argument("--event-log-path", default="/tmp/vtc-events.jsonl")
    parser.add_argument("--accessibility-dump-path")
    parser.add_argument("--screen-share-target")
    parser.add_argument("--screen-share-hold-sec", type=float, default=5)
    parser.add_argument("--display-backend", choices=["xvfb", "xorg_dummy"], default="xvfb")
    parser.add_argument("--skip-device-selection", action="store_true")
    parser.add_argument("--dump-accessibility-tree", action="store_true")
    parser.add_argument("--dump-only", action="store_true")
    parser.add_argument("--leave-after-sec", type=float)
    parser.add_argument(
        "--ignore-certificate-errors",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        asyncio.run(run_adapter(args))
    except KeyboardInterrupt:
        print(f"Interrupted at {time.time()}")


if __name__ == "__main__":
    main()
