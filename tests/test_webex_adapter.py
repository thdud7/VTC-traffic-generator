import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

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
