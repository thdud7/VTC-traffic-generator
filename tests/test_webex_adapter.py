import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "vtc_traffic_generator"))

from vtc_traffic_generator.run_experiment import generate
from vtc_traffic_generator.vtc_automation.adapters.webex import PlaywrightTimeoutError
from vtc_traffic_generator.vtc_automation.adapters.registry import get_adapter, list_supported_services
from vtc_traffic_generator.vtc_automation.adapters.webex import WebexAdapter


class WebexAdapterTests(unittest.TestCase):
    def test_webex_is_registered_for_common_adapter_selection(self):
        self.assertIn("webex", list_supported_services())
        adapter = get_adapter({"vtc_platform": "webex", "adapter_config": {}})
        self.assertIsInstance(adapter, WebexAdapter)
        self.assertTrue(hasattr(adapter, "connect_to_meeting"))

    def test_webex_experiment_generates_remote_adapter_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated = generate("vtc_traffic_generator/experiment.webex.example.json", tmpdir)
            remote_config = json.loads(Path(generated["remote_configs"][0]).read_text(encoding="utf-8"))

        self.assertEqual(remote_config["vtc_platform"], "webex")
        self.assertEqual(remote_config["service"], "webex")
        self.assertEqual(remote_config["adapter_config"]["display_name"], "bot1")
        self.assertEqual(remote_config["adapter_config"]["browser_channel"], "chrome")
        self.assertTrue(remote_config["packet_capture"]["enabled"])
        self.assertIn("browser_join", remote_config["adapter_config"]["selectors"])
        self.assertIn("fallback", remote_config["adapter_config"])

    def test_selector_groups_can_be_overridden_from_config(self):
        adapter = WebexAdapter(
            {
                "adapter_config": {
                    "selectors": {
                        "join_button": "button[data-test='custom-join']",
                    }
                }
            }
        )

        self.assertEqual(adapter.selectors("join_button"), ["button[data-test='custom-join']"])
        self.assertIn('button:has-text("Join")', WebexAdapter({"adapter_config": {}}).selectors("join_button"))

    def test_launch_args_include_ec2_xvfb_friendly_flags(self):
        adapter = WebexAdapter({"adapter_config": {"browser_channel": "chrome", "disable_gpu": True}})

        args = adapter.browser_args()

        self.assertIn("--no-sandbox", args)
        self.assertIn("--disable-dev-shm-usage", args)
        self.assertIn("--autoplay-policy=no-user-gesture-required", args)
        self.assertIn("--use-fake-ui-for-media-stream", args)
        self.assertIn("--window-size=1280,720", args)
        self.assertIn("--lang=en-US", args)
        self.assertIn("--disable-gpu", args)
        self.assertIn("--disable-gpu-compositing", args)
        self.assertFalse(adapter.launch_options()["headless"])

    def test_launch_options_pass_configured_display_environment(self):
        adapter = WebexAdapter({"display": ":99", "adapter_config": {"browser_channel": "chrome"}})

        options = adapter.launch_options()

        self.assertEqual(options["env"]["DISPLAY"], ":99")
        self.assertEqual(options["channel"], "chrome")

    def test_launch_args_include_auto_select_desktop_capture_source(self):
        adapter = WebexAdapter(
            {
                "adapter_config": {
                    "screen_share_target": "VTC Share Window",
                    "extra_browser_args": ["--window-size=1280,720"],
                }
            }
        )

        args = adapter.browser_args()

        self.assertIn("--auto-select-desktop-capture-source=VTC Share Window", args)
        self.assertEqual(args.count("--window-size=1280,720"), 1)

    def test_browser_executable_discovery_uses_common_chrome_names(self):
        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which") as which:
            which.side_effect = lambda name: "/usr/bin/chromium" if name == "chromium" else None
            adapter = WebexAdapter({"adapter_config": {}})

            self.assertEqual(adapter.launch_options()["executable_path"], "/usr/bin/chromium")

    def test_collect_diagnostics_writes_metadata_with_partial_page_state(self):
        class FakePage:
            url = "https://example.webex.com/meet/test"

            def is_closed(self):
                return False

            async def title(self):
                return "Webex test"

            async def screenshot(self, path):
                Path(path).write_bytes(b"png")

            async def content(self):
                return "<html>test</html>"

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = WebexAdapter({"adapter_config": {"diagnostic_dir": tmpdir}})
            adapter.page = FakePage()
            adapter.browser_log.append({"event_type": "console", "message": "hello"})
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                run.return_value = subprocess_completed(returncode=0, stdout="ok\n", stderr="")
                result = asyncio.run(adapter.collect_diagnostics(stage="unit", extra={"case": "partial"}))

            metadata_path = Path(result["files"]["metadata"])
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

        self.assertTrue(metadata_path.name.endswith(".metadata.json"))
        self.assertEqual(metadata["stage"], "unit")
        self.assertEqual(metadata["extra"]["case"], "partial")
        self.assertEqual(metadata["url"], "https://example.webex.com/meet/test")
        self.assertEqual(metadata["title"], "Webex test")
        self.assertEqual(metadata["browser_log"][0]["message"], "hello")

    def test_collect_diagnostics_command_failures_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = WebexAdapter({"adapter_config": {"diagnostic_dir": tmpdir}})
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                run.side_effect = FileNotFoundError("missing tool")
                result = asyncio.run(adapter.collect_diagnostics(stage="commands"))

            metadata = json.loads(Path(result["files"]["metadata"]).read_text(encoding="utf-8"))

        self.assertIn("wmctrl_windows", metadata["commands"])
        self.assertIn("error", metadata["commands"]["wmctrl_windows"])
        self.assertIn("wmctrl_windows", result["errors"])

    def test_display_name_prefers_bot_display_name(self):
        adapter = WebexAdapter(
            {
                "bot": {"display_name": "Webex Bot"},
                "bot_name": "bot1",
                "adapter_config": {"display_name": "Configured Bot"},
            }
        )

        self.assertEqual(adapter._display_name(), "Webex Bot")

    def test_xdotool_fallback_uses_configured_keys(self):
        adapter = WebexAdapter(
            {
                "adapter_config": {
                    "fallback": {
                        "enabled": True,
                        "mic_key": "ctrl+shift+m",
                    }
                }
            }
        )
        calls = []
        adapter._run_command = lambda command, action_name: calls.append((command, action_name)) or True

        self.assertTrue(asyncio.run(adapter._fallback_action("mic")))
        self.assertEqual(calls, [(["xdotool", "key", "ctrl+shift+m"], "mic")])

    def test_connect_to_meeting_lobby_does_not_call_media_ready_unless_allowed(self):
        callbacks = []
        adapter = _join_test_adapter(
            "lobby",
            {
                "accept_lobby_as_joined": True,
                "_meeting_joined_callback": lambda url: callbacks.append(("joined", url)),
                "_media_ready_callback": lambda url: callbacks.append(("media", url)),
            },
        )

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "lobby")
        self.assertIn(("joined", "https://example.webex.com/meet/test"), callbacks)
        self.assertNotIn(("media", "https://example.webex.com/meet/test"), callbacks)

        callbacks = []
        adapter = _join_test_adapter(
            "lobby",
            {
                "accept_lobby_as_joined": True,
                "allow_lobby_media_ready": True,
                "_meeting_joined_callback": lambda url: callbacks.append(("joined", url)),
                "_media_ready_callback": lambda url: callbacks.append(("media", url)),
            },
        )

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertIn(("joined", "https://example.webex.com/meet/test"), callbacks)
        self.assertIn(("media", "https://example.webex.com/meet/test"), callbacks)

    def test_connect_to_meeting_blocked_collects_diagnostics_and_raises(self):
        adapter = _join_test_adapter("blocked")

        with self.assertRaisesRegex(RuntimeError, "blocked"):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(adapter.diagnostic_stages, ["join_blocked"])

    def test_connect_to_meeting_callbacks_wait_for_joined_indicator(self):
        callbacks = []
        adapter = _join_test_adapter(
            "joined",
            {
                "_meeting_joined_callback": lambda url: callbacks.append(("joined", list(adapter.page.clicks))),
                "_media_ready_callback": lambda url: callbacks.append(("media", list(adapter.page.clicks))),
            },
        )

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(callbacks, [("joined", ["#join"]), ("media", ["#join"])])


if __name__ == "__main__":
    unittest.main()


def subprocess_completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeWebexLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        if self.selector in self.page.visible:
            return None
        raise PlaywrightTimeoutError(f"{self.selector} is not visible")

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == "#join":
            self.page.visible.discard("#join")
            self.page.visible.add(f"#{self.page.join_result}")

    async def fill(self, value):
        self.page.fills.append((self.selector, value))

    async def inner_text(self, timeout=1000):
        return self.page.text


class FakeWebexPage:
    url = "https://example.webex.com/meet/test"

    def __init__(self, join_result):
        self.join_result = join_result
        self.visible = {"#join"}
        self.clicks = []
        self.fills = []
        self.text = f"visible {join_result} screen"

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def title(self):
        return "Fake Webex"

    def locator(self, selector):
        return FakeWebexLocator(self, selector)


def _join_test_adapter(join_result, extra_config=None):
    config = {
        "adapter_config": {
            "skip_sanity_checks": True,
            "page_load_wait_sec": 0,
            "prejoin_timeout_ms": 100,
            "joined_timeout_ms": 100,
            "join_result_timeout_sec": 0.2,
            "optional_selector_timeout_ms": 1,
            "join_result_poll_timeout_ms": 1,
            "selectors": {
                "join_button": "#join",
                "start_meeting_button": "#start",
                "joined_indicator": "#joined",
                "lobby_indicator": "#lobby",
                "blocked_indicator": "#blocked",
                "display_name": "#name",
                "email_input": "#email",
                "password_input": "#password",
                "cookie_accept": "#cookie",
                "join_from_browser": "#browser",
                "continue_in_browser": "#continue-browser",
                "join_as_guest": "#guest",
                "continue_button": "#continue",
                "next_button": "#next",
                "use_computer_audio": "#audio",
            },
        }
    }
    extra_config = dict(extra_config or {})
    adapter_config_updates = {
        key: extra_config.pop(key)
        for key in list(extra_config)
        if key in {"accept_lobby_as_joined", "allow_lobby_media_ready"}
    }
    config["adapter_config"].update(adapter_config_updates)
    config.update(extra_config)
    adapter = WebexAdapter(config)
    adapter.page = FakeWebexPage(join_result)
    adapter.launch = _async_true
    adapter.mute_microphone = _async_true
    adapter.unmute_microphone = _async_true
    adapter.start_camera = _async_true
    adapter.stop_camera = _async_true
    adapter.diagnostic_stages = []

    async def collect_diagnostics(stage=None, extra=None):
        adapter.diagnostic_stages.append(stage)
        return {"stage": stage, "extra": extra}

    adapter.collect_diagnostics = collect_diagnostics
    return adapter


async def _async_true(*args, **kwargs):
    return True
