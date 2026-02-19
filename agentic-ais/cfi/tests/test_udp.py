from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.xplane_udp import (
    BEACON_PREFIX,
    build_rref_request_packet,
    parse_beacon_datagram,
    parse_rref_datagram,
)


class TestUdpPackets(unittest.TestCase):
    def test_build_rref_request_packet_shape(self) -> None:
        packet = build_rref_request_packet(freq_hz=10, index=7, dataref="sim/test/dataref")
        self.assertEqual(len(packet), 413)

        header, freq, idx, raw_dataref = struct.unpack("<5sii400s", packet)
        self.assertEqual(header, b"RREF\x00")
        self.assertEqual(freq, 10)
        self.assertEqual(idx, 7)
        self.assertTrue(raw_dataref.startswith(b"sim/test/dataref"))

    def test_parse_rref_datagram(self) -> None:
        payload = b"RREF," + struct.pack("<if", 1, 123.5) + struct.pack("<if", 2, -4.25)
        parsed = parse_rref_datagram(payload)
        self.assertIn(1, parsed)
        self.assertIn(2, parsed)
        self.assertAlmostEqual(parsed[1], 123.5, places=5)
        self.assertAlmostEqual(parsed[2], -4.25, places=5)

    def test_parse_rref_invalid_prefix(self) -> None:
        parsed = parse_rref_datagram(b"NOPE")
        self.assertEqual(parsed, {})

    def test_parse_beacon_datagram(self) -> None:
        # BECN body: byte, byte, int, int, int, ushort, name...
        body = struct.pack("<BBiiiH", 1, 2, 12345, 120000, 1, 49000) + b"MyXPlane\x00"
        payload = BEACON_PREFIX + body
        parsed = parse_beacon_datagram(payload, sender_ip="192.168.1.50")
        self.assertEqual(parsed, ("192.168.1.50", 49000))

    def test_parse_beacon_invalid(self) -> None:
        self.assertIsNone(parse_beacon_datagram(b"NOPE", sender_ip="192.168.1.1"))


if __name__ == "__main__":
    unittest.main()
