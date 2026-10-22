"""Focused implementation tests for Step 7 run/reuse/blocked planning."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.web_ui.planning import (
    PlanDecision as D, PlanReason as R, StageEvidence, WorkState as W, plan_target,
)
from tools.web_ui.readiness import CandidateReadiness


def plan(target, **states):
    evidence = {
        name.replace("_", "-"): value if isinstance(value, StageEvidence) else StageEvidence(value)
        for name, value in states.items()
    }
    return plan_target(target, evidence)


def summary(result):
    return [(item.stage, item.decision, item.reason) for item in result.stages]


class PlanningTests(unittest.TestCase):
    def test_current_reused_and_scoped(self):
        result = plan("generate-ustx", generate_ustx=W.CURRENT, generate_abc=W.MISSING)
        self.assertEqual(summary(result), [("generate-ustx", D.REUSE, R.CURRENT_REUSABLE)])

    def test_stale_upstream_runs_and_downstream_runs(self):
        result = plan("render-vocals", generate_ustx=W.STALE, render_vocals=W.STALE)
        self.assertEqual(summary(result), [
            ("generate-ustx", D.RUN, R.STALE), ("render-vocals", D.RUN, R.STALE),
        ])

    def test_reusable_upstream_not_rerun(self):
        for state, reason in ((W.MISSING, R.MISSING), (W.FAILED, R.FAILED), (W.STALE, R.STALE)):
            result = plan("render-vocals", generate_ustx=W.CURRENT, render_vocals=state)
            self.assertEqual(summary(result), [
                ("generate-ustx", D.REUSE, R.CURRENT_REUSABLE), ("render-vocals", D.RUN, reason),
            ])

    def test_blocked_propagates(self):
        blocked = StageEvidence(W.STALE, CandidateReadiness.BLOCKED)
        result = plan("mix-audio", mix_audio=W.MISSING, render_vocals=W.MISSING,
                      generate_yue2_audio=W.CURRENT, generate_ustx=blocked)
        decisions = {item.stage: (item.decision, item.reason) for item in result.stages}
        self.assertEqual(decisions["generate-ustx"], (D.BLOCKED, R.READINESS_BLOCKED))
        for name in ("render-vocals", "mix-audio"):
            self.assertEqual(decisions[name], (D.BLOCKED, R.DEPENDENCY_BLOCKED))
        self.assertEqual(decisions["generate-yue2-audio"][0], D.REUSE)

    def test_deterministic_and_unique(self):
        first = plan("mix-audio", generate_abc=W.STALE, generate_yue2_audio=W.MISSING,
                     generate_ustx=W.CURRENT, render_vocals=W.FAILED, mix_audio=W.MISSING)
        second = plan("mix-audio", mix_audio=W.MISSING, render_vocals=W.FAILED,
                      generate_ustx=W.CURRENT, generate_yue2_audio=W.MISSING, generate_abc=W.STALE)
        self.assertEqual(first, second)
        names = [item.stage for item in first.stages]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names, ["generate-abc", "generate-ustx", "generate-yue2-audio",
                                 "render-vocals", "mix-audio"])

    def test_unknown_stage(self):
        with self.assertRaises(ValueError):
            plan_target("nope", {})


if __name__ == "__main__":
    unittest.main()
