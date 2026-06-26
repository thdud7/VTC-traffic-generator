import json
import asyncio
import subprocess
import sys
import tempfile
import unittest
import time
from types import SimpleNamespace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "vtc_traffic_generator"))

import vtc_traffic_generator.run_experiment as run_experiment_module
import vtc_traffic_generator.vtc_generator as generator_module
from vtc_traffic_generator.run_experiment import generate, quote_inventory_value
from vtc_traffic_generator.tools.analyze_media_capture import (
    classify_udp_payload,
    load_stats_mappings,
    parse_rtp_header,
    run_command,
)
from vtc_traffic_generator.vtc_automation.adapters.jitsi_electron import JitsiElectronAdapter, MeetingProbeResult
from vtc_traffic_generator.vtc_automation.live_media_probe import classify_udp_payload as classify_live_udp_payload
from vtc_traffic_generator.vtc_automation.packet_capture import PacketCaptureSession


class JitsiMediaHardeningTests(unittest.TestCase):
    def test_experiment_generates_explicit_virtual_microphone_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated = generate("vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.json", tmpdir)
            remote_config = json.loads(Path(generated["remote_configs"][0]).read_text())

        self.assertEqual(remote_config["virtual_audio"]["sink_name"], "VTC_Speaker")
        self.assertEqual(remote_config["virtual_audio"]["source_name"], "VTC_Microphone")
        self.assertEqual(remote_config["virtual_video"]["source"], "testsrc2")
        self.assertEqual(remote_config["virtual_video"]["fps"], 10)
        self.assertEqual(remote_config["virtual_video"]["width"], 640)
        self.assertEqual(remote_config["virtual_video"]["height"], 360)
        self.assertEqual(remote_config["adapter_config"]["microphone_name"], "VTC_Microphone")
        self.assertFalse(remote_config["adapter_config"]["skip_device_selection"])
        self.assertFalse(remote_config["adapter_config"]["trust_shortcut_state"])
        self.assertTrue(remote_config["adapter_config"]["allow_pulse_default_device_selection_fallback"])
        self.assertFalse(remote_config["adapter_config"]["allow_media_capture_meeting_fallback"])
        self.assertTrue(remote_config["adapter_config"]["verify_audio_capture_attached"])
        self.assertTrue(remote_config["adapter_config"]["launch_url_as_arg"])
        self.assertEqual(remote_config["adapter_config"]["launch_url_protocol"], "jitsi-meet")
        self.assertTrue(remote_config["adapter_config"]["skip_url_entry"])
        self.assertFalse(remote_config["adapter_config"]["skip_join_flow"])
        self.assertGreaterEqual(remote_config["adapter_config"]["joined_wait_sec"], 20)
        self.assertFalse(remote_config["adapter_config"]["reset_user_data_dir"])
        self.assertTrue(remote_config["adapter_config"]["coordinates_relative_to_window"])
        self.assertNotIn("|jitsi", remote_config["adapter_config"]["window_title_regex"])
        self.assertNotIn("|jitsi", remote_config["adapter_config"]["accessibility_window_regex"])
        self.assertEqual(remote_config["adapter_config"]["coordinates"]["name_input"], [200, 286])
        self.assertEqual(remote_config["adapter_config"]["coordinates"]["join_button"], [200, 342])
        self.assertEqual(remote_config["adapter_config"]["coordinates"]["mic_button"], [60, 408])
        self.assertEqual(remote_config["adapter_config"]["window_geometry"]["left"], 480)
        self.assertEqual(remote_config["adapter_config"]["window_geometry"]["top"], 40)
        self.assertEqual(remote_config["adapter_config"]["window_geometry"]["width"], 800)
        self.assertEqual(remote_config["adapter_config"]["window_geometry"]["height"], 720)

    def test_generation_uses_execution_scoped_logs_and_stable_config_hash(self):
        with tempfile.TemporaryDirectory() as first_tmp, tempfile.TemporaryDirectory() as second_tmp:
            first = generate("vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.json", first_tmp)
            second = generate("vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.json", second_tmp)

            first_remote = json.loads(Path(first["remote_configs"][0]).read_text())
            first_controller = json.loads(Path(first["controller_config"]).read_text())
            second_remote = json.loads(Path(second["remote_configs"][0]).read_text())
            first_manifest = json.loads(Path(first["run_manifest"]).read_text())
            second_manifest = json.loads(Path(second["run_manifest"]).read_text())
            inventory = Path(first["inventory"]).read_text(encoding="utf-8")

        execution_id = first_remote["execution_id"]
        self.assertIn(execution_id, first_controller["action_log_path"])
        self.assertIn(execution_id, first_controller["event_log_path"])
        self.assertIn(execution_id, first_remote["action_log_path"])
        self.assertIn(execution_id, first_remote["adapter_config"]["event_log_path"])
        self.assertIn(execution_id, first_remote["adapter_config"]["app_log_path"])
        self.assertIn(execution_id, first_remote["adapter_config"]["adapter_log_path"])
        self.assertIn(f"capture_upload_execution_id={execution_id}", inventory)
        self.assertEqual(first_manifest["config_sha256"], second_manifest["config_sha256"])
        self.assertNotEqual(first_remote["execution_id"], second_remote["execution_id"])

    def test_packet_capture_file_stem_includes_execution_id(self):
        session = PacketCaptureSession(
            config={
                "execution_id": "run-abc",
                "service": "jitsi_electron",
                "bot_name": "bot1",
            },
            enabled=True,
        )
        stem = session._file_stem()
        self.assertTrue(stem.startswith("run-abc-"))
        self.assertTrue(stem.endswith("-jitsi_electron-bot1"))

    def test_upload_playbook_filters_current_execution_only(self):
        upload_playbook = Path("ansible/upload_captures.yml").read_text(encoding="utf-8")
        self.assertIn("capture_upload_include_pattern", upload_playbook)
        self.assertIn("{{ capture_upload_include_pattern }}.pcapng", upload_playbook)
        self.assertIn("{{ capture_upload_s3_uri }}/experiments/{{ capture_upload_run_id }}/{{ inventory_hostname }}", upload_playbook)
        self.assertNotIn("--include\n          - \"*.pcapng\"", upload_playbook)
        self.assertNotIn("{{ capture_upload_s3_uri }}/raw/{{ capture_upload_run_id }}", upload_playbook)
        self.assertNotIn("{{ capture_upload_s3_uri }}/analysis/{{ capture_upload_run_id }}", upload_playbook)
        self.assertNotIn("{{ capture_upload_s3_uri }}/logs/{{ capture_upload_run_id }}", upload_playbook)

    def test_controller_artifacts_upload_to_controller_log_prefix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest = root / "run-manifest-run-1.json"
            controller_config = root / "controller_config.json"
            remote_config = root / "remote_config_bot1.json"
            action_log = root / "actions-run-1.txt"
            event_log = root / "events-run-1.jsonl"
            for path in (manifest, controller_config, remote_config, action_log, event_log):
                path.write_text(path.name, encoding="utf-8")

            calls = []
            staged_files = []

            def fake_run(command, cwd=None, check=False):
                calls.append((command, cwd, check))
                staged_files.extend(sorted(path.name for path in Path(command[3]).iterdir()))
                return subprocess.CompletedProcess(command, 0)

            original_run = run_experiment_module.subprocess.run
            run_experiment_module.subprocess.run = fake_run
            try:
                result = run_experiment_module.upload_controller_artifacts(
                    {
                        "capture_upload_s3_uri": "s3://vtc-traffic-data/captures",
                        "capture_upload_run_id": "run-1",
                        "run_manifest": manifest,
                        "controller_config": controller_config,
                        "remote_configs": [remote_config],
                        "controller_action_log_path": action_log,
                        "controller_event_log_path": event_log,
                    }
                )
            finally:
                run_experiment_module.subprocess.run = original_run

        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls[0][0][:3], ["aws", "s3", "sync"])
        self.assertEqual(calls[0][0][4], "s3://vtc-traffic-data/captures/experiments/run-1/controller/")
        self.assertEqual(
            staged_files,
            [
                "actions-run-1.txt",
                "controller_config.json",
                "events-run-1.jsonl",
                "remote_config_bot1.json",
                "run-manifest-run-1.json",
            ],
        )

    def test_controller_registers_graceful_session_stop_rpc(self):
        generator_source = Path("vtc_traffic_generator/vtc_generator.py").read_text(encoding="utf-8")
        self.assertIn('server.register_function(stop_vtc_session, "stop_vtc_session")', generator_source)
        self.assertIn("wait_for_clients_to_finish_sessions", generator_source)

    def test_strict_icsi_schedule_clips_cutoff_utterances(self):
        policy = SimpleNamespace(
            events=[
                SimpleNamespace(
                    meeting_id="Bdb001",
                    speaker_id="me011",
                    bot_index=0,
                    channel="chan0",
                    start_sec=10.0,
                    end_sec=12.0,
                    dialogue_act_type="statement",
                    file_channel="chan0",
                ),
                SimpleNamespace(
                    meeting_id="Bdb001",
                    speaker_id="me011",
                    bot_index=0,
                    channel="chan0",
                    start_sec=179.5,
                    end_sec=181.0,
                    dialogue_act_type="statement",
                    file_channel="chan0",
                ),
                SimpleNamespace(
                    meeting_id="Bdb001",
                    speaker_id="me011",
                    bot_index=0,
                    channel="chan0",
                    start_sec=180.0,
                    end_sec=181.0,
                    dialogue_act_type="statement",
                    file_channel="chan0",
                ),
            ]
        )

        schedule = generator_module.build_icsi_strict_schedule(policy, max_duration_sec=180, scenario_offset_sec=0)

        self.assertEqual(len(schedule), 2)
        self.assertFalse(schedule[0]["clipped"])
        self.assertTrue(schedule[1]["clipped"])
        self.assertEqual(schedule[1]["playback_duration_sec"], 0.5)
        self.assertIs(schedule[1]["next_same_bot_start_sec"], None)
        self.assertEqual(schedule[0]["next_same_bot_start_sec"], 179.5)

    def test_random_mic_off_is_not_selected_during_speech_or_preroll(self):
        state = generator_module.make_scenario_state([object()])
        state["states"][0]["mic"] = True
        state["speaking"][0] = 1
        self.assertIsNone(generator_module.choose_mic_action(generator_module.random.Random(1), state, 1))

        state["speaking"][0] = 0
        state["pre_roll_until_monotonic_ns"][0] = time.monotonic_ns() + 1_000_000_000
        self.assertIsNone(generator_module.choose_mic_action(generator_module.random.Random(1), state, 1))

        state["states"][0]["mic"] = False
        choice = generator_module.choose_mic_action(generator_module.random.Random(1), state, 1)
        self.assertEqual(choice, (0, True))

    def test_post_speech_mic_off_is_suppressed_for_short_same_bot_gap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generator_module.config = {
                "role": "controller",
                "event_log_path": str(Path(tmpdir) / "events.jsonl"),
                "action_log_path": str(Path(tmpdir) / "actions.txt"),
                "execution_id": "run-1",
                "experiment_id": "exp-1",
                "vtc_platform": "jitsi_electron",
            }
            utterance = {
                "meeting_id": "Bdb001",
                "speech_id": "speech-1",
                "bot_index": 0,
                "icsi_meeting_id": "Bdb001",
                "icsi_participant": "me011",
                "icsi_channel": "chan0",
                "scheduled_end_sec": 10.0,
                "scheduled_start_sec": 8.0,
                "next_same_bot_start_sec": 12.0,
                "clipped": False,
            }
            state = generator_module.make_scenario_state([object()])
            executor = SimpleNamespace(submit=lambda *args, **kwargs: self.fail("mic off should be suppressed"))

            result = generator_module.handle_post_speech_mic_decision(
                executor,
                [SimpleNamespace(ip="127.0.0.1", port=8001)],
                utterance,
                state,
                generator_module.datetime.now(generator_module.timezone.utc),
                generator_module.time.monotonic_ns(),
                generator_module.random.Random(1),
                1.0,
                6.0,
                3.0,
                1,
                0.0,
            )

            self.assertIsNone(result)
            events = Path(generator_module.config["event_log_path"]).read_text(encoding="utf-8")
            self.assertIn("insufficient_gap_to_preserve_icsi_timing", events)

    def test_adapter_does_not_trust_shortcuts_by_default(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        self.assertFalse(adapter._trust_shortcut_state())
        self.assertFalse(adapter._use_cached_control_state())

    def test_launch_command_appends_url_when_enabled(self):
        adapter = JitsiElectronAdapter(
            {
                "vtc_url": "https://jitsi.example/testroom",
                "adapter_config": {
                    "launch_command": "/opt/jitsi/jitsi-meet.AppImage --no-sandbox",
                    "launch_url_as_arg": True,
                },
            }
        )
        self.assertEqual(
            adapter._build_launch_command(),
            ["/opt/jitsi/jitsi-meet.AppImage", "--no-sandbox", "https://jitsi.example/testroom"],
        )

    def test_launch_command_converts_https_url_to_jitsi_meet_protocol(self):
        adapter = JitsiElectronAdapter(
            {
                "vtc_url": "https://172.31.32.200:8443/testroom#config.prejoinConfig.enabled=false",
                "adapter_config": {
                    "launch_command": "/opt/jitsi/jitsi-meet.AppImage --no-sandbox",
                    "launch_url_as_arg": True,
                    "launch_url_protocol": "jitsi-meet",
                },
            }
        )
        self.assertEqual(
            adapter._build_launch_command(),
            [
                "/opt/jitsi/jitsi-meet.AppImage",
                "--no-sandbox",
                "jitsi-meet://172.31.32.200:8443/testroom#config.prejoinConfig.enabled=false",
            ],
        )

    def test_connect_to_meeting_repositions_activated_meeting_window(self):
        adapter = JitsiElectronAdapter(
            {
                "vtc_url": "https://172.31.32.200:8443/testroom",
                "adapter_config": {
                    "skip_url_entry": True,
                    "skip_device_selection": True,
                    "skip_join_flow": True,
                    "page_load_wait_sec": 0,
                },
            }
        )
        calls = []
        adapter._activate_window = lambda: None
        adapter.dump_accessibility_tree = lambda *args, **kwargs: True
        adapter._activate_meeting_window = lambda vtc_url: calls.append(("activate", vtc_url)) or True
        adapter._position_window_after_launch = lambda: calls.append(("position", None))

        async def fake_is_in_meeting():
            return True

        adapter.is_in_meeting = fake_is_in_meeting
        asyncio.run(adapter.connect_to_meeting("https://172.31.32.200:8443/testroom", "bot1"))

        self.assertEqual(calls, [("activate", "https://172.31.32.200:8443/testroom"), ("position", None)])

    def test_meeting_probe_requires_stable_media_ready_samples(self):
        callbacks = []
        adapter = JitsiElectronAdapter(
            {
                "vtc_url": "https://172.31.32.200:8443/testroom",
                "_meeting_joined_callback": lambda url: callbacks.append(("joined", url)),
                "_media_ready_callback": lambda url: callbacks.append(("media_ready", url)),
                "adapter_config": {
                    "join_timeout_sec": 1,
                    "media_ready_timeout_sec": 1,
                    "join_poll_interval_sec": 0.05,
                    "join_stable_samples": 2,
                },
            }
        )
        adapter._current_vtc_url = "https://172.31.32.200:8443/testroom"
        samples = [
            MeetingProbeResult(joined=True, media_ready=True, strong_evidence=True, probe_name="live_pcap_jvb_media"),
            MeetingProbeResult(joined=True, media_ready=True, strong_evidence=True, probe_name="live_pcap_jvb_media"),
        ]

        def fake_probe(previous_snapshot):
            return samples.pop(0), None

        adapter._probe_meeting_and_media = fake_probe
        result = asyncio.run(adapter.wait_for_meeting_probe())

        self.assertTrue(result.media_ready)
        self.assertEqual(result.consecutive_success_count, 2)
        self.assertEqual(
            callbacks,
            [
                ("joined", "https://172.31.32.200:8443/testroom"),
                ("media_ready", "https://172.31.32.200:8443/testroom"),
            ],
        )

    def test_coordinate_fallback_is_relative_to_jitsi_window(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        adapter.window_id = "100"

        def fake_run_xdotool(args, check=True, timeout=None):
            if args == ["getwindowgeometry", "--shell", "100"]:
                return subprocess.CompletedProcess(args, 0, stdout="X=480\nY=20\nWIDTH=400\nHEIGHT=600\n", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        adapter._run_xdotool = fake_run_xdotool

        self.assertEqual(adapter._absolute_coordinate((260, 137)), (740, 157))

    def test_screen_share_window_does_not_force_above_jitsi(self):
        deploy_playbook = Path("ansible/deploy_clients.yml").read_text(encoding="utf-8")
        self.assertIn("wmctrl -r \"$title\" -b remove,above", deploy_playbook)
        self.assertNotIn("wmctrl -r \"$title\" -b add,above", deploy_playbook)

    def test_jitsi_audio_capture_status_uses_source_output_source_column(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        adapter._pulse_source_status = lambda microphone_name: {
            "success": True,
            "source_index": "2",
            "target": microphone_name,
        }

        def fake_run_command(command, timeout=None, check=True):
            self.assertEqual(command, ["pactl", "list", "short", "source-outputs"])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="0\t1\t-\tmodule-remap-source.c\ts16le 2ch 44100Hz\n"
                "1\t2\t26\tprotocol-native.c\ts16le 2ch 44100Hz\n",
                stderr="",
            )

        adapter._run_command = fake_run_command
        status = adapter._jitsi_audio_capture_status("VTC_Microphone")
        self.assertTrue(status["success"])
        self.assertEqual(status["matching_source_outputs"][0][1], "2")

    def test_inventory_values_quote_ini_comments(self):
        self.assertEqual(quote_inventory_value("#aabbcc"), '"#aabbcc"')

    def test_pulse_default_device_selection_fallback_requires_matching_unmuted_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            adapter = JitsiElectronAdapter(
                {
                    "adapter_config": {
                        "allow_pulse_default_device_selection_fallback": True,
                        "event_log_path": str(Path(tmpdir) / "events.jsonl"),
                    },
                    "virtual_video": {"device": "/dev/null"},
                }
            )

            adapter._open_device_settings = lambda: False
            adapter.dump_accessibility_tree = lambda *args, **kwargs: True

            def fake_run_command(command, timeout=None, check=True):
                if command == ["pactl", "info"]:
                    return subprocess.CompletedProcess(command, 0, stdout="Default Source: VTC_Microphone\n", stderr="")
                if command == ["pactl", "list", "short", "sources"]:
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="1\talsa_output.pci.monitor\n2\tVTC_Microphone\tmodule-remap-source.c\n",
                        stderr="",
                    )
                if command == ["pactl", "get-source-mute", "VTC_Microphone"]:
                    return subprocess.CompletedProcess(command, 0, stdout="Mute: no\n", stderr="")
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="unexpected")

            adapter._run_command = fake_run_command

            self.assertTrue(asyncio.run(adapter.select_devices("VTC Bot Camera", "VTC_Microphone")))

            events = Path(tmpdir, "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("microphone_device_selected", events)
            self.assertIn("pulse-default-fallback", events)

    def test_pulse_default_device_selection_fallback_rejects_muted_source(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        status = adapter._parse_pulse_source_status(
            microphone_name="VTC_Microphone",
            info_stdout="Default Source: VTC_Microphone\n",
            sources_stdout="2\tVTC_Microphone\tmodule-remap-source.c\n",
            mute_stdout="Mute: yes\n",
        )
        self.assertFalse(status["success"])

    def test_parse_rtp_header_ignores_stun_and_extracts_ssrc_payload_type(self):
        stun = bytes.fromhex("000100002112a442000000000000000000000000")
        self.assertEqual(classify_udp_payload(stun), "stun")
        self.assertEqual(classify_live_udp_payload(stun), "stun")

        payload = bytearray(32)
        payload[0] = 0x80
        payload[1] = 96
        payload[2:4] = (345).to_bytes(2, "big")
        payload[4:8] = (123456).to_bytes(4, "big")
        payload[8:12] = (0xAABBCCDD).to_bytes(4, "big")
        parsed = parse_rtp_header(bytes(payload))
        self.assertEqual(parsed["payload_type"], 96)
        self.assertEqual(parsed["ssrc"], 0xAABBCCDD)
        self.assertEqual(classify_udp_payload(bytes(payload)), "rtp")

    def test_media_analyzer_tolerates_missing_optional_commands(self):
        result = run_command(["definitely-missing-vtc-command"])
        self.assertEqual(result.returncode, 127)
        self.assertIn("definitely-missing-vtc-command", result.stderr)

    def test_stats_mapping_uses_outbound_rtp_codec_relationship(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "stats.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "stats": [
                            {
                                "id": "codec-1",
                                "type": "codec",
                                "mimeType": "audio/opus",
                                "payloadType": 111,
                            },
                            {
                                "id": "outbound-1",
                                "type": "outbound-rtp",
                                "kind": "audio",
                                "ssrc": 1234,
                                "codecId": "codec-1",
                            },
                        ]
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            by_ssrc, by_pt = load_stats_mappings([path])

        self.assertEqual(by_ssrc[1234]["kind"], "audio")
        self.assertEqual(by_ssrc[1234]["codec_mime_type"], "audio/opus")
        self.assertEqual(by_pt[111]["codec_mime_type"], "audio/opus")


if __name__ == "__main__":
    unittest.main()
