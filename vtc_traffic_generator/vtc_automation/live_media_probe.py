from __future__ import annotations

import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


def ipstr(raw: bytes) -> str:
    return ".".join(str(part) for part in raw)


@dataclass
class MediaProbeSnapshot:
    path: str
    readable: bool
    error: str | None = None
    packets_total: int = 0
    stun_packets: int = 0
    dtls_packets: int = 0
    rtp_like_packets: int = 0
    rtcp_like_packets: int = 0
    client_to_jvb_media_like_packets: int = 0
    jvb_to_client_media_like_packets: int = 0
    client_to_jvb_media_like_bytes: int = 0
    jvb_to_client_media_like_bytes: int = 0
    tuple_counts: dict[str, int] = field(default_factory=dict)

    @property
    def bidirectional_media_like(self) -> bool:
        return self.client_to_jvb_media_like_packets > 0 and self.jvb_to_client_media_like_packets > 0

    def media_like_delta_from(self, previous: "MediaProbeSnapshot | None") -> dict[str, int]:
        if previous is None:
            return {
                "client_to_jvb_media_like_packets": 0,
                "jvb_to_client_media_like_packets": 0,
                "client_to_jvb_media_like_bytes": 0,
                "jvb_to_client_media_like_bytes": 0,
            }
        return {
            "client_to_jvb_media_like_packets": self.client_to_jvb_media_like_packets
            - previous.client_to_jvb_media_like_packets,
            "jvb_to_client_media_like_packets": self.jvb_to_client_media_like_packets
            - previous.jvb_to_client_media_like_packets,
            "client_to_jvb_media_like_bytes": self.client_to_jvb_media_like_bytes
            - previous.client_to_jvb_media_like_bytes,
            "jvb_to_client_media_like_bytes": self.jvb_to_client_media_like_bytes
            - previous.jvb_to_client_media_like_bytes,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "readable": self.readable,
            "error": self.error,
            "packets_total": self.packets_total,
            "stun_packets": self.stun_packets,
            "dtls_packets": self.dtls_packets,
            "rtp_like_packets": self.rtp_like_packets,
            "rtcp_like_packets": self.rtcp_like_packets,
            "client_to_jvb_media_like_packets": self.client_to_jvb_media_like_packets,
            "jvb_to_client_media_like_packets": self.jvb_to_client_media_like_packets,
            "client_to_jvb_media_like_bytes": self.client_to_jvb_media_like_bytes,
            "jvb_to_client_media_like_bytes": self.jvb_to_client_media_like_bytes,
            "bidirectional_media_like": self.bidirectional_media_like,
            "tuple_counts": self.tuple_counts,
        }


def classify_udp_payload(payload: bytes) -> str:
    if len(payload) >= 20 and payload[0] & 0xC0 == 0 and payload[4:8] == b"\x21\x12\xa4\x42":
        return "stun"
    if (
        len(payload) >= 13
        and payload[0] in (20, 21, 22, 23, 24, 25)
        and payload[1:3] in (b"\xfe\xff", b"\xfe\xfd", b"\x03\x01", b"\x03\x03")
    ):
        return "dtls"
    if len(payload) >= 12 and payload[0] & 0xC0 == 0x80:
        payload_type = payload[1] & 0x7F
        if 64 <= payload_type <= 95:
            return "rtcp_like"
        return "rtp_like"
    return "unknown"


def iter_pcapng_packets(path: Path) -> Iterable[bytes]:
    data = path.read_bytes()
    off = 0
    endian = "<"
    while off + 12 <= len(data):
        block_type_le, block_len_le = struct.unpack_from("<II", data, off)
        block_type_be, block_len_be = struct.unpack_from(">II", data, off)
        block_type = block_type_le
        block_len = block_len_le
        if block_type == 0x0A0D0D0A:
            magic = data[off + 8 : off + 12]
            if magic == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif magic == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            block_len = struct.unpack_from(endian + "I", data, off + 4)[0]
        elif endian == ">":
            block_type = block_type_be
            block_len = block_len_be

        if block_len < 12 or off + block_len > len(data):
            break

        if block_type == 6 and block_len >= 32:
            body = off + 8
            try:
                _, _, _, caplen, _ = struct.unpack_from(endian + "IIIII", data, body)
            except struct.error:
                pass
            else:
                packet_start = body + 20
                yield data[packet_start : packet_start + caplen]
        off += block_len


def find_ipv4_udp(packet: bytes) -> tuple[str, str, int, int, bytes] | None:
    for ip_offset in range(0, min(40, len(packet) - 28)):
        if packet[ip_offset] >> 4 != 4:
            continue
        ihl = (packet[ip_offset] & 0x0F) * 4
        if ihl < 20 or ip_offset + ihl + 8 > len(packet):
            continue
        if packet[ip_offset + 9] != 17:
            continue
        total_len = struct.unpack_from("!H", packet, ip_offset + 2)[0]
        if total_len < ihl + 8:
            continue
        udp_offset = ip_offset + ihl
        try:
            src_port, dst_port, udp_len, _ = struct.unpack_from("!HHHH", packet, udp_offset)
        except struct.error:
            continue
        if udp_len < 8 or udp_offset + udp_len > len(packet) + 4:
            continue
        src_ip = ipstr(packet[ip_offset + 12 : ip_offset + 16])
        dst_ip = ipstr(packet[ip_offset + 16 : ip_offset + 20])
        payload = packet[udp_offset + 8 : udp_offset + udp_len]
        return src_ip, dst_ip, src_port, dst_port, payload
    return None


def probe_pcap_media(path: str | Path, client_ip: str | None, jvb_ip: str, jvb_port: int = 10000) -> MediaProbeSnapshot:
    pcap_path = Path(path).expanduser()
    snapshot = MediaProbeSnapshot(path=str(pcap_path), readable=False)
    if not pcap_path.exists():
        snapshot.error = "pcap does not exist"
        return snapshot

    tuple_counts: Counter[str] = Counter()
    try:
        for packet in iter_pcapng_packets(pcap_path):
            udp = find_ipv4_udp(packet)
            if not udp:
                continue
            src_ip, dst_ip, src_port, dst_port, payload = udp
            if jvb_ip not in (src_ip, dst_ip) or jvb_port not in (src_port, dst_port):
                continue
            snapshot.packets_total += 1
            cls = classify_udp_payload(payload)
            if cls == "stun":
                snapshot.stun_packets += 1
            elif cls == "dtls":
                snapshot.dtls_packets += 1
            elif cls == "rtp_like":
                snapshot.rtp_like_packets += 1
            elif cls == "rtcp_like":
                snapshot.rtcp_like_packets += 1

            tuple_counts[f"{src_ip}:{src_port}->{dst_ip}:{dst_port}:{cls}"] += 1
            if cls not in {"rtp_like", "rtcp_like"}:
                continue

            if dst_ip == jvb_ip and dst_port == jvb_port and (not client_ip or src_ip == client_ip):
                snapshot.client_to_jvb_media_like_packets += 1
                snapshot.client_to_jvb_media_like_bytes += len(payload)
            elif src_ip == jvb_ip and src_port == jvb_port and (not client_ip or dst_ip == client_ip):
                snapshot.jvb_to_client_media_like_packets += 1
                snapshot.jvb_to_client_media_like_bytes += len(payload)
        snapshot.readable = True
        snapshot.tuple_counts = dict(tuple_counts)
    except Exception as exc:
        snapshot.error = str(exc)
    return snapshot

