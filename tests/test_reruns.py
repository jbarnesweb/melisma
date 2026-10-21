"""Focused Step 6 policy regressions, supplementing external acceptance."""

import json
import sys
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.artifact_catalog import ArtifactCatalog
from tools.orchestration.execution import Execution, RunRegistry
from tools.orchestration.process import (
    Command, ExpectedArtifact, Operation, OperationInput, OperationResult,
    ProcessResult, ProcessState,
)
from tools.orchestration.provenance import ProductionConfiguration
from tools.web_ui.readiness import CandidateReadiness, ReadinessAssessment
from tools.web_ui.reruns import (
    BoundReadiness, RerunRequest, RerunTrigger, assess_rerun, submit_rerun,
)
import test_stale_propagation as production_fixtures


class RerunTests(unittest.TestCase):
    def setUp(self):
        self.catalog = ArtifactCatalog()
        self.registry = RunRegistry(catalog=self.catalog)
        command = Command(Path("/fixture/tool"), (), Path("/fixture"))
        self.operation = Operation(
            "melisma", "ustx", command,
            (ExpectedArtifact(Path("/fixture/new.ustx"), "ustx"),),
            inputs=(
                OperationInput("midi", Path("/fixture/a.mid"), "midi", "a" * 32, "a" * 64),
                OperationInput("lyrics", Path("/fixture/lyrics.txt"), "lyrics", "b" * 32, "b" * 64),
            ),
            production=ProductionConfiguration(
                "melisma-phase1", output_settings=(("ustx", (("singer", "Alpha"),)),),
            ),
        )
        self.old = self.attempt(self.operation)

    def attempt(self, operation, state=ProcessState.EXITED, *, stopped=False, validation=()):
        run = Execution(operation, output_limit=1024, catalog=self.catalog)
        run.stop_requested = stopped
        run.process = ProcessResult(operation.command, state, 7, stop_requested=stopped)
        run.result = OperationResult(operation, run.process, (), validation_processes=validation)
        self.registry.runs[run.id] = run
        return run

    def request(self, candidate=None, *, run=None, kind="failed", readiness=CandidateReadiness.READY):
        candidate = self.operation if candidate is None else candidate
        run = self.old if run is None else run
        return RerunRequest(
            "generate-ustx", candidate,
            (RerunTrigger(kind, "generate-ustx", candidate.stage, run.id),),
            (BoundReadiness(candidate, ReadinessAssessment(
                "generate-ustx", candidate.stage, readiness,
            )),),
        )

    def assess(self, request):
        return assess_rerun(request, catalog=self.catalog, registry=self.registry)

    def test_new_destination_and_incidental_command_data_remain_private(self):
        candidate = replace(
            self.operation, expected=(ExpectedArtifact(Path("/fixture/other.ustx"), "ustx"),),
            metadata=(("private", "PRIVATE_METADATA"),),
            command=replace(self.operation.command, arguments=("PRIVATE_ARG",),
                            environment=(("PRIVATE_ENV", "PRIVATE_VALUE"),)),
        )
        result = self.assess(self.request(candidate))
        self.assertEqual(result.decision, "rerun")
        self.assertNotIn("PRIVATE", json.dumps(asdict(result)))

    def test_same_operation_allows_changed_selected_input_and_revision(self):
        for updates in (
            {"artifact_id": "c" * 32}, {"revision": "c" * 64},
        ):
            with self.subTest(updates=updates):
                candidate = replace(self.operation, inputs=(
                    replace(self.operation.inputs[0], **updates), self.operation.inputs[1],
                ))
                self.assertEqual(self.assess(self.request(candidate)).decision, "rerun")

    def test_same_operation_allows_changed_settings_and_executable_context(self):
        for candidate in (
            replace(self.operation, production=ProductionConfiguration(
                "melisma-phase1", output_settings=(("ustx", (("singer", "Beta"),)),),
            )),
            replace(self.operation, command=replace(
                self.operation.command, executable=Path("/fixture/other-tool"),
            )),
            replace(self.operation, command=replace(
                self.operation.command, cwd=Path("/fixture/other-source"),
            )),
        ):
            with self.subTest(candidate=candidate):
                self.assertEqual(self.assess(self.request(candidate)).decision, "rerun")

    def test_historical_context_is_not_required_for_retained_failed_attempt(self):
        previous = replace(self.operation, inputs=(
            replace(self.operation.inputs[0], revision=None), self.operation.inputs[1],
        ))
        old = self.attempt(previous)
        self.assertEqual(self.assess(self.request(run=old)).decision, "rerun")

    def test_changed_context_preserves_replacement_readiness(self):
        candidate = replace(
            self.operation,
            inputs=(replace(self.operation.inputs[0], artifact_id="c" * 32),
                    self.operation.inputs[1]),
            production=ProductionConfiguration(
                "melisma-phase1", output_settings=(("ustx", (("singer", "Beta"),)),),
            ),
        )
        for readiness, decision in (
            (CandidateReadiness.READY, "rerun"),
            (CandidateReadiness.UNKNOWN, "unknown"),
            (CandidateReadiness.BLOCKED, "not-rerunnable"),
        ):
            with self.subTest(readiness=readiness):
                request = self.request(candidate, readiness=readiness)
                result = self.assess(request)
                self.assertEqual(result.decision, decision)
                if readiness != CandidateReadiness.READY:
                    with mock.patch.object(self.registry, "start") as start:
                        outcome = submit_rerun(
                            request, catalog=self.catalog, registry=self.registry,
                            adapter=mock.Mock(),
                        )
                    start.assert_not_called()
                    self.assertEqual(outcome.decision, decision)
                    self.assertIn("readiness_" + readiness.value, {
                        reason.code for reason in result.reasons
                    })

    def test_changed_context_still_requires_exact_replacement_readiness(self):
        candidate = replace(self.operation, inputs=(
            replace(self.operation.inputs[0], artifact_id="c" * 32), self.operation.inputs[1],
        ))
        request = replace(self.request(), candidate=candidate)
        self.assertEqual(self.assess(request).decision, "not-rerunnable")

    def test_changed_context_does_not_substitute_workflow_or_operation(self):
        candidate = replace(
            self.operation, name="abc", production=ProductionConfiguration("melisma-phase1"),
            expected=(ExpectedArtifact(Path("/fixture/new.abc"), "abc"),),
        )
        disguised = RerunRequest(
            "generate-abc", candidate,
            (RerunTrigger("failed", "generate-abc", candidate.stage, self.old.id),),
            (BoundReadiness(candidate, ReadinessAssessment(
                "generate-abc", candidate.stage, CandidateReadiness.READY,
            )),),
        )
        self.assertEqual(self.assess(disguised).decision, "not-rerunnable")
        request = self.request()
        wrong_workflow = replace(
            request, triggers=(replace(request.triggers[0], workflow="generate-abc"),),
        )
        self.assertEqual(self.assess(wrong_workflow).decision, "not-rerunnable")

    def test_stopped_and_cancelled_are_positively_eligible(self):
        for state, kind in (
            (ProcessState.SIGNALLED, "stopped"), (ProcessState.CANCELLED, "cancelled"),
        ):
            run = self.attempt(self.operation, state, stopped=True)
            result = self.assess(self.request(run=run, kind=kind))
            self.assertEqual(result.decision, "rerun")
            self.assertIn(kind + "_attempt_selected", {item.code for item in result.reasons})
            self.assertEqual(self.assess(self.request(run=run)).decision, "not-rerunnable")

    def test_stopped_and_cancelled_allow_corrective_context(self):
        candidate = replace(
            self.operation, production=ProductionConfiguration(
                "melisma-phase1", output_settings=(("ustx", (("singer", "Beta"),)),),
            ),
        )
        for state, kind in (
            (ProcessState.SIGNALLED, "stopped"), (ProcessState.CANCELLED, "cancelled"),
        ):
            run = self.attempt(self.operation, state, stopped=True)
            previous = run.result
            with self.subTest(kind=kind):
                result = self.assess(self.request(candidate, run=run, kind=kind))
                self.assertEqual(result.decision, "rerun")
                self.assertIs(run.result, previous)
                self.assertEqual(run.operation, self.operation)

    def test_cancelled_playback_validation_allows_corrective_audio_selection(self):
        operation = Operation(
            "ffplay", "play", self.operation.command, (),
            inputs=(replace(self.operation.inputs[0], role="audio", type="audio"),),
        )
        validation = ProcessResult(operation.command, ProcessState.CANCELLED, None)
        old = self.attempt(operation, ProcessState.REJECTED, stopped=True, validation=(validation,))
        request = RerunRequest(
            "play-audio", operation,
            (RerunTrigger("cancelled", "play-audio", operation.stage, old.id),),
            (BoundReadiness(operation, ReadinessAssessment(
                "play-audio", operation.stage, CandidateReadiness.READY,
            )),),
        )
        self.assertEqual(self.assess(request).decision, "rerun")
        changed = replace(operation, inputs=(replace(operation.inputs[0], artifact_id="d" * 32),))
        changed_request = replace(request, candidate=changed, readiness=(BoundReadiness(
            changed, request.readiness[0].assessment,
        ),))
        self.assertEqual(self.assess(changed_request).decision, "rerun")

    def test_stale_evidence_keeps_exact_historical_production(self):
        f = production_fixtures.StalePropagationTests(methodName="runTest")
        self.addCleanup(f.doCleanups)
        f.setUp()
        candidate = f.catalog.bind_inputs(f.singer_candidate())
        assessment = f.by_name(f.assess({"ustx": candidate}))["ustx"]
        self.assertEqual(assessment.currentness, "stale")
        trigger = RerunTrigger(
            "stale", "generate-ustx", candidate.stage, assessment.producer_run,
            assessment.artifact_id, assessment.artifact_type, assessment,
        )
        request = replace(self.request(candidate), triggers=(trigger,))
        registry = RunRegistry(catalog=f.catalog)
        self.addCleanup(registry.close)
        self.assertEqual(assess_rerun(
            request, catalog=f.catalog, registry=registry,
        ).decision, "rerun")
        other = f.record("other", f.melisma.build(
            "ustx", f.midi, f.lyrics, f.root / "other.ustx", publish_stdout=True,
        ), "ustx", execute=True)
        for updates in (
            {"run_id": other.producer_run}, {"artifact_id": other.id},
            {"run_id": other.producer_run, "artifact_id": other.id},
        ):
            with self.subTest(updates=updates):
                result = assess_rerun(
                    replace(request, triggers=(replace(trigger, **updates),)),
                    catalog=f.catalog, registry=registry,
                )
                self.assertEqual(result.decision, "not-rerunnable")

    def test_known_readiness_prevention_dominates_missing_evidence(self):
        request = replace(self.request(readiness=CandidateReadiness.BLOCKED), triggers=())
        self.assertEqual(self.assess(request).decision, "not-rerunnable")

    def test_submission_denial_does_not_start(self):
        with mock.patch.object(self.registry, "start") as start:
            result = submit_rerun(
                self.request(readiness=CandidateReadiness.UNKNOWN),
                catalog=self.catalog, registry=self.registry, adapter=mock.Mock(),
            )
        start.assert_not_called()
        self.assertEqual(result.decision, "unknown")
        self.assertIsNone(result.run_id)


if __name__ == "__main__":
    unittest.main()
