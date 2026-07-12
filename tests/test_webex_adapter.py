import asyncio
import contextlib
import io
import json
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "vtc_traffic_generator"))

from vtc_traffic_generator.run_experiment import generate
from vtc_traffic_generator.vtc_automation import test_adapter
from vtc_traffic_generator.vtc_automation.adapters.webex import PlaywrightTimeoutError, TargetClosedError
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
            inventory = Path(generated["inventory"]).read_text(encoding="utf-8")

        self.assertEqual(remote_config["vtc_platform"], "webex")
        self.assertEqual(remote_config["service"], "webex")
        self.assertEqual(remote_config["adapter_config"]["display"], ":99")
        self.assertEqual(remote_config["adapter_config"]["display_name"], "bot1")
        self.assertEqual(remote_config["adapter_config"]["microphone_name"], "VTC_Microphone")
        self.assertEqual(remote_config["adapter_config"]["browser_channel"], "chrome")
        self.assertTrue(remote_config["adapter_config"]["screen_share_target"].startswith("VTC Share Window"))
        self.assertEqual(
            remote_config["adapter_config"]["auto_select_desktop_capture_source"],
            remote_config["adapter_config"]["screen_share_target"],
        )
        self.assertEqual(remote_config["virtual_audio"]["source_name"], "VTC_Microphone")
        self.assertTrue(remote_config["packet_capture"]["enabled"])
        self.assertNotIn("jvb_ip", remote_config["packet_capture"])
        self.assertNotIn("jvb_port", remote_config["packet_capture"])
        self.assertIn("browser_join", remote_config["adapter_config"]["selectors"])
        self.assertIn("fallback", remote_config["adapter_config"])
        self.assertIn("vtc_service=webex", inventory)
        self.assertIn("audio_source_name=VTC_Microphone", inventory)
        self.assertNotIn("jitsi_electron_launcher", inventory)

    def test_ansible_branches_browser_and_jitsi_preflight_by_service(self):
        deploy_playbook = Path("ansible/deploy_clients.yml").read_text(encoding="utf-8")
        preflight_playbook = Path("ansible/preflight_clients.yml").read_text(encoding="utf-8")

        self.assertIn("Verify Chrome or Chromium is available for browser services", deploy_playbook)
        self.assertIn("google-chrome google-chrome-stable chromium chromium-browser", deploy_playbook)
        self.assertIn("== 'jitsi_electron'", deploy_playbook)
        self.assertIn("source_name={{ audio_source_name | default('VTC_Microphone') | quote }}", preflight_playbook)
        self.assertIn("Webex|Chrome|Chromium|Google Chrome", preflight_playbook)

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

    def test_test_adapter_supports_overall_and_debug_timeouts(self):
        with patch.object(
            sys,
            "argv",
            [
                "test_adapter",
                "--service",
                "webex",
                "--vtc-url",
                "https://example.webex.com/meet/test",
                "--display-name",
                "bot",
                "--prejoin-timeout-sec",
                "45",
                "--join-timeout-sec",
                "45",
                "--overall-timeout-sec",
                "90",
            ],
        ):
            args = test_adapter.parse_args()

        config = test_adapter.build_config(args)

        self.assertEqual(args.overall_timeout_sec, 90)
        self.assertEqual(config["adapter_config"]["prejoin_timeout_ms"], 45000)
        self.assertEqual(config["adapter_config"]["join_result_timeout_sec"], 45)
        self.assertEqual(config["adapter_config"]["joined_timeout_ms"], 45000)

    def test_overall_timeout_calls_collect_diagnostics_and_closes_adapter(self):
        fake_adapter = FakeSmokeAdapter()
        with patch.object(
            sys,
            "argv",
            [
                "test_adapter",
                "--service",
                "webex",
                "--vtc-url",
                "https://example.webex.com/meet/test",
                "--display-name",
                "bot",
                "--leave-after-sec",
                "10",
                "--overall-timeout-sec",
                "0.01",
            ],
        ):
            args = test_adapter.parse_args()

        with patch("vtc_traffic_generator.vtc_automation.test_adapter.create_vtc_adapter", return_value=fake_adapter):
            with self.assertRaisesRegex(RuntimeError, "overall-timeout-sec"):
                asyncio.run(test_adapter.run_adapter(args))

        self.assertIn("webex_smoke_overall_timeout", fake_adapter.diagnostic_stages)
        self.assertTrue(fake_adapter.closed)

    def test_test_adapter_exits_cleanly_after_joined_result_and_leave_delay(self):
        fake_adapter = FakeJoinedSmokeAdapter({"status": "waiting_for_others", "leave_control_present": True})
        with patch.object(
            sys,
            "argv",
            [
                "test_adapter",
                "--service",
                "webex",
                "--vtc-url",
                "https://example.webex.com/meet/test",
                "--display-name",
                "bot",
                "--leave-after-sec",
                "0",
                "--overall-timeout-sec",
                "1",
            ],
        ):
            args = test_adapter.parse_args()

        stdout = io.StringIO()
        with patch("vtc_traffic_generator.vtc_automation.test_adapter.create_vtc_adapter", return_value=fake_adapter):
            with contextlib.redirect_stdout(stdout):
                asyncio.run(test_adapter.run_adapter(args))

        self.assertIn('"status": "waiting_for_others"', stdout.getvalue())
        self.assertTrue(fake_adapter.left)
        self.assertTrue(fake_adapter.closed)

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

    def test_chrome_profile_preparation_writes_protocol_handler_excluded_schemes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "webex-profile"
            adapter = WebexAdapter(
                {
                    "adapter_config": {
                        "chrome_user_data_dir": str(profile_dir),
                        "blocked_external_protocol_schemes": ["webex", "wbx", "ciscospark"],
                        "protocol_handler_excluded_scheme_value": True,
                    }
                }
            )

            prepared = adapter._prepare_external_protocol_suppression_profile()

            self.assertEqual(prepared, profile_dir)
            for relative_path in ("Local State", "Default/Preferences"):
                data = json.loads((profile_dir / relative_path).read_text(encoding="utf-8"))
                excluded = data["protocol_handler"]["excluded_schemes"]
                self.assertEqual(excluded["webex"], True)
                self.assertEqual(excluded["wbx"], True)
                self.assertEqual(excluded["ciscospark"], True)

    def test_webex_launch_uses_persistent_profile_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "webex-profile"
            fake_playwright = FakePlaywright()
            adapter = WebexAdapter(
                {
                    "adapter_config": {
                        "skip_sanity_checks": True,
                        "chrome_user_data_dir": str(profile_dir),
                        "browser_channel": "chrome",
                    }
                }
            )

            with patch("playwright.async_api.async_playwright", return_value=FakePlaywrightStarter(fake_playwright)):
                asyncio.run(adapter.launch())

            self.assertEqual(fake_playwright.chromium.persistent_user_data_dir, str(profile_dir))
            self.assertIsNotNone(adapter.context)
            self.assertIsNone(adapter.browser)

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
                "post_join_media_check": True,
                "_meeting_joined_callback": lambda url: callbacks.append(("joined", list(adapter.page.clicks))),
                "_media_ready_callback": lambda url: callbacks.append(("media", list(adapter.page.clicks))),
            },
        )

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(callbacks, [("joined", ["#join"]), ("media", ["#join"])])

    def test_connect_to_meeting_does_not_call_strict_media_before_joined_indicator(self):
        adapter = _join_test_adapter("joined", {"post_join_media_check": True})
        calls = []

        async def strict_media_action(name):
            self.assertIn("#joined", adapter.page.visible)
            calls.append(name)
            return True

        adapter.unmute_microphone = lambda: strict_media_action("unmute")
        adapter.start_camera = lambda: strict_media_action("camera")

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(calls, ["unmute", "camera"])

    def test_connect_to_meeting_returns_joined_without_default_post_join_media_check(self):
        adapter = _join_test_adapter("joined")
        calls = []

        async def strict_media_action(name):
            calls.append(name)
            raise AssertionError("post-join media action should not run by default")

        adapter.unmute_microphone = lambda: strict_media_action("unmute")
        adapter.start_camera = lambda: strict_media_action("camera")

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertEqual(calls, [])

    def test_connect_to_meeting_skip_device_selection_does_not_strictly_unmute_after_joined(self):
        adapter = _join_test_adapter(
            "joined",
            {"post_join_media_check": True, "skip_device_selection": True},
        )
        calls = []

        async def strict_media_action(name):
            calls.append(name)
            raise AssertionError("strict post-join media action should be skipped")

        adapter.unmute_microphone = lambda: strict_media_action("unmute")
        adapter.start_camera = lambda: strict_media_action("camera")

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertFalse(result["media_ready"])
        self.assertEqual(calls, [])

    def test_waiting_for_others_indicator_is_classified_and_returned(self):
        adapter = _join_test_adapter(
            "waiting_for_others",
            {"selectors": {"waiting_for_others_indicator": "#waiting_for_others"}},
        )

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "waiting_for_others")
        self.assertTrue(result["leave_control_present"])
        self.assertFalse(result["media_ready"])

    def test_korean_waiting_for_others_text_is_joined_like_success(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        adapter.page.text = "다른 사용자가 참여할 때까지 기다리는 중..."
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "waiting_for_others")
        self.assertIn("webex_waiting_for_others_detected", output.getvalue())
        self.assertEqual(adapter.page.fills, [])

    def test_english_waiting_for_others_text_is_joined_like_success(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        adapter.page.text = "Waiting for others to join"

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "waiting_for_others")

    def test_post_final_join_waiting_for_others_text_succeeds_without_prejoin_retry(self):
        adapter = _join_test_adapter("waiting_for_others", {"post_final_join_result_timeout_sec": 0.2})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        progress = output.getvalue()
        self.assertEqual(result["status"], "waiting_for_others")
        self.assertIn("webex_post_final_join_wait_start", progress)
        self.assertIn("webex_waiting_for_others_detected", progress)
        self.assertIn("webex_join_success", progress)
        self.assertLess(progress.index("final_join_clicked"), progress.index("webex_post_final_join_wait_start"))
        self.assertNotIn("webex_prejoin_loop_iteration_start", progress[progress.index("final_join_clicked"):])

    def test_post_final_join_waiting_for_host_text_succeeds(self):
        adapter = _join_test_adapter("waiting_for_host", {"post_final_join_result_timeout_sec": 0.2})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "waiting_for_host")
        self.assertIn("webex_waiting_for_host_detected", output.getvalue())
        self.assertIn("webex_join_success", output.getvalue())

    def test_post_final_join_lobby_text_succeeds(self):
        adapter = _join_test_adapter("lobby", {"post_final_join_result_timeout_sec": 0.2})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "lobby")
        self.assertIn("webex_lobby_detected", output.getvalue())
        self.assertIn("webex_join_success", output.getvalue())

    def test_post_final_join_window_fallback_in_meeting_title_succeeds(self):
        adapter = _join_test_adapter("no_dom_state", {"post_final_join_result_timeout_sec": 0.6})
        adapter.page.text = ""
        output = io.StringIO()
        original_window_state = adapter._detect_webex_meeting_window_state

        def post_final_window_state():
            if not adapter._final_join_clicked_success:
                return None
            return original_window_state()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch.object(adapter, "_detect_webex_meeting_window_state", side_effect=post_final_window_state):
                    with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                        run.return_value = subprocess_completed(
                            returncode=0,
                            stdout="0x00400003  0 2 40 1288 851 bot4 In meeting · Meeting · Webex - Chromium\n",
                            stderr="",
                        )
                        with contextlib.redirect_stdout(output):
                            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertEqual(result["selector"], "window_fallback")
        self.assertTrue(asyncio.run(adapter.is_in_meeting()))
        self.assertTrue(adapter.in_meeting)
        self.assertIn("webex_post_final_join_window_fallback_candidate", output.getvalue())
        self.assertIn("webex_post_final_join_window_fallback_success", output.getvalue())
        self.assertIn("webex_join_success", output.getvalue())

    def test_post_final_join_get_ready_window_fallback_is_rejected_not_joined(self):
        adapter = _join_test_adapter("no_dom_state", {"post_final_join_result_timeout_sec": 0.01})
        adapter.page.text = ""
        output = io.StringIO()
        original_window_state = adapter._detect_webex_meeting_window_state

        def post_final_window_state():
            if not adapter._final_join_clicked_success:
                return None
            return original_window_state()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch.object(adapter, "_detect_webex_meeting_window_state", side_effect=post_final_window_state):
                    with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                        run.return_value = subprocess_completed(
                            returncode=0,
                            stdout="0x00400003  0 2 40 1288 851 bot5 Get ready to join · Meeting · Webex - Chromium\n",
                            stderr="",
                        )
                        with contextlib.redirect_stdout(output):
                            with self.assertRaisesRegex(RuntimeError, "webex_post_final_join_pending_timeout"):
                                asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        progress = output.getvalue()
        self.assertIn("webex_post_final_join_window_fallback_candidate", progress)
        self.assertIn("webex_post_final_join_window_fallback_rejected", progress)
        self.assertIn("webex_post_final_join_pending_timeout", progress)
        self.assertNotIn("webex_post_final_join_window_fallback_success", progress)
        self.assertNotIn("webex_join_success", progress)
        self.assertFalse(adapter.in_meeting)

    def test_post_final_join_get_ready_window_eventually_in_meeting_succeeds(self):
        adapter = _join_test_adapter("no_dom_state", {"post_final_join_result_timeout_sec": 0.6})
        adapter.page.text = ""
        output = io.StringIO()
        original_window_state = adapter._detect_webex_meeting_window_state

        def post_final_window_state():
            if not adapter._final_join_clicked_success:
                return None
            return original_window_state()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch.object(adapter, "_detect_webex_meeting_window_state", side_effect=post_final_window_state):
                    with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                        run.side_effect = [
                            subprocess_completed(
                                returncode=0,
                                stdout="0x00400003  0 2 40 1288 851 bot5 Get ready to join · Meeting · Webex - Chromium\n",
                                stderr="",
                            ),
                            subprocess_completed(
                                returncode=0,
                                stdout="0x00400003  0 2 40 1288 851 bot5 In meeting · Meeting · Webex - Chromium\n",
                                stderr="",
                            ),
                        ]
                        with contextlib.redirect_stdout(output):
                            result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertTrue(adapter.in_meeting)
        self.assertIn("webex_post_final_join_window_fallback_rejected", output.getvalue())
        self.assertIn("webex_post_final_join_window_fallback_success", output.getvalue())

    def test_post_final_join_unknown_state_times_out_with_specific_stage(self):
        adapter = _join_test_adapter("no_dom_state", {"post_final_join_result_timeout_sec": 0.01})
        adapter.page.text = ""
        output = io.StringIO()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value=None):
                with contextlib.redirect_stdout(output):
                    with self.assertRaisesRegex(RuntimeError, "timeout"):
                        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertIn("webex_post_final_join_result_timeout", output.getvalue())
        self.assertIn("webex_post_final_join_result_timeout", adapter.diagnostic_stages)

    def test_prejoin_cisco_loading_state_times_out_with_specific_stage(self):
        adapter = _join_test_adapter("no_dom_state", {"prejoin_timeout_ms": 10})
        adapter.page.visible = set()
        adapter.page.text = ""
        adapter.page.html = "<html><body></body></html>"
        adapter.page.title_text = "Cisco Webex"
        output = io.StringIO()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                    run.return_value = subprocess_completed(
                        returncode=0,
                        stdout="0x00400003  0 2 40 1288 851 bot6 Cisco Webex - Chromium\n",
                        stderr="",
                    )
                    with contextlib.redirect_stdout(output):
                        with self.assertRaisesRegex(RuntimeError, "webex_prejoin_loading_timeout"):
                            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertIn("webex_prejoin_loading_timeout", adapter.diagnostic_stages)
        self.assertNotIn("webex_join_success", output.getvalue())

    def test_context_popup_waiting_for_others_text_is_scanned(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        popup = FakeWebexPage("joined")
        popup.url = "https://web.webex.com/meeting/test"
        popup.visible = set()
        popup.text = "Waiting for others to join"
        adapter.context = FakeContext([popup])

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "waiting_for_others")
        self.assertEqual(result["url"], "https://web.webex.com/meeting/test")

    def test_media_controls_without_display_name_input_are_joined_like_success(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#mute", "#leave"}
        adapter.page.selector_texts["#mute"] = "Mute"
        adapter.page.selector_texts["#leave"] = "Leave meeting"
        adapter.page.text_inputs = []

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertEqual(adapter.page.fills, [])

    def test_linux_window_manager_fallback_detects_webex_meeting_window(self):
        adapter = _join_test_adapter("joined")
        adapter._browser_join_clicked_once = True

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                    run.return_value = subprocess_completed(
                        returncode=0,
                        stdout="0x01200007  0 10 10 1280 720 host Webex Meeting - Google Chrome\n",
                        stderr="",
                    )
                    state = adapter._detect_webex_meeting_window_state()

        self.assertEqual(state["status"], "joined")
        self.assertEqual(state["selector"], "window_fallback")
        self.assertTrue(state["joined"])
        self.assertEqual(state["joined_source"], "window_fallback")

    def test_prejoin_window_fallback_join_returns_without_display_name_or_browser_join(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#join"}
        adapter._browser_join_clicked_once = True

        async def fail_fill(*args, **kwargs):
            raise AssertionError("display name fill should not run after joined fallback")

        async def fail_browser_join(*args, **kwargs):
            raise AssertionError("browser join should not be clicked after joined fallback")

        adapter._fill_display_name = fail_fill
        adapter._fill_display_name_if_needed = fail_fill
        adapter._click_browser_prejoin_selector = fail_browser_join

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                    run.return_value = subprocess_completed(
                        returncode=0,
                        stdout="0x00400003  0 2 40 1288 851 bot4 Cisco Webex - Chromium\n",
                        stderr="",
                    )
                    result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertEqual(result["selector"], "window_fallback")
        self.assertEqual(adapter.page.clicks, [])
        self.assertEqual(adapter.page.fills, [])

    def test_window_fallback_joined_progress_emits_success_and_exits(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#join"}
        adapter._browser_join_clicked_once = True
        output = io.StringIO()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                    run.return_value = subprocess_completed(
                        returncode=0,
                        stdout="0x00400003  0 2 40 1288 851 bot4 Cisco Webex - Chromium\n",
                        stderr="",
                    )
                    with contextlib.redirect_stdout(output):
                        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        progress = output.getvalue()
        self.assertEqual(result["status"], "joined")
        self.assertIn("webex_joined_state_detected_by_window_fallback", progress)
        self.assertIn('"status": "joined"', progress)
        self.assertIn("webex_joined_state_scan_result", progress)
        self.assertIn("webex_join_success", progress)
        self.assertNotIn("display_name_fill_skipped", progress)

    def test_get_ready_window_fallback_is_candidate_not_terminal_success(self):
        adapter = _join_test_adapter("joined")
        adapter._browser_join_clicked_once = True

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which", return_value="/usr/bin/wmctrl"):
                with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                    run.return_value = subprocess_completed(
                        returncode=0,
                        stdout="0x00400003  0 2 40 1288 851 bot4 Get ready to join · Meeting · Webex - Chromium\n",
                        stderr="",
                    )
                    state = adapter._detect_webex_meeting_window_state()

        self.assertEqual(state["status"], "candidate")
        self.assertFalse(state["joined"])
        self.assertFalse(adapter._is_terminal_join_state(state))

    def test_leave_without_button_falls_back_to_close_and_logs(self):
        adapter = _join_test_adapter("joined")
        closed = []

        async def close_page():
            closed.append(True)

        adapter.page.close = close_page
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter.leave())

        self.assertTrue(result)
        self.assertEqual(closed, [True])
        progress = output.getvalue()
        self.assertIn("webex_leave_attempt", progress)
        self.assertIn("webex_leave_fallback_close", progress)
        self.assertIn("webex_leave_done", progress)

    def test_korean_in_meeting_title_is_classified_as_joined(self):
        adapter = _join_test_adapter("no_selector_match")
        adapter.page.title_after_join = "미팅 중 · 미팅 · Webex"

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertEqual(result["selector"], "title:joined")

    def test_optional_post_join_microphone_timeout_is_consumed(self):
        adapter = _join_test_adapter(
            "joined",
            {"post_join_media_check": True, "post_join_media_check_timeout_sec": 0.01},
        )

        async def slow_unmute():
            await asyncio.sleep(1)
            return True

        adapter.unmute_microphone = slow_unmute

        result = asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(result["status"], "joined")
        self.assertFalse(result["media_ready"])

    def test_prejoin_media_preference_failure_is_non_fatal_by_default(self):
        adapter = _control_test_adapter(
            {"mic": False},
            transitions_enabled=False,
            extra_adapter_config={
                "selectors": {
                    "prejoin_mic_on_indicator": "#mic-on",
                    "prejoin_mic_off_indicator": "#mic-off",
                    "prejoin_camera_on_indicator": "#camera-on",
                    "prejoin_camera_off_indicator": "#camera-off",
                }
            },
        )

        self.assertFalse(asyncio.run(adapter._apply_prejoin_media_preferences()))

        self.assertEqual(adapter.page.clicks, ["#mic-off"])
        self.assertEqual(adapter.diagnostic_stages, [])

    def test_strict_prejoin_media_state_collects_diagnostics_on_failure(self):
        adapter = _control_test_adapter(
            {"mic": False},
            transitions_enabled=False,
            extra_adapter_config={
                "strict_prejoin_media_state": True,
                "selectors": {
                    "prejoin_mic_on_indicator": "#mic-on",
                    "prejoin_mic_off_indicator": "#mic-off",
                    "prejoin_camera_on_indicator": "#camera-on",
                    "prejoin_camera_off_indicator": "#camera-off",
                },
            },
        )

        self.assertFalse(asyncio.run(adapter._apply_prejoin_media_preferences()))

        self.assertEqual(adapter.diagnostic_stages, ["webex_prejoin_media_state_unverified"])

    def test_fail_on_prejoin_media_state_unverified_can_raise(self):
        adapter = _control_test_adapter(
            {"mic": False},
            transitions_enabled=False,
            extra_adapter_config={
                "fail_on_prejoin_media_state_unverified": True,
                "selectors": {
                    "prejoin_mic_on_indicator": "#mic-on",
                    "prejoin_mic_off_indicator": "#mic-off",
                    "prejoin_camera_on_indicator": "#camera-on",
                    "prejoin_camera_off_indicator": "#camera-off",
                },
            },
        )

        with self.assertRaisesRegex(RuntimeError, "prejoin microphone state"):
            asyncio.run(adapter._apply_prejoin_media_preferences())

    def test_browser_join_selector_is_attempted_before_final_join(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#browser", "#join"})

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertLess(adapter.page.clicks.index("#browser"), adapter.page.clicks.index("#join"))

    def test_display_name_is_filled_on_guest_page(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot-local-1"))

        self.assertIn(("#name", "bot-local-1"), adapter.page.fills)

    def test_external_protocol_prompt_dismissed_after_goto_and_during_prejoin_loop(self):
        adapter = _join_test_adapter("joined")
        stages = []

        async def dismiss(stage=None):
            stages.append(stage)
            return True

        adapter._dismiss_external_protocol_prompt = dismiss

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertIn("after_goto", stages)
        self.assertIn("prejoin_loop", stages)

    def test_connect_to_meeting_retries_when_title_reports_target_closed_after_goto(self):
        first = ClosingAfterGotoPage("joined")
        second = FakeWebexPage("joined")
        adapter = _join_test_adapter("joined")
        adapter.page = first
        adapter.context = FakeContext([second])

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(first.goto_calls, 1)
        self.assertEqual(second.url, "https://example.webex.com/meet/test")
        self.assertIn("page_recreated", [item["event_type"] for item in adapter.browser_log])

    def test_event_after_goto_uses_safe_title_helper(self):
        adapter = _join_test_adapter("joined")
        adapter.page = TitleFailsPage("joined")
        events = []

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.emit_event") as emit:
            emit.side_effect = lambda config, event, details, service=None: events.append((event, details))
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        opened = [details for event, details in events if event == "webex_page_opened"]
        self.assertEqual(opened[0]["title"], "")
        self.assertEqual(opened[0]["url"], "https://example.webex.com/meet/test")

    def test_collect_diagnostics_safe_helpers_do_not_crash_when_page_closed(self):
        class ClosedDiagnosticsPage:
            url = "https://example.webex.com/meet/test"

            def is_closed(self):
                return True

            async def title(self):
                raise TargetClosedError("closed")

            async def screenshot(self, path):
                raise TargetClosedError("closed")

            async def content(self):
                raise TargetClosedError("closed")

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = WebexAdapter({"adapter_config": {"diagnostic_dir": tmpdir}})
            adapter.page = ClosedDiagnosticsPage()
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                run.return_value = subprocess_completed(returncode=0, stdout="ok\n", stderr="")
                result = asyncio.run(adapter.collect_diagnostics(stage="closed"))

            metadata = json.loads(Path(result["files"]["metadata"]).read_text(encoding="utf-8"))

        self.assertEqual(metadata["title"], "")
        self.assertEqual(metadata["url"], "")
        self.assertNotIn("screenshot", result["files"])
        self.assertNotIn("html", result["files"])

    def test_navigation_retry_runs_once_when_page_closes_after_goto(self):
        first = ClosingAfterGotoPage("joined")
        second = FakeWebexPage("joined")
        adapter = _join_test_adapter("joined")
        adapter.page = first
        adapter.context = FakeContext([second])

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertEqual(first.goto_calls, 1)
        self.assertEqual(adapter.page, second)
        self.assertEqual(adapter.diagnostic_stages, ["webex_page_closed_after_goto"])

    def test_navigation_retry_failure_raises_clear_runtime_error(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "max_navigation_retries": 1,
                "retry_navigation_on_page_closed": True,
            },
        )
        adapter.page = ClosingAfterGotoPage("joined")
        adapter.context = FakeContext([ClosingAfterGotoPage("joined")])

        with self.assertRaisesRegex(RuntimeError, "Webex page closed after navigation before prejoin flow could start"):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

    def test_external_protocol_prompt_helper_does_not_quit_or_kill_chrome(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        commands = []
        adapter._run_external_protocol_command = lambda command, action_name, env_display=None: commands.append(command) or {
            "command": command,
            "action": action_name,
            "success": True,
        }

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        rendered = json.dumps(commands, ensure_ascii=False).lower()
        self.assertNotIn(" to quit", rendered)
        self.assertNotIn("killall", rendered)
        self.assertNotIn("pkill", rendered)

    def test_external_protocol_prompt_helper_never_clicks_open_webex(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        commands = []

        def run(command, action_name, env_display=None):
            commands.append(command)
            return {"command": command, "action": action_name, "success": True}

        adapter._run_external_protocol_command = run

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        rendered = json.dumps(commands, ensure_ascii=False)
        self.assertNotIn("Open Webex", rendered)
        self.assertNotIn("Webex 열기", rendered)

    def test_macos_external_protocol_fallback_supports_korean_cancel(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        commands = []
        adapter._run_external_protocol_command = lambda command, action_name, env_display=None: commands.append(command) or {
            "command": command,
            "action": action_name,
            "success": True,
        }

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        self.assertIn("취소", json.dumps(commands, ensure_ascii=False))

    def test_korean_cancel_locator_timeout_is_observed(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        adapter.page.timeout_selectors.add('button:has-text("취소")')
        loop_errors = []

        async def run_probe():
            loop = asyncio.get_running_loop()
            loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
            return await adapter._click_browser_prejoin_selector("cancel_open_app_prompt", timeout_ms=1)

        result = asyncio.run(run_probe())

        self.assertIsNone(result)
        self.assertEqual(loop_errors, [])

    def test_external_protocol_progress_uses_required_stage_names(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        adapter._run_external_protocol_command = lambda command, action_name, env_display=None: {
            "command": command,
            "action": action_name,
            "success": True,
        }
        output = io.StringIO()

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            with contextlib.redirect_stdout(output):
                asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        progress = output.getvalue()
        self.assertIn("external_protocol_dismiss_attempt", progress)
        self.assertIn("external_protocol_dismiss_done", progress)
        self.assertIn("trigger_stage", progress)

    def test_macos_external_protocol_fallback_targets_common_chrome_processes(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        commands = []
        adapter._run_external_protocol_command = lambda command, action_name, env_display=None: commands.append(command) or {
            "command": command,
            "action": action_name,
            "success": True,
        }

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        script = json.dumps(commands, ensure_ascii=False)
        self.assertIn("Google Chrome for Testing", script)
        self.assertIn("Google Chrome", script)
        self.assertIn("Chromium", script)

    def test_macos_external_protocol_strategies_are_ordered(self):
        adapter = WebexAdapter({"adapter_config": {}})

        actions = [action for action, _script in adapter._macos_external_protocol_dismiss_scripts()]

        self.assertEqual(
            actions[:3],
            [
                "external_protocol_prompt_macos_recursive_cancel_Google Chrome for Testing",
                "external_protocol_prompt_macos_recursive_cancel_Google Chrome",
                "external_protocol_prompt_macos_recursive_cancel_Chromium",
            ],
        )
        self.assertEqual(actions[-1], "external_protocol_prompt_macos_escape_final")

    def test_macos_external_protocol_permission_error_is_reported(self):
        adapter = WebexAdapter(
            {
                "adapter_config": {
                    "dismiss_external_protocol_dialog": True,
                    "strict_external_protocol_dismiss": True,
                }
            }
        )
        adapter.page = FakeWebexPage("joined")

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                run.return_value = subprocess_completed(
                    returncode=1,
                    stderr="System Events에 오류 발생: osascript에 보조 접근이 허용되지 않습니다. (-25211)",
                )
                with self.assertRaisesRegex(RuntimeError, "Automation/Accessibility"):
                    asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

    def test_macos_external_protocol_permission_error_is_non_fatal_by_default(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        adapter.page = FakeWebexPage("joined")

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Darwin"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.subprocess.run") as run:
                run.return_value = subprocess_completed(
                    returncode=1,
                    stderr="System Events에 오류 발생: osascript에 보조 접근이 허용되지 않습니다. (-25211)",
                )
                self.assertTrue(asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit")))

    def test_linux_external_protocol_fallback_uses_xdotool_escape(self):
        adapter = WebexAdapter({"adapter_config": {"dismiss_external_protocol_dialog": True}})
        commands = []
        adapter._run_external_protocol_command = lambda command, action_name, env_display=None: commands.append(command) or {
            "command": command,
            "action": action_name,
            "success": True,
        }

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.platform.system", return_value="Linux"):
            with patch("vtc_traffic_generator.vtc_automation.adapters.webex.shutil.which") as which:
                which.side_effect = lambda name: f"/usr/bin/{name}" if name == "xdotool" else None
                asyncio.run(adapter._dismiss_external_protocol_prompt(stage="unit"))

        self.assertIn(["xdotool", "key", "Escape"], commands)

    def test_korean_browser_join_selector_candidates_are_supported(self):
        selectors = WebexAdapter({"adapter_config": {}}).selectors("join_from_browser")

        self.assertIn('button:has-text("이 브라우저에서 참여")', selectors)
        self.assertIn('button:has-text("웹 앱 사용")', selectors)
        self.assertNotIn('button:has-text("Webex 앱 다운로드")', selectors)

    def test_app_download_option_is_not_clicked_when_browser_join_exists(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#browser", "#download", "#join"})

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot"))

        self.assertIn("#browser", adapter.page.clicks)
        self.assertNotIn("#download", adapter.page.clicks)

    def test_webex_download_retry_selector_candidates_are_supported(self):
        adapter = WebexAdapter({"adapter_config": {}})

        self.assertIn('text="Get ready to join"', adapter.selectors("download_page_indicator"))
        self.assertIn('text="Webex Installer.dmg"', adapter.selectors("download_page_indicator"))
        self.assertIn('text="Webex Installer.dmg"', adapter.selectors("installer_download_indicator"))
        self.assertIn('text="Problem joining from browser?"', adapter.selectors("problem_joining_from_browser"))
        self.assertIn("#fallBkJoinByBrowser", adapter.selectors("try_again_browser_join"))
        self.assertIn('button:has-text("Try again")', adapter.selectors("try_again_browser_join"))
        self.assertNotIn('a:has-text("Try again")', adapter.selectors("try_again_browser_join"))
        self.assertIn('button:has-text("Try again")', adapter.selectors("try_again_button"))
        self.assertNotIn('a:has-text("Try again")', adapter.selectors("try_again_button"))
        self.assertIn('button:has-text("Got it")', adapter.selectors("got_it_button"))
        self.assertIn('text="Join on mobile"', adapter.selectors("join_on_mobile_indicator"))
        self.assertIn('text="Download"', adapter.selectors("app_download_indicator"))

    def test_html_text_fallback_detects_download_text_when_visible_text_empty(self):
        adapter = _join_test_adapter("joined")
        adapter.page.text = ""
        adapter.page.html = """
            <html><body>
              <h1>Get ready to join</h1>
              <p>Open &quot;Webex Installer.dmg&quot; after it downloads.</p>
              <button>Got it</button><a>Try again</a>
            </body></html>
        """
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertTrue(state["detected"])
        self.assertIn("Open \"Webex Installer.dmg\" after it downloads", state["visible_text"])
        self.assertIn("webex_page_state_snapshot_done", output.getvalue())

    def test_webex_download_retry_page_indicator_detects_aws_installer_text(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                }
            },
        )
        adapter.page.visible = {"#download-indicator"}
        adapter.page.text = "Cisco Webex Open Webex Installer.dmg after it downloads."

        state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertTrue(state["detected"])
        self.assertEqual(state["indicators"]["download_page_indicator"], "#download-indicator")

    def test_webex_download_retry_page_detects_aws_installer_text_from_html(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        adapter.page.text = ""
        adapter.page.html = "<main>Meeting Webex Open Webex Installer.dmg after it downloads Try again</main>"

        state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertTrue(state["detected"])
        self.assertEqual(state["indicators"]["download_page_indicator"], "page_text")

    def test_webex_download_url_without_visible_download_text_returns_quickly(self):
        adapter = _join_test_adapter("joined")
        adapter.page.url = "https://example.webex.com/meeting/download/test"
        adapter.page.visible = set()
        adapter.page.text = "Loading meeting"

        state = asyncio.run(asyncio.wait_for(adapter._download_retry_page_state(timeout_ms=1), timeout=0.2))

        self.assertFalse(state["detected"])
        self.assertEqual(adapter.page.waits, [])
        self.assertEqual(state["indicators"]["download_url"], "location.href")

    def test_webex_download_detection_does_not_wait_for_download_locator_visibility(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "app_download_indicator": 'text="Download"',
                    "download_page_indicator": 'text="Get ready to join"',
                }
            },
        )
        adapter.page.url = "https://example.webex.com/meeting/download/test"
        adapter.page.text = "Get ready to join Open Webex Installer.dmg after it downloads"
        adapter.page.timeout_selectors.add('text="Download"')

        state = asyncio.run(asyncio.wait_for(adapter._download_retry_page_state(timeout_ms=1), timeout=0.2))

        self.assertTrue(state["detected"])
        self.assertEqual(adapter.page.waits, [])
        self.assertNotIn('text="Download"', adapter.page.waits)

    def test_webex_webclient_frame_snapshot_preempts_download_retry_detection(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        outer.text = "Get ready to join Open Webex Installer.dmg after it downloads"
        frame = FakeWebexFrame("joined", url="https://web.webex.com/meeting/test", name="unified-webclient-iframe")
        frame.visible = {"#name", "#join"}
        frame.text = "Name Join meeting"
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertTrue(result["webclient_frame_detected"])
        self.assertFalse(result["clicked"])
        self.assertEqual(outer.clicks, [])
        self.assertIs(adapter._preferred_webex_meeting_frame, frame)
        self.assertIn("webex_frame_scan_start", output.getvalue())
        self.assertIn("webex_webclient_frame_detected", output.getvalue())

    def test_guest_join_frame_url_is_webclient_candidate_without_dom_snapshot(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text = ""
        frame.text_inputs = []
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertTrue(result["webclient_frame_detected"])
        self.assertTrue(result["webclient_frame_exists"])
        self.assertFalse(result["webclient_frame_ready"])
        self.assertEqual(result["webclient_frame"]["url"], "https://web.webex.com/guest-join-meeting")
        self.assertIn("webex_webclient_frame_candidate", output.getvalue())

    def test_outer_browser_join_wins_over_empty_guest_frame(self):
        adapter = _join_test_adapter("joined", {"webclient_frame_ready_wait_sec": 0.01})
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#browser"}
        outer.text = "Get ready to join"
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertTrue(result["clicked"])
        self.assertIn("#browser", outer.clicks)
        self.assertIn('"action": "click_browser_join"', output.getvalue())
        self.assertNotIn('"action": "continue_webclient_prejoin"', output.getvalue())

    def test_ready_guest_frame_selects_prejoin_and_fills_display_name(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = {'input[type="text"]'}
        frame.text_inputs = ['input[type="text"]']
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            state = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))
            filled = asyncio.run(adapter._fill_display_name_if_needed("bot-ready", timeout_ms=5))

        self.assertTrue(state["webclient_frame_ready"])
        self.assertFalse(state["clicked"])
        self.assertTrue(filled["success"])
        self.assertIn(('input[type="text"]', "bot-ready"), frame.fills)
        self.assertIn('"reason": "ready_webclient_frame"', output.getvalue())

    def test_empty_guest_frame_fails_fast_not_overall_timeout(self):
        adapter = _join_test_adapter("joined", {"webclient_frame_ready_wait_sec": 0.01})
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        outer.text = ""
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "webex_webclient_frame_not_ready"):
                asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertEqual(adapter.diagnostic_stages[-1], "webex_webclient_frame_not_ready")
        self.assertIn("webex_webclient_frame_not_ready", output.getvalue())

    def test_unready_guest_frame_with_try_again_selects_try_again_before_waiting(self):
        adapter = _join_test_adapter("joined", {"webclient_frame_ready_wait_sec": 0.01})
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#fallBkJoinByBrowser"}
        outer.selector_texts["#fallBkJoinByBrowser"] = "Try again"
        outer.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        outer.text = "Problem joining from browser? Try again Join on mobile"
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=5))

        self.assertTrue(result["clicked"])
        self.assertEqual(result["click"]["selector"], "#fallBkJoinByBrowser")
        self.assertIn('"action": "click_try_again"', output.getvalue())
        self.assertNotIn('"action": "wait_for_webclient_frame_ready"', output.getvalue())
        self.assertNotIn("webex_webclient_frame_not_ready", output.getvalue())

    def test_browser_join_problem_text_is_explicit_retry_state(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = set()
        adapter.page.text = "Problem joining from browser? Try again Join on mobile"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertTrue(state["detected"])
        self.assertTrue(state["browser_join_problem_detected"])
        self.assertIn("webex_browser_join_problem_detected", output.getvalue())

    def test_try_again_fallback_selector_js_click_succeeds(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#fallBkJoinByBrowser"}
        adapter.page.selector_texts["#fallBkJoinByBrowser"] = "Try again"
        adapter.page.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        state = {"try_again_action": {"scope": "page", "selector": "#fallBkJoinByBrowser"}}
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            got_it, clicked = asyncio.run(adapter._click_try_again_from_state(state, timeout_ms=5))

        self.assertFalse(got_it)
        self.assertEqual(clicked["method"], "js_candidate_click")
        self.assertEqual(adapter.page.js_candidate_clicks, ["#fallBkJoinByBrowser"])
        self.assertIn("webex_try_again_click_success", output.getvalue())

    def test_try_again_success_rescans_fresh_webclient_frame_for_name_flow(self):
        adapter = _join_test_adapter("joined", {"download_retry_settle_sec": 0})
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#fallBkJoinByBrowser"}
        outer.selector_texts["#fallBkJoinByBrowser"] = "Try again"
        outer.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        outer.text = "Problem joining from browser? Try again Join on mobile"
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]

        def reveal_webclient(_selector):
            frame.url = "https://web.webex.com/meeting/test"
            frame.visible = {"#name", "#join"}
            frame.text_inputs = ["#name"]
            frame.text = "Name Join meeting"

        outer.js_candidate_click_callback = reveal_webclient
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=5))
            fill = asyncio.run(adapter._fill_display_name_if_needed("bot6", timeout_ms=5))

        self.assertTrue(result["clicked"])
        self.assertTrue(fill["success"])
        self.assertIn(("#name", "bot6"), frame.fills)
        self.assertIn("webex_post_try_again_rescan_start", output.getvalue())
        self.assertIn("webex_post_try_again_rescan_result", output.getvalue())

    def test_browser_join_candidate_timeout_has_no_unhandled_future_exception(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#browser"}
        outer.text = "Get ready to join"
        outer.timeout_selectors.add("#browser")
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]

        async def run_click():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            state = await adapter._download_retry_page_state(timeout_ms=1)
            with self.assertRaisesRegex(RuntimeError, "browser_join_click_not_found"):
                await adapter._click_browser_join_from_state(state, timeout_ms=1) or await adapter._raise_browser_join_click_not_found(state)
            await asyncio.sleep(0)
            return unhandled

        self.assertEqual(asyncio.run(run_click()), [])

    def test_browser_join_candidate_click_attempts_exact_selector_scope_first(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#other"}
        frame = FakeWebexFrame("joined", url="https://example.webex.com/meeting/download/frame")
        frame.visible = {"#broadcom-center-right"}
        frame.selector_texts["#broadcom-center-right"] = "Join from this browser"
        frame.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        outer.frames = [outer, frame]
        state = {
            "browser_join_action": {
                "scope": "frame[1]",
                "selector": "#broadcom-center-right",
                "text": "Join from this browser Join from this browser broadcom-center-right",
            }
        }

        result = asyncio.run(adapter._click_browser_join_from_state(state, timeout_ms=5))

        self.assertEqual(result["selector"], "#broadcom-center-right")
        self.assertEqual(result["scope"], "frame[1]")
        self.assertEqual(result["method"], "js_candidate_click")
        self.assertEqual(frame.js_candidate_clicks, ["#broadcom-center-right"])
        self.assertNotIn('button:has-text("Join Meeting")', outer.waits + frame.waits)

    def test_browser_join_candidate_js_click_succeeds_without_broad_final_join_probe(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.visible = {"#broadcom-center-right"}
        outer.selector_texts["#broadcom-center-right"] = "Join from this browser"
        outer.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        state = {
            "browser_join_action": {
                "scope": "page",
                "selector": "#broadcom-center-right",
                "text": "Join from this browser",
            }
        }
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._click_browser_join_from_state(state, timeout_ms=5))

        self.assertTrue(result["ok"])
        self.assertIn("browser_join_click_success", output.getvalue())
        self.assertNotIn('button:has-text("Join Meeting")', outer.waits)
        self.assertNotIn('button:has-text("Join Meeting")', outer.clicks)

    def test_browser_join_candidate_click_fallbacks_are_bounded(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "browser_join_click_total_timeout_sec": 0.05,
                "download_retry_click_total_timeout_sec": 0.05,
            },
        )
        outer = adapter.page
        outer.visible = {"#broadcom-center-right"}
        outer.selector_texts["#broadcom-center-right"] = "Join from this browser"
        outer.js_candidate_click_result = {"ok": False, "reason": "not_visible"}
        outer.timeout_selectors.add("#broadcom-center-right")
        state = {
            "browser_join_action": {
                "scope": "page",
                "selector": "#broadcom-center-right",
                "text": "Join from this browser",
            }
        }
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._click_browser_join_from_state(state, timeout_ms=1))

        self.assertIsNone(result)
        self.assertIn("playwright_locator_click", output.getvalue())
        self.assertIn("playwright_force_click", output.getvalue())
        self.assertIn("browser_join_click_failed", output.getvalue())
        self.assertNotIn('button:has-text("Join Meeting")', outer.waits)

    def test_browser_join_click_does_not_leave_unhandled_future_when_broad_join_meeting_would_timeout(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.visible = {"#broadcom-center-right"}
        outer.selector_texts["#broadcom-center-right"] = "Join from this browser"
        outer.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        outer.timeout_selectors.add('button:has-text("Join Meeting")')
        state = {
            "browser_join_action": {
                "scope": "page",
                "selector": "#broadcom-center-right",
                "text": "Join from this browser",
            }
        }

        async def run_click():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            await adapter._click_browser_join_from_state(state, timeout_ms=1)
            await asyncio.sleep(0)
            return unhandled

        self.assertEqual(asyncio.run(run_click()), [])
        self.assertNotIn('button:has-text("Join Meeting")', outer.waits)

    def test_browser_join_success_rescans_context_pages_for_display_name_flow(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.text = "Get ready to join Join from this browser"
        outer.visible = {"#broadcom-center-right"}
        outer.selector_texts["#broadcom-center-right"] = "Join from this browser"
        outer.js_candidate_click_result = {"ok": True, "method": "js_candidate_click"}
        meeting_page = FakeWebexPage("joined")
        meeting_page.url = "about:blank"
        meeting_page.visible = set()
        meeting_page.text_inputs = ["#name"]
        def reveal_meeting_page(_selector):
            meeting_page.url = "https://web.webex.com/guest-join-meeting/test"
            meeting_page.visible = {"#name", "#join"}
        outer.js_candidate_click_callback = reveal_meeting_page
        adapter.context = FakeContext([outer, meeting_page])

        result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=5))
        fill = asyncio.run(adapter._fill_display_name_if_needed("bot-webex", timeout_ms=5))

        self.assertTrue(result["clicked"])
        self.assertTrue(fill["success"])
        self.assertIn(("#name", "bot-webex"), meeting_page.fills)

    def test_post_browser_join_detached_old_frame_is_rescanned_without_unhandled_exception(self):
        adapter = _join_test_adapter("joined", {"post_browser_join_transition_timeout_sec": 0.3})
        old_frame = DetachedWebexFrame("joined", url="https://example.webex.com/meeting/download/frame")
        adapter.page.frames = [adapter.page, old_frame]
        output = io.StringIO()

        async def run_transition():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            with contextlib.redirect_stdout(output):
                result = await adapter._post_browser_join_transition_loop("bot-webex", timeout_ms=1)
            await asyncio.sleep(0)
            return result, unhandled

        result, unhandled = asyncio.run(run_transition())

        self.assertEqual(unhandled, [])
        self.assertEqual(result["status"], "timeout")
        self.assertIn("webex_frame_detached_during_transition", output.getvalue())
        self.assertIn("webex_post_browser_join_rescan_start", output.getvalue())

    def test_post_browser_join_cancels_pending_browser_join_locator_probe_task(self):
        adapter = _join_test_adapter("joined", {"post_browser_join_transition_timeout_sec": 0.1})
        adapter.page.visible = set()
        adapter.page.text_inputs = []
        cancelled = []

        async def stale_probe():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        async def run_transition():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            adapter._track_probe_task(asyncio.create_task(stale_probe()))
            await asyncio.sleep(0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = await adapter._post_browser_join_transition_loop("bot-webex", timeout_ms=1)
            await asyncio.sleep(0)
            return result, unhandled, output.getvalue()

        result, unhandled, progress = asyncio.run(run_transition())

        self.assertEqual(unhandled, [])
        self.assertTrue(cancelled)
        self.assertEqual(result["status"], "timeout")
        self.assertIn("webex_stale_probe_tasks_cancelled", progress)

    def test_post_browser_join_fresh_meeting_frame_continues_to_final_join(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        frame = FakeWebexFrame("joined", url="https://web.webex.com/meeting/test")
        frame.visible = {"#name", "#join"}
        frame.text_inputs = ["#name"]
        frame.text = "Name Join"
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._post_browser_join_transition_loop("bot-webex", timeout_ms=5))

        self.assertEqual(result["status"], "final_join")
        self.assertIn(("#name", "bot-webex"), frame.fills)
        self.assertIn("webex_post_browser_join_transition_start", output.getvalue())
        self.assertIn("webex_post_browser_join_rescan_result", output.getvalue())

    def test_post_browser_join_joined_waiting_state_returns_success_state(self):
        adapter = _join_test_adapter("waiting_for_others")
        adapter.page.visible = {"#waiting"}
        adapter.page.text = "Waiting for others to join"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._post_browser_join_transition_loop("bot-webex", timeout_ms=1))

        self.assertEqual(result["status"], "waiting_for_others")
        self.assertTrue(result["joined"])
        self.assertIn("webex_post_browser_join_transition_start", output.getvalue())

    def test_post_browser_join_timeout_is_bounded_specific_diagnostic(self):
        adapter = _join_test_adapter("joined", {"post_browser_join_transition_timeout_sec": 0.05})
        adapter.page.visible = set()
        adapter.page.text_inputs = []
        adapter.page.text = "Get ready to join"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._post_browser_join_transition_loop("bot-webex", timeout_ms=1))

        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["stage"], "webex_post_browser_join_transition_timeout")
        self.assertEqual(adapter.diagnostic_stages[-1], "webex_post_browser_join_transition_timeout")
        self.assertIn("webex_post_browser_join_transition_timeout", output.getvalue())

    def test_visible_button_scope_wins_over_url_only_frame_score(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        frame0 = FakeWebexFrame("joined", url="https://example.webex.com/meeting/download/outer")
        frame0.visible = {"#browser", "#got-it", "#continue"}
        frame0.selector_texts["#browser"] = "Join from browser"
        frame0.text = "Get ready to join"
        frame2 = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame2.visible = set()
        frame2.text_inputs = []
        outer.frames = [outer, frame0, FakeWebexFrame("joined", url="https://example.webex.com/blank"), frame2]

        state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertFalse(state["webclient_frame_ready"])
        self.assertEqual(state["browser_join_action"]["scope"], "frame[1]")
        self.assertEqual(state["browser_join_action"]["selector"], "#browser")

    def test_outer_download_shell_browser_join_is_not_ready_webclient_frame(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#browser"}
        outer.selector_texts["#browser"] = "Join from this browser"
        outer.text = "Get ready to join Join from this browser"

        state = asyncio.run(adapter._download_retry_page_state(timeout_ms=1))

        self.assertFalse(state["webclient_frame_ready"])
        self.assertFalse(state["webclient_frame_detected"])
        self.assertEqual(state["browser_join_action"]["selector"], "#browser")

    def test_repeated_empty_display_name_frame_attempt_is_skipped(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.visible = set()
        outer.text_inputs = []
        frame = FakeWebexFrame("joined", url="https://example.webex.com/meeting/prejoin")
        frame.visible = set()
        frame.text_inputs = []
        frame.text = "meeting"
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            first = asyncio.run(adapter._fill_display_name("bot", timeout_ms=1))
            second = asyncio.run(adapter._fill_display_name("bot", timeout_ms=1))

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertIn("empty_frame_already_attempted", output.getvalue())

    def test_guest_join_text_snapshot_timeout_still_probes_frame_input(self):
        adapter = _join_test_adapter("joined", {"frame_snapshot_timeout_sec": 0.001})
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = set()
        frame = TextSnapshotTimeoutFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = {'input[type="text"]'}
        frame.text_inputs = ['input[type="text"]']
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))
            filled = asyncio.run(adapter._fill_display_name_if_needed("bot-frame", timeout_ms=5))

        self.assertTrue(result["webclient_frame_detected"])
        self.assertTrue(filled["success"])
        self.assertIn(('input[type="text"]', "bot-frame"), frame.fills)
        self.assertIn("frame_snapshot_timeout", output.getvalue())
        self.assertIn("webex_display_name_input_probe_start", output.getvalue())

    def test_display_name_frame_input_type_text_is_filled(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = {'input[type="text"]'}
        frame.text_inputs = ['input[type="text"]']
        outer.frames = [outer, frame]

        result = asyncio.run(adapter._fill_display_name("typed-bot", timeout_ms=5))

        self.assertEqual(result, 'input[type="text"]')
        self.assertIn(('input[type="text"]', "typed-bot"), frame.fills)

    def test_display_name_frame_mdc_input_is_filled(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = {'mdc-input input:not([type="hidden"])'}
        frame.text_inputs = ['mdc-input input:not([type="hidden"])']
        outer.frames = [outer, frame]

        result = asyncio.run(adapter._fill_display_name("mdc-bot", timeout_ms=5))

        self.assertEqual(result, 'mdc-input input:not([type="hidden"])')
        self.assertIn(('mdc-input input:not([type="hidden"])', "mdc-bot"), frame.fills)

    def test_display_name_probe_ignores_hidden_webex_form_inputs(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#name"}
        adapter.page.text_inputs = ["#hidden-return", "#name"]
        adapter.page.input_attrs["#hidden-return"] = {"type": "hidden", "name": "Par_ReturnURL"}
        adapter.page.input_attrs["#name"] = {"type": "text", "name": "displayName"}

        with patch("asyncio.create_task") as create_task:
            result = asyncio.run(adapter._fill_display_name("visible-bot", timeout_ms=1))

        self.assertTrue(result)
        self.assertNotIn(("#hidden-return", "visible-bot"), adapter.page.fills)
        self.assertIn(("#name", "visible-bot"), adapter.page.fills)
        self.assertNotIn("#hidden-return", adapter.page.input_value_calls)
        create_task.assert_not_called()

    def test_display_name_input_selectors_exclude_plain_hidden_input_patterns(self):
        selectors = WebexAdapter({"adapter_config": {}})._display_name_input_selectors()

        self.assertIn('input:not([type="hidden"])[name*="name" i]', selectors)
        self.assertNotIn('input[name*="name" i]', selectors)

    def test_guest_join_frame_without_fillable_input_fails_fast_with_diagnostic(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.visible = set()
        outer.text_inputs = []
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        outer.frames = [outer, frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, "webex_display_name_input_not_found"):
                asyncio.run(adapter._fill_display_name("missing-bot", timeout_ms=5))

        self.assertEqual(adapter.diagnostic_stages[-1], "webex_display_name_input_not_found")
        self.assertIn("webex_display_name_input_not_found", output.getvalue())

    def test_textarea_probe_timeout_has_no_unhandled_future_exception(self):
        adapter = _join_test_adapter("joined")
        outer = adapter.page
        outer.visible = set()
        outer.text_inputs = []
        frame = FakeWebexFrame("joined", url="https://web.webex.com/guest-join-meeting")
        frame.visible = set()
        frame.text_inputs = []
        frame.timeout_selectors.add("textarea")
        outer.frames = [outer, frame]

        async def run_fill():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            with self.assertRaisesRegex(RuntimeError, "webex_display_name_input_not_found"):
                await adapter._fill_display_name("timeout-bot", timeout_ms=5)
            await asyncio.sleep(0)
            return unhandled

        self.assertEqual(asyncio.run(run_fill()), [])

    def test_webex_page_state_detection_timeout_has_no_unhandled_future_exception(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "page_state_detection_timeout_sec": 0.01,
                "frame_snapshot_timeout_sec": 0.01,
            },
        )
        adapter.page = SlowSnapshotPage("joined")

        async def slow_page_state_snapshot(timeout_ms=None):
            await asyncio.sleep(1)
            return {}

        adapter._page_state_snapshot = slow_page_state_snapshot

        async def run_detection():
            loop = asyncio.get_running_loop()
            unhandled = []
            loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
            state = await adapter._download_retry_page_state(timeout_ms=1)
            await asyncio.sleep(0)
            return state, unhandled

        state, unhandled = asyncio.run(run_detection())

        self.assertTrue(state["classification_timeout"])
        self.assertEqual(adapter.diagnostic_stages[-1], "webex_page_state_detection_timeout")
        self.assertEqual(unhandled, [])

    def test_webex_download_retry_page_clicks_try_again_and_reaches_name_fill(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_settle_sec": 0,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                    "try_again_browser_join": "#try-again",
                    "got_it_button": "#got-it",
                    "app_download_indicator": "#download-button",
                    "join_on_mobile_indicator": "#mobile",
                }
            },
        )
        adapter.page.visible = {"#download-indicator", "#problem", "#try-again", "#download-button", "#mobile"}
        adapter.page.try_again_reveals = {"#name", "#join"}
        adapter.page.enable_join_on_name_fill = True
        adapter.page.text = (
            "Open Webex Installer.dmg after it downloads. "
            "Problem joining from browser? Try again Join on mobile Download Webex"
        )

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot-aws-4"))

        self.assertTrue(any(click == "#try-again" or "Try again" in str(click) for click in adapter.page.clicks))
        self.assertIn(("#name", "bot-aws-4"), adapter.page.fills)
        self.assertIn("#join", adapter.page.clicks)
        self.assertNotIn("#download-button", adapter.page.clicks)
        self.assertNotIn("#mobile", adapter.page.clicks)

    def test_webex_download_retry_page_clicks_got_it_if_present(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "got_it_button": "#got-it",
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                    "try_again_browser_join": "#try-again",
                }
            },
        )
        adapter.page.visible = {"#got-it", "#download-indicator", "#problem", "#try-again"}
        adapter.page.try_again_reveals = {"#join"}

        asyncio.run(adapter._run_prejoin_transition_loop("bot"))

        self.assertIn("#got-it", adapter.page.clicks)

    def test_webex_download_retry_page_clicks_try_again_after_got_it(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_settle_sec": 0,
                "selectors": {
                    "got_it_button": "#got-it",
                    "download_page_indicator": "#download-indicator",
                    "try_again_button": "#try-again",
                    "try_again_browser_join": "#try-again",
                },
            },
        )
        adapter.page.visible = {"#got-it", "#download-indicator", "#try-again"}
        adapter.page.text = "Get ready to join Open Webex Installer.dmg after it downloads Try again Got it"

        asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertLess(adapter.page.clicks.index("#got-it"), adapter.page.clicks.index("#try-again"))

    def test_webex_download_retry_missing_got_it_is_skipped_and_try_again_attempted(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_settle_sec": 0,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "try_again_button": "#try-again",
                    "try_again_browser_join": "#try-again",
                },
            },
        )
        adapter.page.visible = {"#download-indicator", "#try-again"}
        adapter.page.text = "Get ready to join Open Webex Installer.dmg after it downloads Try again"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertTrue(result["clicked"])
        self.assertFalse(result["got_it_clicked"])
        self.assertIn("#try-again", adapter.page.clicks)
        self.assertIn("webex_got_it_click_skipped", output.getvalue())
        self.assertIn("webex_try_again_click_attempt", output.getvalue())

    def test_webex_download_retry_skips_hidden_try_again_when_webclient_frame_loaded(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_settle_sec": 0,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                    "try_again_browser_join": "#hidden-try-again",
                    "join_button": "#join",
                    "display_name": "#name",
                },
            },
        )
        outer = adapter.page
        outer.url = "https://example.webex.com/meeting/download/test"
        outer.visible = {"#download-indicator"}
        outer.text = "Get ready to join Open Webex Installer.dmg after it downloads"
        outer.html = """
            <div role="dialog" aria-label="Problem joining from browser?" style="display: none;">
              <button id="got-it">Got it</button>
              <button id="fallBkJoinByBrowser"><span>Try again</span></button>
            </div>
        """
        frame = FakeWebexFrame("joined", url="https://web.webex.com/meeting", name="unified-webclient-iframe")
        frame.visible = {"#name", "#join"}
        frame.text = "Name Join meeting"
        frame.enable_join_on_name_fill = True
        outer.frames = [outer, FakeWebexFrame("joined", url="https://example.webex.com/blank"), frame]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))
            filled = asyncio.run(adapter._fill_display_name_if_needed("bot-aws-4", timeout_ms=1))

        self.assertTrue(result["webclient_frame_detected"])
        self.assertTrue(filled["success"])
        self.assertIn(("#name", "bot-aws-4"), frame.fills)
        self.assertNotIn("#hidden-try-again", outer.clicks)
        self.assertEqual(adapter.diagnostic_stages, [])
        self.assertIn("webex_webclient_frame_detected", output.getvalue())

    def test_webex_download_retry_korean_confirm_timeout_is_caught_without_future_leak(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_settle_sec": 0,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "try_again_button": "#try-again",
                    "try_again_browser_join": "#try-again",
                },
            },
        )
        adapter.page.visible = {"#download-indicator", "#try-again"}
        adapter.page.text = "Get ready to join Open Webex Installer.dmg after it downloads 확인 Try again"
        adapter.page.timeout_selectors.add("text=확인")

        with patch("asyncio.create_task") as create_task:
            result = asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertTrue(result["clicked"])
        self.assertFalse(result["got_it_clicked"])
        self.assertIn("#try-again", adapter.page.clicks)
        self.assertIn("text=확인", adapter.page.waits)
        self.assertNotIn('button:has-text("확인")', adapter.page.waits)
        create_task.assert_not_called()

    def test_webex_download_retry_try_again_is_attempted_after_got_it_skip(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "try_again_browser_join": "#try-again",
                },
            },
        )
        adapter.page.visible = {"#download-indicator", "#try-again"}
        adapter.page.text = "Open Webex Installer.dmg after it downloads Try again"

        asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        got_it_waits = [selector for selector in adapter.page.waits if "Got it" in selector or "확인" in selector]
        self.assertTrue(got_it_waits)
        self.assertIn("#try-again", adapter.page.clicks)

    def test_webex_download_retry_clicks_try_again_as_div_or_span(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {'div:has-text("Try again")'}

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["selector"], 'div:has-text("Try again")')
        self.assertIn('div:has-text("Try again")', adapter.page.clicks)

    def test_webex_download_retry_clicks_try_again_role_button(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {"#try-again-role"}
        adapter.page.text_roles = {"button": {"Try again": "#try-again-role"}}
        adapter.page.selector_texts["#try-again-role"] = "Try again"
        adapter.page.roles["#try-again-role"] = "button"

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["selector"], "role=button[name=/Try\\ again/i]")
        self.assertIn("#try-again-role", adapter.page.clicks)

    def test_webex_download_retry_clicks_got_it_as_plain_span(self):
        adapter = _join_test_adapter("joined")
        adapter.page.text = "Got it"

        result = asyncio.run(adapter._click_got_it_button(timeout_ms=1))

        self.assertEqual(result["selector"], 'get_by_text("Got it", exact=True)')
        self.assertIn("text=Got it", adapter.page.clicks)

    def test_webex_download_retry_js_text_fallback_clicks_shadow_candidate(self):
        adapter = _join_test_adapter("joined")
        adapter.page.js_text_action_result = {
            "ok": True,
            "method": "js_text_click",
            "text": "Try again",
            "tag": "SPAN",
            "role": "button",
            "shadow": True,
        }

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["method"], "js_text_click")
        self.assertTrue(result["shadow"])
        self.assertLessEqual(len(adapter.page.waits), 1)

    def test_webex_download_retry_try_again_uses_js_before_locator_scanning(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {'button:has-text("Try again")'}
        adapter.page.js_text_action_result = {
            "ok": True,
            "method": "js_text_click",
            "text": "Try again",
            "selector": "js_text_action",
            "tag": "BUTTON",
        }

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["method"], "js_text_click")
        self.assertEqual(adapter.page.waits, [])

    def test_webex_download_retry_coordinate_fallback_only_for_allowed_texts(self):
        adapter = _join_test_adapter("joined")
        adapter.page.text = "Try again Download"
        adapter.page.rects["text=Try again"] = {"x": 10, "y": 20, "width": 40, "height": 20}

        result = asyncio.run(
            adapter._coordinate_text_action_click(
                ["Try again"],
                forbidden_texts=adapter._download_retry_forbidden_texts(),
                exact=True,
                timeout_ms=1,
            )
        )

        self.assertEqual(result["method"], "coordinate_text_click")
        self.assertEqual(adapter.page.mouse.clicks, [(30.0, 30.0)])
        self.assertFalse(adapter._coordinate_text_fallback_allowed(["Download"]))

    def test_webex_download_retry_optional_click_does_not_create_wait_tasks(self):
        adapter = _join_test_adapter("joined")
        adapter.page.text = "Open Webex Installer.dmg after it downloads"

        with patch("asyncio.create_task") as create_task:
            result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertIsNone(result)
        create_task.assert_not_called()

    def test_webex_download_retry_dom_click_does_not_click_download_or_mobile(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "try_again_browser_join": ["#download-button", "#mobile"],
                }
            },
        )
        adapter.page.visible = {"#download-button", "#mobile"}
        adapter.page.dom_try_again_result = {"selector": "dom-near-problem-text", "text": "Try again", "tag": "A"}

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["method"], "js_text_click")
        self.assertEqual(result["selector"], "dom-near-problem-text")
        self.assertNotIn("#download-button", adapter.page.clicks)
        self.assertNotIn("#mobile", adapter.page.clicks)

    def test_webex_download_retry_fallback_skips_unsafe_buttons(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "try_again_browser_join": [
                        'button:has-text("Download Webex")',
                        'button:has-text("Join on mobile")',
                        'button:has-text("Try again")',
                    ],
                }
            },
        )
        adapter.page.visible = {
            'button:has-text("Download Webex")',
            'button:has-text("Join on mobile")',
            'button:has-text("Try again")',
        }

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["selector"], 'button:has-text("Try again")')
        self.assertNotIn('button:has-text("Download Webex")', adapter.page.clicks)
        self.assertNotIn('button:has-text("Join on mobile")', adapter.page.clicks)

    def test_webex_download_retry_try_again_not_rejected_by_broad_body_forbidden_text(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible = {'button:has-text("Try again")'}
        adapter.page.text = (
            "Get ready to join Open Webex Installer.dmg after it downloads "
            "Try again Join on mobile Download Webex"
        )
        adapter.page.container_texts['button:has-text("Try again")'] = "Try again"
        details = {
            "candidate_text": "Try again",
            "clickable_target_text": "Try again",
            "broad_context_text": adapter.page.text,
            "container_text": adapter.page.text,
        }

        self.assertIsNone(
            adapter._text_action_rejected_reason(
                details,
                ["Try again", "Retry", "다시 시도"],
                adapter._download_retry_forbidden_texts(),
                exact=True,
            )
        )

        result = asyncio.run(adapter._click_try_again_browser_join(timeout_ms=1))

        self.assertEqual(result["text"], "Try again")
        self.assertTrue(any("Try again" in str(click) for click in adapter.page.clicks))

    def test_webex_download_retry_slow_try_again_raises_specific_diagnostic_fast(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "download_retry_click_total_timeout_sec": 0.01,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                },
            },
        )
        adapter.page.visible = {"#download-indicator", "#problem", "#try-again"}
        adapter.page.text = "Open Webex Installer.dmg after it downloads. Problem joining from browser? Try again"

        async def slow_try_again(timeout_ms=None):
            await asyncio.sleep(1)
            return None

        adapter._click_try_again_browser_join = slow_try_again

        with self.assertRaisesRegex(RuntimeError, "Try again was not clickable"):
            asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertEqual(adapter.diagnostic_stages[-1], "webex_download_retry_try_again_not_clickable")
        self.assertTrue(adapter.diagnostic_extras[-1]["download_retry"]["click_timeout"])
        self.assertEqual(adapter.diagnostic_extras[-1]["download_retry"]["click_total_timeout_sec"], 0.01)

    def test_webex_download_retry_click_failure_uses_specific_diagnostic_stage(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "max_download_retry_attempts": 1,
                "download_retry_settle_sec": 0,
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                    "try_again_browser_join": "#missing-try-again",
                    "try_again_button": "#missing-try-again",
                }
            },
        )
        adapter.page.visible = {"#download-indicator", "#problem"}
        adapter.page.text = "Open Webex Installer.dmg after it downloads. Problem joining from browser?"

        with self.assertRaisesRegex(RuntimeError, "no_actionable_webex_state"):
            asyncio.run(adapter._run_prejoin_transition_loop("bot"))

        self.assertEqual(adapter.diagnostic_stages[-1], "no_actionable_webex_state")
        self.assertIn("links_buttons_debug", adapter.diagnostic_extras[-1])
        self.assertIn("download_retry", adapter.diagnostic_extras[-1])

    def test_webex_download_retry_try_again_failure_raises_specific_stage(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "download_page_indicator": "#download-indicator",
                    "problem_joining_from_browser": "#problem",
                    "try_again_browser_join": "#missing-try-again",
                    "try_again_button": "#missing-try-again",
                }
            },
        )
        adapter.page.visible = {"#download-indicator", "#problem"}
        adapter.page.text = "Open Webex Installer.dmg after it downloads. Problem joining from browser?"

        with self.assertRaisesRegex(RuntimeError, "no_actionable_webex_state"):
            asyncio.run(adapter._handle_download_retry_page(timeout_ms=1))

        self.assertEqual(adapter.diagnostic_stages[-1], "no_actionable_webex_state")

    def test_optional_display_name_selector_timeout_is_caught(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "display_name": 'input[name*="name" i]',
                    "name_input": 'input[name*="name" i]',
                }
            },
        )
        adapter.page.visible = set()

        result = asyncio.run(adapter._find_display_name_input(timeout_ms=1))

        self.assertIsNone(result)

    def test_korean_name_input_selector_candidates_are_supported(self):
        selectors = WebexAdapter({"adapter_config": {}}).selectors("display_name")

        self.assertIn('input:not([type="hidden"])[aria-label*="이름" i]', selectors)
        self.assertIn('input:not([type="hidden"])[placeholder*="참가자" i]', selectors)
        self.assertIn('input:not([type="hidden"])[aria-label*="이름을 입력" i]', selectors)

    def test_korean_final_join_button_selector_candidates_are_supported(self):
        selectors = WebexAdapter({"adapter_config": {}}).selectors("join_button")

        self.assertIn('button:has-text("미팅 참여")', selectors)
        self.assertIn('button:has-text("참가")', selectors)

    def test_disabled_join_button_retries_display_name_before_failing(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})
        adapter.page.disabled.add("#join")

        with self.assertRaisesRegex(RuntimeError, "final join button"):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "retry-name"))

        self.assertGreaterEqual(adapter.page.fills.count(("#name", "retry-name")), 1)
        self.assertEqual(adapter.diagnostic_stages[-1], "webex_join_button_disabled")
        self.assertIn("input_debug", adapter.diagnostic_extras[-1])
        self.assertIn("visible_text", adapter.diagnostic_extras[-1])

    def test_progress_logging_emits_display_name_and_final_join_stages(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "progress-bot"))

        progress = output.getvalue()
        self.assertIn("display_name_fill_attempt", progress)
        self.assertIn("display_name_fill_success", progress)
        self.assertIn("visible_text_input_count", progress)
        self.assertIn("fill_method", progress)
        self.assertIn("final_join_button_seen", progress)
        self.assertIn("final_join_clicked", progress)

    def test_join_result_timeout_fails_with_diagnostics(self):
        adapter = _join_test_adapter("missing_result")
        adapter.page.visible.update({"#name", "#join"})

        with self.assertRaisesRegex(RuntimeError, "status timeout"):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "timeout-bot"))

        self.assertEqual(adapter.diagnostic_stages[-1], "webex_post_final_join_result_timeout")
        self.assertIn("visible_text", adapter.diagnostic_extras[-1]["join_result"])

    def test_korean_name_page_text_triggers_display_name_fill(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})
        adapter.page.text = "이름을 입력하고 참여하십시오. 이름 *"

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "bot-local-1"))

        self.assertEqual(adapter.page.values["#name"], "bot-local-1")

    def test_korean_name_label_candidate_is_supported(self):
        adapter = _join_test_adapter("joined", {"selectors": {"display_name": "#missing", "name_input": "#missing"}})
        adapter.page.visible.update({"#label-name", "#join"})
        adapter.page.labels["이름 *"] = "#label-name"

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "label-bot"))

        self.assertEqual(adapter.page.values["#label-name"], "label-bot")

    def test_single_visible_text_input_fallback_is_used(self):
        adapter = _join_test_adapter("joined", {"selectors": {"display_name": "#missing", "name_input": "#missing"}})
        adapter.page.visible.update({"#only-input", "#join"})
        adapter.page.text_inputs = ["#only-input"]

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "fallback-bot"))

        self.assertEqual(adapter.page.values["#only-input"], "fallback-bot")

    def test_role_textbox_candidate_is_used(self):
        adapter = _join_test_adapter("joined", {"selectors": {"display_name": "#missing", "name_input": "#missing"}})
        adapter.page.visible.update({"#role-name", "#join"})
        adapter.page.role_textboxes = ["#role-name"]
        adapter.page.input_attrs["#role-name"] = {"role": "textbox", "aria-label": "이름 *"}

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "role-bot"))

        self.assertEqual(adapter.page.values["#role-name"], "role-bot")

    def test_active_element_fallback_works_when_name_input_is_focused(self):
        adapter = _join_test_adapter("joined", {"selectors": {"display_name": "#missing", "name_input": "#missing"}})
        adapter.page.visible.update({"#focused-name", "#join"})
        adapter.page.focused_selector = "#focused-name"
        adapter.page.text_inputs = ["#other-input", "#second-input"]

        result = asyncio.run(adapter._fill_display_name("focused-bot", timeout_ms=1))

        self.assertEqual(result, ':focus:is(input:not([type]), input[type="text"], input[type="search"], textarea, [role="textbox"], [contenteditable="true"])')
        self.assertEqual(adapter.page.values["#focused-name"], "focused-bot")

    def test_display_name_fill_success_is_verified(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.add("#name")

        result = asyncio.run(adapter._fill_display_name("verified-bot", timeout_ms=1))

        self.assertTrue(result)
        self.assertEqual(adapter.page.input_value_calls[-1], "#name")
        self.assertEqual(adapter.page.values["#name"], "verified-bot")

    def test_keyboard_fallback_is_attempted_when_locator_fill_does_not_update_value(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.add("#name")
        adapter.page.fill_updates_value = False

        result = asyncio.run(adapter._fill_display_name("keyboard-bot", timeout_ms=1))

        self.assertTrue(result)
        self.assertIn(("press", "Meta+A" if platform.system() == "Darwin" else "Control+A"), adapter.page.keyboard.actions)
        self.assertIn(("type", "keyboard-bot"), adapter.page.keyboard.actions)
        self.assertEqual(adapter.page.values["#name"], "keyboard-bot")

    def test_js_fallback_is_attempted_when_keyboard_type_does_not_update_value(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.add("#name")
        adapter.page.fill_updates_value = False
        adapter.page.keyboard_updates_value = False

        result = asyncio.run(adapter._fill_display_name("js-bot", timeout_ms=1))

        self.assertTrue(result)
        self.assertIn(("#name", "js-bot"), adapter.page.js_sets)
        self.assertEqual(adapter.page.values["#name"], "js-bot")

    def test_disabled_final_join_retries_name_fill_then_clicks_when_enabled(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})
        adapter.page.disabled.add("#join")
        adapter.page.enable_join_on_name_fill = True

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "enable-bot"))

        self.assertIn("#join", adapter.page.clicks)
        self.assertEqual(adapter.page.values["#name"], "enable-bot")

    def test_disabled_final_join_does_not_click_until_enabled(self):
        adapter = _join_test_adapter("joined")
        adapter.page.visible.update({"#name", "#join"})
        adapter.page.disabled.add("#join")

        with self.assertRaisesRegex(RuntimeError, "final join button"):
            asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "disabled-bot"))

        self.assertNotIn("#join", adapter.page.clicks)

    def test_name_fill_failed_diagnostics_include_input_debug(self):
        adapter = _join_test_adapter("joined", {"selectors": {"display_name": "#missing", "name_input": "#missing"}})
        adapter.page.visible.add("#join")

        result = asyncio.run(adapter._fill_display_name_if_needed("missing-bot", timeout_ms=1, diagnose=True))

        self.assertFalse(result["success"])
        self.assertEqual(adapter.diagnostic_stages[-1], "webex_name_fill_failed")
        self.assertIn("input_debug", adapter.diagnostic_extras[-1])

    def test_name_input_alias_still_fills_display_name(self):
        adapter = _join_test_adapter(
            "joined",
            {
                "selectors": {
                    "name_input": "#alias-name",
                }
            },
        )
        adapter.page.visible.update({"#alias-name", "#join"})

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "alias-bot"))

        self.assertIn(("#alias-name", "alias-bot"), adapter.page.fills)

    def test_prejoin_controls_inside_webex_iframe_are_used(self):
        adapter = _join_test_adapter("joined")
        frame = FakeWebexPage("joined")
        frame.visible.update({"#name", "#join"})
        adapter.page = FakeWebexPage("joined")
        adapter.page.visible = set()
        adapter.page.frames = [frame]

        asyncio.run(adapter.connect_to_meeting("https://example.webex.com/meet/test", "frame-bot"))

        self.assertIn(("#name", "frame-bot"), frame.fills)
        self.assertIn("#join", frame.clicks)

    def test_unmute_microphone_clicks_off_indicator_and_updates_after_on_verification(self):
        adapter = _control_test_adapter({"mic": False})

        self.assertTrue(asyncio.run(adapter.unmute_microphone()))

        self.assertEqual(adapter.page.clicks, ["#mic-off"])
        self.assertTrue(adapter.mic_enabled)

    def test_mute_microphone_clicks_on_indicator_and_updates_after_off_verification(self):
        adapter = _control_test_adapter({"mic": True})

        self.assertTrue(asyncio.run(adapter.mute_microphone()))

        self.assertEqual(adapter.page.clicks, ["#mic-on"])
        self.assertFalse(adapter.mic_enabled)

    def test_microphone_no_click_when_already_desired_state(self):
        adapter = _control_test_adapter({"mic": True})

        self.assertTrue(asyncio.run(adapter.unmute_microphone()))

        self.assertEqual(adapter.page.clicks, [])
        self.assertTrue(adapter.mic_enabled)

    def test_start_camera_clicks_off_indicator_and_updates_after_on_verification(self):
        adapter = _control_test_adapter({"camera": False})

        self.assertTrue(asyncio.run(adapter.start_camera()))

        self.assertEqual(adapter.page.clicks, ["#camera-off"])
        self.assertTrue(adapter.camera_enabled)

    def test_stop_camera_clicks_on_indicator_and_updates_after_off_verification(self):
        adapter = _control_test_adapter({"camera": True})

        self.assertTrue(asyncio.run(adapter.stop_camera()))

        self.assertEqual(adapter.page.clicks, ["#camera-on"])
        self.assertFalse(adapter.camera_enabled)

    def test_cached_state_not_updated_when_post_click_verification_fails_without_trust(self):
        adapter = _control_test_adapter({"mic": False}, transitions_enabled=False)
        adapter.mic_enabled = False

        with self.assertRaisesRegex(RuntimeError, "microphone state could not be verified"):
            asyncio.run(adapter.unmute_microphone())

        self.assertEqual(adapter.page.clicks, ["#mic-off"])
        self.assertFalse(adapter.mic_enabled)
        self.assertEqual(adapter.diagnostic_stages, ["webex_microphone_state_unverified"])

    def test_cached_state_can_be_updated_with_warning_when_unverified_trust_enabled(self):
        events = []
        adapter = _control_test_adapter(
            {"mic": False},
            transitions_enabled=False,
            extra_adapter_config={"trust_click_state_after_unverified_action": True},
        )
        adapter.mic_enabled = False

        with patch("vtc_traffic_generator.vtc_automation.adapters.webex.emit_event") as emit:
            emit.side_effect = lambda config, event, details, service=None: events.append(event)
            self.assertTrue(asyncio.run(adapter.unmute_microphone()))

        self.assertTrue(adapter.mic_enabled)
        self.assertIn("webex_control_state_unverified", events)
        self.assertIn("webex_control_state_trusted_after_unverified_action", events)

    def test_start_screen_share_clicks_button_and_verifies_before_cache_update(self):
        adapter = _control_test_adapter({"screen": False})

        self.assertTrue(asyncio.run(adapter.start_screen_share()))

        self.assertEqual(adapter.page.clicks, ["#share-button"])
        self.assertTrue(adapter.screen_sharing)

    def test_stop_screen_share_clicks_stop_and_verifies_inactive_before_cache_update(self):
        adapter = _control_test_adapter({"screen": True})

        self.assertTrue(asyncio.run(adapter.stop_screen_share()))

        self.assertEqual(adapter.page.clicks, ["#share-stop"])
        self.assertFalse(adapter.screen_sharing)


if __name__ == "__main__":
    unittest.main()


def subprocess_completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakePlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeChromium:
    def __init__(self):
        self.persistent_user_data_dir = None
        self.persistent_options = None

    async def launch_persistent_context(self, user_data_dir, **options):
        self.persistent_user_data_dir = user_data_dir
        self.persistent_options = options
        return FakePersistentContext()


class FakePersistentContext:
    browser = None

    def on(self, event_name, callback):
        return None

    async def new_page(self):
        return FakeWebexPage("joined")

    async def close(self):
        return None


class FakeSmokeAdapter:
    def __init__(self):
        self.diagnostic_stages = []
        self.closed = False

    async def launch(self):
        return None

    async def connect_to_meeting(self, vtc_url, display_name):
        await asyncio.sleep(60)

    async def is_in_meeting(self):
        return False

    async def leave(self):
        return True

    async def close(self):
        self.closed = True

    async def collect_diagnostics(self, stage=None, extra=None):
        self.diagnostic_stages.append(stage)
        return {"stage": stage, "files": {"metadata": "/tmp/fake.metadata.json"}}


class FakeJoinedSmokeAdapter:
    def __init__(self, join_result):
        self.join_result = join_result
        self.left = False
        self.closed = False

    async def launch(self):
        return None

    async def connect_to_meeting(self, vtc_url, display_name):
        return self.join_result

    async def is_in_meeting(self):
        return True

    async def leave(self):
        self.left = True
        return True

    async def close(self):
        self.closed = True


class FakeWebexLocator:
    def __init__(self, page, selector, matches=None):
        self.page = page
        self.selector = selector
        self.matches = matches

    @property
    def first(self):
        if self.matches:
            return FakeWebexLocator(self.page, self.matches[0])
        return self

    def nth(self, index):
        if self.matches:
            return FakeWebexLocator(self.page, self.matches[index])
        return self

    async def count(self):
        if self.matches is not None:
            return len(self.matches)
        return 1

    async def wait_for(self, state="visible", timeout=0):
        self.page.waits.append(self.selector)
        if self.selector in self.page.timeout_selectors:
            raise PlaywrightTimeoutError(f"Timeout waiting for {self.selector} to be visible")
        if self.selector in self.page.visible or self.page.selector_text_visible(self.selector):
            return None
        raise PlaywrightTimeoutError(f"{self.selector} is not visible")

    async def click(self, **kwargs):
        if self.selector in self.page.timeout_selectors:
            raise PlaywrightTimeoutError(f"Timeout clicking {self.selector}")
        self.page.clicks.append(self.selector)
        self.page.focused_selector = self.selector
        self.page.select_all = False
        if self.selector == "#got-it" or "Got it" in self.selector or "확인" in self.selector or "알겠습니다" in self.selector:
            self.page.visible.discard("#got-it")
        if self.selector == "#try-again" or "Try again" in self.selector or "Retry" in self.selector or "다시 시도" in self.selector:
            self.page.visible.discard("#download-indicator")
            self.page.visible.discard("#problem")
            self.page.visible.discard("#try-again")
            self.page.visible.discard("#download-button")
            self.page.visible.discard("#mobile")
            self.page.visible.update(self.page.try_again_reveals)
            self.page.text = f"visible {self.page.join_result} screen"
        if self.selector == "#join":
            self.page.visible.discard("#join")
            self.page.visible.add(f"#{self.page.join_result}")
            if self.page.join_result == "waiting_for_others":
                self.page.visible.add("#joined")
            post_click_text = {
                "waiting_for_others": "Waiting for others to join",
                "waiting_for_host": "Waiting for the host",
                "lobby": "You're in the lobby",
            }.get(self.page.join_result)
            if post_click_text:
                self.page.text = post_click_text
            if self.page.title_after_join is not None:
                self.page.title_text = self.page.title_after_join

    async def fill(self, value, timeout=None):
        self.page.fills.append((self.selector, value))
        if self.page.fill_updates_value:
            self.page.set_value(self.selector, value)

    async def input_value(self, timeout=0):
        self.page.input_value_calls.append(self.selector)
        return self.page.values.get(self.selector, "")

    async def is_enabled(self, timeout=0):
        return self.selector not in self.page.disabled

    async def inner_text(self, timeout=1000):
        return self.page.text

    async def evaluate(self, script, value=None):
        if "candidate_text" in script:
            text = self.page.text_for_selector(self.selector)
            return {
                "candidate_text": text,
                "clickable_target_text": self.page.clickable_target_texts.get(self.selector, text),
                "broad_context_text": self.page.broad_context_texts.get(self.selector, self.page.text),
                "container_text": self.page.container_texts.get(self.selector, text),
                "tag": self.page.tags.get(self.selector, "SPAN"),
                "role": self.page.roles.get(self.selector, ""),
                "href": self.page.hrefs.get(self.selector, ""),
                "onclick": self.selector in self.page.onclick_selectors,
                "tabindex": self.page.tabindexes.get(self.selector, ""),
                "rect": self.page.rects.get(self.selector),
            }
        if value is not None:
            self.page.js_sets.append((self.selector, value))
            self.page.set_value(self.selector, value)
            return self.page.values.get(self.selector, "")
        if "getAttribute" in script:
            attrs = self.page.input_attrs.get(self.selector, {})
            return {
                "name": attrs.get("name", ""),
                "id": attrs.get("id", self.selector.lstrip("#")),
                "type": attrs.get("type", "text"),
                "placeholder": attrs.get("placeholder", ""),
                "aria_label": attrs.get("aria-label", ""),
                "role": attrs.get("role", ""),
                "value": self.page.values.get(self.selector, ""),
                "focused": self.page.focused_selector == self.selector,
            }
        return self.page.values.get(self.selector, "")

    async def bounding_box(self):
        return self.page.rects.get(self.selector)


class FakeKeyboard:
    def __init__(self, page=None):
        self.page = page
        self.presses = []
        self.actions = []

    async def press(self, key):
        self.presses.append(key)
        self.actions.append(("press", key))
        if key in {"Control+A", "Meta+A"} and self.page is not None:
            self.page.select_all = True
        if key == "Backspace" and self.page is not None and self.page.select_all:
            selector = self.page.focused_selector
            if selector:
                self.page.set_value(selector, "")
            self.page.select_all = False

    async def type(self, value):
        self.actions.append(("type", value))
        if self.page is None or not self.page.keyboard_updates_value:
            return
        selector = self.page.focused_selector
        if not selector:
            return
        current = "" if self.page.select_all else self.page.values.get(selector, "")
        self.page.set_value(selector, f"{current}{value}")
        self.page.select_all = False


class FakeWebexPage:
    url = "https://example.webex.com/meet/test"

    def __init__(self, join_result):
        self.join_result = join_result
        self.visible = {"#join"}
        self.disabled = set()
        self.clicks = []
        self.waits = []
        self.timeout_selectors = set()
        self.fills = []
        self.values = {}
        self.input_value_calls = []
        self.js_sets = []
        self.labels = {}
        self.text_inputs = ["#name"]
        self.role_textboxes = []
        self.input_attrs = {}
        self.fill_updates_value = True
        self.keyboard_updates_value = True
        self.enable_join_on_name_fill = False
        self.focused_selector = None
        self.select_all = False
        self.title_text = "Fake Webex"
        self.title_after_join = None
        self.text = f"visible {join_result} screen"
        self.html = "<html><body>visible {}</body></html>".format(join_result)
        self.keyboard = FakeKeyboard(self)
        self.try_again_reveals = set()
        self.dom_try_again_result = None
        self.js_text_action_result = None
        self.js_text_action_candidates = []
        self.js_candidate_click_result = None
        self.js_candidate_clicks = []
        self.js_candidate_click_callback = None
        self.links_buttons_debug = []
        self.text_roles = {}
        self.selector_texts = {}
        self.container_texts = {}
        self.clickable_target_texts = {}
        self.broad_context_texts = {}
        self.tags = {}
        self.roles = {}
        self.hrefs = {}
        self.onclick_selectors = set()
        self.tabindexes = {}
        self.rects = {}
        self.mouse = FakeMouse(self)
        self.frames = []

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    async def title(self):
        return self.title_text

    async def content(self):
        return self.html

    def locator(self, selector):
        if selector.startswith(":focus"):
            return FakeWebexLocator(self, self.focused_selector or "#missing-focus")
        if selector.startswith("input:not([type") or selector in {
            "input, textarea, [contenteditable=\"true\"]",
            "input:not([type=\"hidden\"]), textarea, [contenteditable=\"true\"]",
        }:
            return FakeWebexLocator(self, selector, matches=list(self.text_inputs))
        return FakeWebexLocator(self, selector)

    def get_by_text(self, text, exact=True):
        if text in {"Try again", "Retry", "다시 시도"} and "#try-again" in self.visible:
            return FakeWebexLocator(self, "#try-again")
        if text in {"Got it", "확인", "알겠습니다"} and "#got-it" in self.visible:
            return FakeWebexLocator(self, "#got-it")
        return FakeWebexLocator(self, f"text={text}")

    def get_by_label(self, pattern):
        for label, selector in self.labels.items():
            if pattern.search(label):
                return FakeWebexLocator(self, selector)
        return FakeWebexLocator(self, "#missing-label")

    def get_by_role(self, role, name=None):
        if role in {"button", "link"}:
            for text, selector in self.text_roles.get(role, {}).items():
                if name is None or name.search(text):
                    return FakeWebexLocator(self, selector)
            return FakeWebexLocator(self, f"#missing-role-{role}")
        if role != "textbox":
            return FakeWebexLocator(self, "#missing-role")
        if name is None:
            return FakeWebexLocator(self, "role=textbox", matches=list(self.role_textboxes))
        for selector in self.role_textboxes:
            attrs = self.input_attrs.get(selector, {})
            accessible_name = " ".join(
                str(attrs.get(key, "")) for key in ("aria-label", "placeholder", "name", "id")
            )
            if name.search(accessible_name):
                return FakeWebexLocator(self, selector)
        return FakeWebexLocator(self, "#missing-role")

    async def evaluate(self, script, payload=None):
        if "js_candidate_click" in script:
            self.js_candidate_clicks.append(payload)
            if isinstance(self.js_candidate_click_result, dict):
                result = dict(self.js_candidate_click_result)
                result.setdefault("selector", payload)
                if result.get("ok") and self.js_candidate_click_callback:
                    self.js_candidate_click_callback(payload)
                return result
            if self.js_candidate_click_result:
                if self.js_candidate_click_callback:
                    self.js_candidate_click_callback(payload)
                return {"ok": True, "method": "js_candidate_click", "selector": payload}
            return {"ok": False, "method": "js_candidate_click", "reason": "not_configured"}
        if "dom-near-problem-text" in script:
            return self.dom_try_again_result
        if "js_text_click" in script or "js_text_diagnostic" in script:
            if "const shouldClick = false" in script:
                return list(self.js_text_action_candidates)
            result = self.js_text_action_result or self.dom_try_again_result
            return {**result, "ok": True, "method": "js_text_click"} if isinstance(result, dict) else result
        if "location.href" in script and "frame_urls" in script:
            frame_urls = [
                str(getattr(frame, "url", "") or "")
                for frame in self.frames
                if frame is not self
            ]
            return {"title": self.title_text, "url": self.url, "frame_urls": frame_urls}
        if "mdc-input input" in script or "visible_inputs" in script:
            visible_inputs = []
            for selector in sorted(self.visible):
                text = self.text_for_selector(selector)
                item = {
                    "tag": self.tags.get(selector, "BUTTON" if "button" in selector or "join" in selector else "DIV"),
                    "role": self.roles.get(selector, ""),
                    "type": self.input_attrs.get(selector, {}).get("type", ""),
                    "name": self.input_attrs.get(selector, {}).get("name", ""),
                    "id": selector.lstrip("#"),
                    "text": text,
                    "aria_label": self.input_attrs.get(selector, {}).get("aria-label", ""),
                    "placeholder": self.input_attrs.get(selector, {}).get("placeholder", ""),
                    "href": self.hrefs.get(selector, ""),
                }
                if selector in self.text_inputs or selector in self.role_textboxes or "input" in selector or selector == "#name":
                    visible_inputs.append(item)
            return visible_inputs
        if "visible_buttons_links" in script or "button, a, [role='button']" in script or "button, a, [role='button'], [role='link']" in script:
            visible_buttons_links = []
            for selector in sorted(self.visible):
                text = self.text_for_selector(selector)
                item = {
                    "tag": self.tags.get(selector, "BUTTON" if "button" in selector or "join" in selector else "DIV"),
                    "role": self.roles.get(selector, ""),
                    "type": self.input_attrs.get(selector, {}).get("type", ""),
                    "name": self.input_attrs.get(selector, {}).get("name", ""),
                    "id": selector.lstrip("#"),
                    "text": text,
                    "aria_label": self.input_attrs.get(selector, {}).get("aria-label", ""),
                    "placeholder": self.input_attrs.get(selector, {}).get("placeholder", ""),
                    "href": self.hrefs.get(selector, ""),
                }
                if selector in {"#join", "#start", "#browser", "#continue", "#try-again", "#got-it"} or "button" in selector or text:
                    visible_buttons_links.append(item)
            return visible_buttons_links
        if "visible_text" in script and "hidden_text" in script:
            visible_text = self.text or WebexAdapter({"adapter_config": {}})._sanitize_html_text(self.html)
            return {"visible_text": visible_text, "hidden_text": visible_text or self.html}
        if "querySelectorAll(\"button, a, [role='button']\")" in script:
            return list(self.links_buttons_debug)
        selector = self.focused_selector
        attrs = self.input_attrs.get(selector, {}) if selector else {}
        return {
            "tag": "INPUT" if selector else "",
            "name": attrs.get("name", ""),
            "id": (selector or "").lstrip("#"),
            "type": attrs.get("type", "text") if selector else "",
            "placeholder": attrs.get("placeholder", ""),
            "aria_label": attrs.get("aria-label", ""),
            "role": attrs.get("role", ""),
            "value": self.values.get(selector, "") if selector else "",
        }

    def set_value(self, selector, value):
        self.values[selector] = str(value)
        if self.enable_join_on_name_fill and str(value):
            self.disabled.discard("#join")

    def selector_text_visible(self, selector):
        text = self.text_for_selector(selector)
        return bool(text and text in self.text)

    def text_for_selector(self, selector):
        if selector in self.selector_texts:
            return self.selector_texts[selector]
        if selector == "#try-again":
            return "Try again"
        if selector == "#got-it":
            return "Got it"
        if selector == "#download-indicator":
            return "Get ready to join"
        if selector == "#problem":
            return "Problem joining from browser?"
        if selector == "#download-button":
            return "Download"
        if selector == "#mobile":
            return "Join on mobile"
        if selector == "#join":
            return "Join"
        if selector.startswith("text="):
            return selector.split("=", 1)[1].strip('"')
        for marker in ("Try again", "Retry", "다시 시도", "Got it", "확인", "알겠습니다"):
            if marker in selector:
                return marker
        return ""


class FakeWebexFrame(FakeWebexPage):
    def __init__(self, join_result, url="https://web.webex.com/meeting", name=""):
        super().__init__(join_result)
        self.url = url
        self._name = name
        self.frames = []

    def name(self):
        return self._name


class SlowSnapshotPage(FakeWebexPage):
    async def evaluate(self, script, payload=None):
        if "visible_inputs" in script and "visible_buttons_links" in script:
            await asyncio.sleep(1)
        return await super().evaluate(script, payload=payload)


class TextSnapshotTimeoutFrame(FakeWebexFrame):
    async def evaluate(self, script, payload=None):
        if "visible_text" in script and "hidden_text" in script:
            await asyncio.sleep(1)
        return await super().evaluate(script, payload=payload)


class DetachedWebexFrame(FakeWebexFrame):
    async def evaluate(self, script, payload=None):
        raise RuntimeError("Frame was detached")


class FakeMouse:
    def __init__(self, page):
        self.page = page
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))
        self.page.clicks.append(("mouse", x, y))


class TitleFailsPage(FakeWebexPage):
    async def title(self):
        raise RuntimeError("title unavailable")


class ClosingAfterGotoPage(FakeWebexPage):
    def __init__(self, join_result):
        super().__init__(join_result)
        self.goto_calls = 0

    async def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls += 1
        self.url = url

    async def title(self):
        raise TargetClosedError("Target page, context or browser has been closed")


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)

    async def new_page(self):
        if not self.pages:
            raise TargetClosedError("Target page, context or browser has been closed")
        return self.pages.pop(0)


class FakeControlLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def wait_for(self, state="visible", timeout=0):
        if self.page.is_visible(self.selector):
            return None
        raise PlaywrightTimeoutError(f"{self.selector} is not visible")

    async def click(self):
        self.page.click(self.selector)

    async def is_enabled(self, timeout=0):
        return True


class FakeControlPage:
    def __init__(self, states, transitions_enabled=True):
        self.states = dict(states)
        self.transitions_enabled = transitions_enabled
        self.clicks = []

    def locator(self, selector):
        return FakeControlLocator(self, selector)

    def is_visible(self, selector):
        if selector == "#mic-on":
            return self.states.get("mic") is True
        if selector == "#mic-off":
            return self.states.get("mic") is False
        if selector == "#camera-on":
            return self.states.get("camera") is True
        if selector == "#camera-off":
            return self.states.get("camera") is False
        if selector == "#share-button":
            return self.states.get("screen") is not True
        if selector in {"#share-on", "#share-stop"}:
            return self.states.get("screen") is True
        return False

    def click(self, selector):
        self.clicks.append(selector)
        if not self.transitions_enabled:
            return
        if selector == "#mic-on":
            self.states["mic"] = False
        elif selector == "#mic-off":
            self.states["mic"] = True
        elif selector == "#camera-on":
            self.states["camera"] = False
        elif selector == "#camera-off":
            self.states["camera"] = True
        elif selector == "#share-button":
            self.states["screen"] = True
        elif selector == "#share-stop":
            self.states["screen"] = False


def _control_test_adapter(states, transitions_enabled=True, extra_adapter_config=None):
    adapter_config = {
        "control_state_timeout_ms": 1,
        "control_state_settle_sec": 0,
        "optional_selector_timeout_ms": 1,
        "selectors": {
            "mic_on_indicator": "#mic-on",
            "mic_off_indicator": "#mic-off",
            "camera_on_indicator": "#camera-on",
            "camera_off_indicator": "#camera-off",
            "screen_share_button": "#share-button",
            "screen_share_on_indicator": "#share-on",
            "screen_share_stop": "#share-stop",
        },
    }
    adapter_config.update(extra_adapter_config or {})
    adapter = WebexAdapter({"adapter_config": adapter_config})
    adapter.page = FakeControlPage(states, transitions_enabled=transitions_enabled)
    adapter.mic_enabled = bool(states.get("mic", adapter.mic_enabled))
    adapter.camera_enabled = bool(states.get("camera", adapter.camera_enabled))
    adapter.screen_sharing = bool(states.get("screen", adapter.screen_sharing))
    adapter.diagnostic_stages = []

    async def collect_diagnostics(stage=None, extra=None):
        adapter.diagnostic_stages.append(stage)
        return {"stage": stage, "extra": extra}

    adapter.collect_diagnostics = collect_diagnostics
    return adapter


def _join_test_adapter(join_result, extra_config=None):
    config = {
        "adapter_config": {
            "skip_sanity_checks": True,
            "dismiss_external_protocol_dialog": False,
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
    selectors_update = extra_config.pop("selectors", None)
    adapter_config_updates = {
        key: extra_config.pop(key)
        for key in list(extra_config)
        if key
        in {
            "accept_lobby_as_joined",
            "allow_lobby_media_ready",
            "browser_join_click_total_timeout_sec",
            "download_retry_settle_ms",
            "download_retry_settle_sec",
            "download_retry_click_total_timeout_sec",
            "frame_snapshot_timeout_sec",
            "fail_on_post_join_media_unverified",
            "max_download_retry_attempts",
            "page_state_detection_timeout_sec",
            "retry_navigation_on_page_closed",
            "max_navigation_retries",
            "post_join_media_check",
            "post_join_media_check_timeout_sec",
            "post_browser_join_transition_timeout_sec",
            "post_final_join_result_timeout_sec",
            "skip_device_selection",
            "strict_post_join_media_state",
            "webclient_frame_ready_wait_sec",
        }
    }
    config["adapter_config"].update(adapter_config_updates)
    if selectors_update:
        config["adapter_config"]["selectors"].update(selectors_update)
    config.update(extra_config)
    adapter = WebexAdapter(config)
    adapter.page = FakeWebexPage(join_result)
    adapter.launch = _async_true
    adapter.mute_microphone = _async_true
    adapter.unmute_microphone = _async_true
    adapter.start_camera = _async_true
    adapter.stop_camera = _async_true
    adapter.diagnostic_stages = []
    adapter.diagnostic_extras = []

    async def collect_diagnostics(stage=None, extra=None):
        adapter.diagnostic_stages.append(stage)
        adapter.diagnostic_extras.append(extra or {})
        return {"stage": stage, "extra": extra}

    adapter.collect_diagnostics = collect_diagnostics
    return adapter


async def _async_true(*args, **kwargs):
    return True
