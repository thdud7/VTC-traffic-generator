import json
import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "vtc_traffic_generator"))

from vtc_traffic_generator.run_experiment import generate, quote_inventory_value
from vtc_traffic_generator.tools.analyze_media_capture import (
    classify_udp_payload,
    load_stats_mappings,
    parse_rtp_header,
)
from vtc_traffic_generator.vtc_automation.adapters.jitsi_electron import JitsiElectronAdapter


class JitsiMediaHardeningTests(unittest.TestCase):
    def test_experiment_generates_explicit_virtual_microphone_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            generated = generate("vtc_traffic_generator/experiment.icsi.jitsi.3bot.5min.json", tmpdir)
            remote_config = json.loads(Path(generated["remote_configs"][0]).read_text())

        self.assertEqual(remote_config["virtual_audio"]["sink_name"], "VTC_Speaker")
        self.assertEqual(remote_config["virtual_audio"]["source_name"], "VTC_Microphone")
        self.assertEqual(remote_config["adapter_config"]["microphone_name"], "VTC_Microphone")
        self.assertFalse(remote_config["adapter_config"]["skip_device_selection"])
        self.assertFalse(remote_config["adapter_config"]["trust_shortcut_state"])
        self.assertTrue(remote_config["adapter_config"]["allow_pulse_default_device_selection_fallback"])
        self.assertTrue(remote_config["adapter_config"]["verify_audio_capture_attached"])

    def test_adapter_does_not_trust_shortcuts_by_default(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        self.assertFalse(adapter._trust_shortcut_state())
        self.assertFalse(adapter._use_cached_control_state())

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
