from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.mcp_client import _normalize_tts_message


class TestMcpClientTtsNormalization(unittest.TestCase):
    def test_units_converted_for_tts(self) -> None:
        text = "Approach 85 kt, sink 500 fpm at 300 AGL."
        out = _normalize_tts_message(text)
        self.assertIn("85 knots", out)
        self.assertIn("500 feet per minute", out)
        self.assertIn("300 above ground level", out)

    def test_kts_alias_converted(self) -> None:
        text = "Maintain 70 kts on final."
        out = _normalize_tts_message(text)
        self.assertIn("70 knots", out)


if __name__ == "__main__":
    unittest.main()

