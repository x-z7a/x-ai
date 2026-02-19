from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.xplane_udp import build_rref_request_packet, parse_rref_datagram


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


if __name__ == "__main__":
    unittest.main()
