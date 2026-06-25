import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "vtc_traffic_generator"))

from vtc_traffic_generator.run_experiment import generate
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

    def test_adapter_does_not_trust_shortcuts_by_default(self):
        adapter = JitsiElectronAdapter({"adapter_config": {}})
        self.assertFalse(adapter._trust_shortcut_state())
        self.assertFalse(adapter._use_cached_control_state())

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
