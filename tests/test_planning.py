"""Step 7 run/reuse/blocked planning: evidence derivation and decisions."""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import FFmpegAdapter, OpenUtauAdapter
from tools.orchestration.capabilities import (
    Capability, Provenance, Readiness, SourceIdentity, State, Verification,
)
from tools.orchestration.execution import Execution, RunRegistry, StageState
from tools.orchestration.playback import FFplayAdapter
from tools.orchestration.process import OperationResult, ProcessResult, ProcessState
from tools.web_ui.planning import (
    InputBinding, PlanDecision as D, PlanReason as R, StageEvidence, StageRequest,
    WorkState as W, plan_from_state, plan_target,
)
from tools.web_ui.readiness import CandidateEvidence, CandidateReadiness as CR, ReadinessReason, ReasonCode
import test_stale_propagation as fixtures

RUN, REUSE, BLOCKED = D.RUN, D.REUSE, D.BLOCKED


def ev(work, readiness=CR.READY, reasons=(), inputs=None):
    return StageEvidence(work, readiness, reasons, inputs)


def summary(plan):
    return {item.stage: (item.decision, item.reason) for item in plan.stages}


class PureDecisionTests(unittest.TestCase):
    def test_scenarios_and_scoping(self):
        plan = plan_target("render-vocals", {
            "generate-abc": ev(W.CURRENT), "generate-ustx": ev(W.STALE),
            "render-vocals": ev(W.STALE),
        })
        self.assertEqual(summary(plan), {
            "generate-ustx": (RUN, R.STALE), "render-vocals": (RUN, R.STALE),
        })
        self.assertEqual([s.stage for s in plan.stages], ["generate-ustx", "render-vocals"])

    def test_blocked_propagation_and_reuse_of_unrelated(self):
        stale_ustx_blocked = ev(W.STALE, CR.BLOCKED, (ReadinessReason(ReasonCode.CAPABILITY_UNAVAILABLE),))
        plan = plan_target("mix-audio", {
            "generate-ustx": stale_ustx_blocked, "render-vocals": ev(W.MISSING),
            "generate-abc": ev(W.CURRENT),
            "mix-audio": ev(W.MISSING), "yue2-instrumental.generate": ev(W.CURRENT),
            "yue2-instrumental.prepare": ev(W.CURRENT),
        })
        got = summary(plan)
        self.assertEqual(got["generate-ustx"], (BLOCKED, R.READINESS_BLOCKED))
        self.assertEqual(got["render-vocals"], (BLOCKED, R.DEPENDENCY_BLOCKED))
        self.assertEqual(got["mix-audio"], (BLOCKED, R.DEPENDENCY_BLOCKED))
        self.assertEqual(got["yue2-instrumental.generate"][0], REUSE)
        self.assertEqual(got["generate-abc"][0], REUSE)

    def test_plan_aware_readiness_reconciliation(self):
        stale = ReadinessReason(ReasonCode.ARTIFACT_STALE, input_role="ustx")
        genuine = ReadinessReason(ReasonCode.CAPABILITY_UNAVAILABLE)
        base = {"generate-ustx": ev(W.STALE)}
        ok = plan_target("render-vocals", {
            **base, "render-vocals": ev(W.STALE, CR.BLOCKED, (stale,))})
        self.assertEqual(summary(ok)["render-vocals"], (RUN, R.STALE))
        bad = plan_target("render-vocals", {
            **base, "render-vocals": ev(W.STALE, CR.BLOCKED, (stale, genuine))})
        self.assertEqual(summary(bad)["render-vocals"], (BLOCKED, R.READINESS_BLOCKED))

    def test_unknown_evidence_is_never_run_by_default(self):
        plan = plan_target("generate-ustx", {})
        self.assertEqual(summary(plan)["generate-ustx"][0], BLOCKED)
        plan = plan_target("generate-ustx", {"generate-ustx": StageEvidence(W.UNKNOWN, CR.READY)})
        self.assertEqual(summary(plan)["generate-ustx"][0], BLOCKED)
        plan = plan_target("generate-ustx", {"generate-ustx": StageEvidence(W.MISSING)})
        self.assertEqual(summary(plan)["generate-ustx"][0], BLOCKED)

    def test_failed_and_missing_run_when_satisfiable(self):
        for work, reason in ((W.FAILED, R.FAILED), (W.MISSING, R.MISSING)):
            plan = plan_target("render-vocals", {
                "generate-ustx": ev(W.CURRENT), "render-vocals": ev(work)})
            self.assertEqual(summary(plan)["render-vocals"], (RUN, reason))
            self.assertEqual(summary(plan)["generate-ustx"], (REUSE, R.CURRENT_REUSABLE))

    def test_operation_granularity(self):
        gen = "yue2-instrumental.generate"
        plan = plan_target(gen, {"yue2-instrumental.prepare": ev(W.CURRENT), gen: ev(W.MISSING),
                                 "generate-abc": ev(W.CURRENT)})
        self.assertEqual(summary(plan), {
            "generate-abc": (REUSE, R.CURRENT_REUSABLE),
            "yue2-instrumental.prepare": (REUSE, R.CURRENT_REUSABLE), gen: (RUN, R.MISSING)})
        plan = plan_target("mix-audio", {
            "generate-ustx": ev(W.CURRENT), "render-vocals": ev(W.CURRENT),
            "yue2-instrumental.prepare": ev(W.STALE), gen: ev(W.STALE),
            "generate-abc": ev(W.CURRENT), "mix-audio": ev(W.STALE)})
        self.assertEqual([i.stage for i in plan.stages if i.decision == RUN], [
            "yue2-instrumental.prepare", gen, "mix-audio"])

    def test_imported_ustx_has_no_generate_stage(self):
        plan = plan_target("render-vocals", {"render-vocals": ev(
            W.MISSING, inputs=(InputBinding("ustx", imported=True),))})
        self.assertEqual(summary(plan), {"render-vocals": (RUN, R.MISSING)})

    def test_selected_producer_controls_playback(self):
        base = {"render-vocals": ev(W.CURRENT), "mix-audio": ev(W.CURRENT),
                "generate-ustx": ev(W.CURRENT)}
        for producer in ("render-vocals", "mix-audio"):
            plan = plan_target("play-audio", {**base, "play-audio": ev(
                W.MISSING, inputs=(InputBinding("audio", producer),))})
            names = {i.stage for i in plan.stages}
            self.assertIn(producer, names)
            if producer == "render-vocals":
                self.assertNotIn("mix-audio", names)

    def test_unresolved_selection_blocks(self):
        plan = plan_target("play-audio", {"play-audio": ev(W.MISSING)})
        self.assertEqual(summary(plan)["play-audio"], (BLOCKED, R.DEPENDENCY_BLOCKED))

    def test_deterministic(self):
        a = {"generate-abc": ev(W.STALE), "generate-ustx": ev(W.CURRENT), "render-vocals": ev(W.FAILED)}
        self.assertEqual(plan_target("render-vocals", a), plan_target("render-vocals", dict(reversed(list(a.items())))))

    def test_unknown_target(self):
        with self.assertRaises(ValueError):
            plan_target("nope", {})


class DerivedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.StalePropagationTests(methodName="runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.catalog = self.f.catalog

    def cap(self, op, tool=None):
        tool = tool or op.tool
        return Capability(
            SourceIdentity(tool, f"{op.command.cwd}|{op.command.executable}",
                           op.production.contract if tool == op.tool and op.production else f"{tool}-phase1"),
            "executable", str(op.command.executable), Provenance.FILESYSTEM,
            Verification.OBSERVED, State.READY, Readiness.READY)

    def request(self, op, **updates):
        caps = (self.cap(op),)
        if any(item.kind == "audio" for item in op.expected) or op.stage == "ffplay.play":
            caps += (self.cap(op, "ffprobe"),)
        values = dict(candidate=op, configuration=StageState.READY, validated=True, capabilities=caps)
        values.update(updates)
        return StageRequest(op, CandidateEvidence(**values))

    def bound(self, name, op=None):
        # Output paths are fresh so occupancy does not mask currentness decisions.
        op = op or self.f.operations[name]
        return self.catalog.bind_inputs(replace(op, expected=tuple(
            replace(e, path=e.path.with_name("new-" + e.path.name)) for e in op.expected)))

    def requests(self, changes=None):
        changes = changes or {}
        names = {"generate-abc": "abc", "generate-ustx": "ustx", "render-vocals": "vocal",
                 "yue2-instrumental.prepare": "prepare", "yue2-instrumental.generate": "instrumental",
                 "mix-audio": "mix"}
        return {node: self.request(self.bound(name, changes.get(name))) for node, name in names.items()}

    def snapshot(self):
        return self.f.history_snapshot()

    def test_all_current_reuses_and_is_read_only(self):
        before = self.snapshot()
        plan = plan_from_state("mix-audio", catalog=self.catalog, requests=self.requests())
        self.assertTrue(all(d == REUSE for d, _ in summary(plan).values()), plan)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(summary(plan)), 6)

    def test_missing_evidence_is_blocked_not_run(self):
        plan = plan_from_state("generate-ustx", catalog=self.catalog, requests={})
        self.assertEqual(summary(plan)["generate-ustx"][0], BLOCKED)

    def test_singer_change_selective_invalidation(self):
        changes = {"ustx": self.f.singer_candidate()}
        reqs = self.requests(changes)
        plan = plan_from_state("mix-audio", catalog=self.catalog, requests=reqs)
        got = summary(plan)
        self.assertEqual(got["generate-ustx"], (RUN, R.STALE))
        self.assertEqual(got["render-vocals"], (RUN, R.STALE))
        self.assertEqual(got["mix-audio"], (RUN, R.STALE))
        self.assertEqual(got["generate-abc"][0], REUSE)
        self.assertEqual(got["yue2-instrumental.generate"][0], REUSE)
        self.assertEqual(got["yue2-instrumental.prepare"][0], REUSE)

    def test_failed_run_with_partial_output_is_failed(self):
        registry = RunRegistry(catalog=self.catalog)
        self.addCleanup(registry.close)
        op = self.f.operations["vocal"]
        run = Execution(op, output_limit=1024, catalog=self.catalog)
        run.process = ProcessResult(op.command, ProcessState.EXITED, 1)
        run.result = OperationResult(op, run.process, ())
        registry.runs[run.id] = run
        plan = plan_from_state("render-vocals", catalog=self.catalog, registry=registry,
                               requests=self.requests())
        self.assertEqual(summary(plan)["render-vocals"], (RUN, R.FAILED))
        self.assertEqual(summary(plan)["generate-ustx"][0], REUSE)

    def test_unsuccessful_catalog_run_is_failed(self):
        run = self.catalog.runs[self.f.entries["vocal"].producer_run]
        self.catalog.runs[run.id] = replace(run, success=False)
        plan = plan_from_state("render-vocals", catalog=self.catalog, requests=self.requests())
        self.assertEqual(summary(plan)["render-vocals"], (RUN, R.FAILED))

    def test_missing_output_runs_and_unsatisfiable_capability_blocks(self):
        reqs = self.requests()
        Path(self.f.entries["vocal"].path).unlink()
        self.catalog.refresh()
        plan = plan_from_state("render-vocals", catalog=self.catalog, requests=reqs)
        self.assertEqual(summary(plan)["render-vocals"], (RUN, R.MISSING))
        op = self.bound("vocal")
        reqs["render-vocals"] = self.request(op, capabilities=())
        plan = plan_from_state("render-vocals", catalog=self.catalog, requests=reqs)
        self.assertEqual(summary(plan)["render-vocals"][0], BLOCKED)

    def test_selected_lineage_controls_playback(self):
        for name, node, other in (("vocal", "render-vocals", "mix-audio"),
                                  ("mix", "mix-audio", "render-vocals")):
            entry = self.f.entries[name]
            reqs = self.requests()
            op = self.catalog.bind_inputs(FFplayAdapter(self.f.executable, self.f.root).build(Path(entry.path)))
            reqs["play-audio"] = self.request(op)
            plan = plan_from_state("play-audio", catalog=self.catalog, requests=reqs)
            names = {i.stage for i in plan.stages}
            self.assertIn(node, names)
            if node == "render-vocals":
                self.assertNotIn(other, names)

    def test_imported_ustx_needs_no_generate_stage(self):
        imported = self.f.file("imported.ustx", "ustx_version: 0.7\nresolution: 480\ntracks:\nvoice_parts:")
        self.catalog.register_input(imported, "ustx")
        op = self.catalog.bind_inputs(OpenUtauAdapter(self.f.executable, self.f.root).build(
            imported, self.f.root / "imported-vocal.wav"))
        plan = plan_from_state("render-vocals", catalog=self.catalog,
                               requests={"render-vocals": self.request(op)})
        self.assertEqual(summary(plan), {"render-vocals": (RUN, R.STALE)})

    def test_planning_does_not_start_runs(self):
        registry = RunRegistry(catalog=self.catalog)
        self.addCleanup(registry.close)
        plan_from_state("mix-audio", catalog=self.catalog, registry=registry, requests=self.requests())
        self.assertEqual(len(registry.runs), 0)


if __name__ == "__main__":
    unittest.main()
