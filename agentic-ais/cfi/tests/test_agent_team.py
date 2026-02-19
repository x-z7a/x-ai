from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cfi_ai.agent_team import CfiAgentTeam
from cfi_ai.types import FlightPhase


@dataclass
class _Msg:
    source: str
    content: str


class TestAgentTeamHelpers(unittest.TestCase):
    def test_candidate_selection(self) -> None:
        active = "approach_expert"
        master = "master_cfi"

        cands = CfiAgentTeam.choose_candidates([], active, master)
        self.assertEqual(cands, [active])

        cands = CfiAgentTeam.choose_candidates([_Msg(source="user", content="x")], active, master)
        self.assertEqual(cands, [active])

        cands = CfiAgentTeam.choose_candidates(
            [_Msg(source="user", content="x"), _Msg(source=active, content="analysis")],
            active,
            master,
        )
        self.assertEqual(cands, [master])

    def test_parse_decision_json(self) -> None:
        raw = '{"summary":"Stable approach","feedback_items":["Hold 70 kt","Small power corrections"],"speak_now":true,"speak_text":"Nice trend, keep this stabilized profile."}'
        decision = CfiAgentTeam.parse_decision(raw, FlightPhase.APPROACH)
        self.assertEqual(decision.phase, FlightPhase.APPROACH)
        self.assertEqual(decision.summary, "Stable approach")
        self.assertEqual(len(decision.feedback_items), 2)
        self.assertTrue(decision.speak_now)

    def test_parse_decision_fallback(self) -> None:
        decision = CfiAgentTeam.parse_decision("not json", FlightPhase.CRUISE)
        self.assertFalse(decision.speak_now)
        self.assertIn("No structured", decision.summary)


if __name__ == "__main__":
    unittest.main()
