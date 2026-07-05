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


if __name__ == "__main__":
    unittest.main()


def subprocess_completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)
