"""Step 5 model-free candidate readiness; docstrings name the mutations caught."""

import copy
import importlib
import json
import signal
import sys
import unittest
from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import OpenUtauAdapter
from tools.orchestration.capabilities import (
    Capability, Provenance, Readiness, SourceIdentity, State, Verification,
)
from tools.orchestration.configuration import Configuration
from tools.orchestration.execution import Execution, RunRegistry, StageState, stage_document
from tools.orchestration.process import (
    ExpectedArtifact, OperationInput, OperationResult, ProcessResult, ProcessState,
)
from tools.orchestration.provenance import production_provenance
from tools.web_ui.currentness import (
    Availability, Currentness, CurrentnessReason, assess_currentness,
)
import test_stale_propagation as production_fixtures


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.f = production_fixtures.StalePropagationTests(methodName="runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.catalog = self.f.catalog
        self.currentness = self.f.assess()
        self.render = self.catalog.bind_inputs(OpenUtauAdapter(
            self.f.executable, self.f.root,
        ).build(Path(self.f.entries["ustx"].path), self.f.root / "candidate-vocal.wav"))
        self.mix = self.catalog.bind_inputs(self.f.mix_candidate())
        self.abc = self.catalog.bind_inputs(self.f.melisma.build(
            "abc", self.f.midi, self.f.lyrics, self.f.root / "candidate.abc",
        ))
        self.model = importlib.import_module("tools.web_ui.readiness")

    def capability(self, operation, tool=None, **updates):
        tool = tool or operation.tool
        source = SourceIdentity(
            tool, f"{operation.command.cwd}|{operation.command.executable}",
            operation.production.contract if tool == operation.tool and operation.production
            else f"{tool}-phase1",
        )
        values = dict(
            source=source, key="executable", value=str(operation.command.executable),
            provenance=Provenance.FILESYSTEM, verification=Verification.OBSERVED,
            state=State.READY, readiness=Readiness.READY,
        )
        values.update(updates)
        return Capability(**values)

    def evidence(self, operation, **updates):
        capabilities = (self.capability(operation),)
        if any(item.kind == "audio" for item in operation.expected):
            capabilities += (self.capability(operation, "ffprobe"),)
        values = dict(
            candidate=operation, configuration=StageState.READY,
            validated=True, capabilities=capabilities,
        )
        values.update(updates)
        return self.model.CandidateEvidence(**values)

    def assess(self, operation=None, workflow="render-vocals", **updates):
        operation = self.render if operation is None else operation
        values = dict(evidence=self.evidence(operation), currentness=self.currentness)
        values.update(updates)
        return self.model.assess_readiness(self.catalog, workflow, operation, **values)

    def helper_generation_candidate(self):
        return self.catalog.bind_inputs(self.f.helper.generate(
            Path(self.f.entries["prepare"].path).parent, self.f.root / "qualification-helper",
            models_root=self.f.root / "models", device="cpu",
        ))

    def assert_status(self, assessment, expected, *codes):
        self.assertEqual(assessment.readiness.value, expected)
        self.assertTrue(set(codes) <= {reason.code.value for reason in assessment.reasons},
                        assessment.reasons)
        return assessment

    def update_entry(self, name, **updates):
        entry = self.f.entries[name]
        self.catalog.entries[entry.id] = replace(entry, **updates)

    def stale(self, name, **updates):
        entry = self.f.entries[name]
        values = dict(
            currentness=Currentness.STALE,
            reasons=(CurrentnessReason("setting_changed", field="settings.singer"),),
        )
        values.update(updates)
        return tuple(
            replace(item, **values) if item.artifact_id == entry.id else item
            for item in self.currentness
        )

    def test_complete_candidate_ready_and_result_immutable(self):
        """Catches blanket blocking, existence-only readiness and mutable results."""
        result = self.assert_status(self.assess(), "ready")
        self.assertEqual(result.reasons, ())
        self.assertEqual(json.loads(json.dumps(asdict(result)))["readiness"], "ready")
        with self.assertRaises(AttributeError):
            result.readiness = self.model.CandidateReadiness.BLOCKED
        self.assertEqual({item.value for item in self.model.CandidateReadiness},
                         {"ready", "blocked", "unknown"})

    def test_required_role_missing(self):
        """Catches trusting construction evidence after a required selection is lost."""
        candidate = replace(self.render, inputs=())
        self.assert_status(self.assess(candidate), "blocked", "required_input_missing")

    def test_selected_missing_artifact(self):
        """Catches successful producer history blessing a now missing selected file."""
        self.update_entry("ustx", exists=False, valid=False, revision=None)
        self.assert_status(self.assess(), "blocked", "artifact_missing")

    def test_selected_invalid_artifact(self):
        """Catches using existence or successful attempt instead of input validation."""
        self.update_entry("ustx", valid=False)
        self.assert_status(self.assess(), "blocked", "artifact_invalid")

    def test_unknown_validity_and_capture(self):
        """Catches promoting incomplete validation/identity/revision to Ready or Blocked."""
        for changes in ({"valid": None}, {"revision": None}):
            with self.subTest(changes=changes):
                self.update_entry("ustx", **changes)
                self.assert_status(self.assess(), "unknown")
                self.catalog.entries[self.f.entries["ustx"].id] = self.f.entries["ustx"]
        for changes in ({"artifact_id": None}, {"revision": None}):
            candidate = replace(self.render, inputs=(replace(self.render.inputs[0], **changes),))
            with self.subTest(changes=changes):
                self.assert_status(self.assess(candidate), "unknown", "evidence_missing")

    def test_no_candidate_and_no_evidence_are_unknown(self):
        """Catches inventing a candidate or interpreting omitted evidence as failure."""
        self.assert_status(self.model.assess_readiness(
            self.catalog, "render-vocals", None,
        ), "unknown", "evidence_missing")
        self.assert_status(self.assess(evidence=None), "unknown", "evidence_missing")

    def test_unknown_currentness_is_neither_stale_nor_ready(self):
        """Catches both Unknown -> Ready and Unknown -> Blocked coercion."""
        assessments = self.stale("ustx", currentness=Currentness.UNKNOWN)
        result = self.assert_status(self.assess(currentness=assessments), "unknown",
                                    "artifact_currentness_unknown")
        self.assertNotIn("artifact_stale", {reason.code.value for reason in result.reasons})
        self.assert_status(self.assess(currentness=()), "unknown", "artifact_currentness_unknown")

    def test_legacy_generated_input_without_provenance_remains_unknown(self):
        """Catches blessing legacy generated bytes using existence, old success or unqualified CURRENT."""
        entry = self.f.entries["ustx"]
        self.catalog.entries[entry.id] = replace(entry, provenance=None)
        run = self.catalog.runs[entry.producer_run]
        self.catalog.runs[run.id] = replace(run, productions=())
        self.assert_status(self.assess(), "unknown", "artifact_currentness_unknown")

    def test_each_selected_stale_input_blocks_with_immediate_safe_source(self):
        """Catches treating stale as missing or losing the actual stale role/source."""
        for name, candidate, workflow, role in (
            ("ustx", self.render, "render-vocals", "ustx"),
            ("vocal", self.mix, "mix-audio", "vocal"),
            ("instrumental", self.mix, "mix-audio", "instrumental"),
        ):
            with self.subTest(name=name):
                reason = CurrentnessReason(
                    "upstream_stale", upstream_artifact=self.f.entries["abc"].id,
                    upstream_workflow="generate-abc", upstream_run="abc",
                )
                result = self.assert_status(self.assess(
                    candidate, workflow, currentness=self.stale(name, reasons=(reason,)),
                ), "blocked", "artifact_stale")
                stale, = (item for item in result.reasons if item.code.value == "artifact_stale")
                self.assertEqual((stale.input_role, stale.artifact_id, stale.artifact_type),
                                 (role, self.f.entries[name].id, self.f.entries[name].type))
                self.assertEqual(stale.source_code, "upstream_stale")
                self.assertEqual(stale.upstream_artifact, self.f.entries["abc"].id)
                self.assertEqual(stale.upstream_workflow, "generate-abc")
                self.assertNotIn("artifact_missing", {item.code.value for item in result.reasons})

    def test_unselected_failed_stale_branches_do_not_block(self):
        """Catches transitive/global failure blocking across every independent branch."""
        for name in ("vocal", "native", "mix"):
            operation = self.f.operations[name]
            result = OperationResult(
                operation, ProcessResult(operation.command, ProcessState.START_FAILED, None), (),
            )
            self.catalog.register_result(result, f"later-failed-{name}")
        for candidate, workflow, unrelated in (
            (self.abc, "generate-abc", "vocal"),
            (self.render, "render-vocals", "native"),
            (self.render, "render-vocals", "mix"),
            (self.mix, "mix-audio", "native"),
        ):
            with self.subTest(workflow=workflow, unrelated=unrelated):
                self.assert_status(self.assess(
                    candidate, workflow, currentness=self.stale(unrelated),
                ), "ready")

    def test_preparation_generic_audio_and_native_stale_do_not_block_final_mix(self):
        """Catches broad helper/native workflow matching instead of selected artifact ID/type."""
        _, entries = self.f.complete_manifest_production("prepare", ("reference.wav",))
        self.catalog = self.f.catalog
        generic = next(item for item in entries if item.type == "audio")
        operation = self.f.helper.prepare(
            Path(self.f.entries["abc"].path), self.f.root / "new-preparation",
            style="different-style", run_id="prepared", seed=42,
        )
        assessments = assess_currentness(self.catalog, (
            self.f.model().ProductionSelection(
                "generate-yue2-audio", generic.id, self.catalog.bind_inputs(operation),
            ),
        ))
        self.assertEqual(assessments[0].currentness, Currentness.STALE)
        self.assertEqual(assessments[0].artifact_type, "audio")
        self.assert_status(self.assess(
            self.mix, "mix-audio", currentness=(*self.stale("native"), *assessments),
        ), "ready")

    def test_failed_mix_playback_and_mix_gain_changes_do_not_block_upstream(self):
        """Catches reverse dependency blocking and treating mix configuration as global."""
        registry = RunRegistry(catalog=self.catalog)
        self.addCleanup(registry.close)
        for stage in ("ffmpeg", "ffplay"):
            candidate = replace(self.mix, tool=stage, name="mix" if stage == "ffmpeg" else "play")
            run = Execution(candidate, output_limit=1024)
            run.result = OperationResult(
                candidate, ProcessResult(candidate.command, ProcessState.CANCELLED, None), (),
            )
            registry.runs[run.id] = run
        noisy = replace(self.render, metadata=(*self.render.metadata, ("mix_gain", "99")))
        self.assert_status(self.assess(noisy, registry=registry,
                                       currentness=self.stale("mix")), "ready")
        self.assert_status(self.assess(self.abc, "generate-abc", registry=registry), "ready")
        native = self.catalog.bind_inputs(self.f.native_candidate())
        self.assert_status(self.assess(native, "generate-yue2-audio", registry=registry), "ready")

    def test_retained_success_after_failed_stopped_cancelled_attempts(self):
        """Catches latest-attempt blocking instead of selecting earlier successful bytes."""
        for name in ("ustx", "vocal", "instrumental"):
            operation = self.f.operations[name]
            for state in (ProcessState.START_FAILED, ProcessState.CANCELLED, ProcessState.SIGNALLED):
                process = ProcessResult(
                    operation.command, state, -15 if state == ProcessState.SIGNALLED else None,
                    stop_requested=state == ProcessState.SIGNALLED,
                )
                self.catalog.register_result(OperationResult(operation, process, ()),
                                             f"retry-{name}-{state.value}")
        self.assert_status(self.assess(), "ready")
        self.assert_status(self.assess(self.mix, "mix-audio"), "ready")
        self.assertTrue(all(self.catalog.runs[name].success
                            for name in ("ustx", "vocal", "instrumental")))

    def test_selecting_later_bad_output_does_not_borrow_earlier_success(self):
        """Catches accepting any successful artifact from the same producer stage."""
        earlier = self.f.entries["ustx"]
        for index, (exists, valid, code) in enumerate((
            (False, False, "artifact_missing"), (True, False, "artifact_invalid"),
        )):
            entry = replace(
                earlier, id=f"{index + 100:032x}", path=str(self.f.root / f"bad-{index}.ustx"),
                exists=exists, valid=valid, production_status="partial",
                producer_run="later-failed", provenance=None,
            )
            self.catalog.entries[entry.id] = entry
            candidate = replace(self.render, inputs=(OperationInput(
                "ustx", Path(entry.path), "ustx", entry.id, entry.revision,
            ),))
            self.assert_status(self.assess(candidate), "blocked", code)

    def test_imported_ustx_and_abc_need_no_producer(self):
        """Catches requiring producer execution/provenance for a valid imported selection."""
        for kind in ("ustx", "abc"):
            path = self.f.file(f"manual.{kind}", Path(self.f.entries[kind].path).read_text())
            entry = self.catalog.register_input(path, kind)
            if kind == "ustx":
                candidate = OpenUtauAdapter(self.f.executable, self.f.root).build(
                    path, self.f.root / "imported-render.wav",
                )
                workflow = "render-vocals"
            else:
                candidate = self.f.native_candidate("scored", abc=path)
                workflow = "generate-yue2-audio"
            candidate = self.catalog.bind_inputs(candidate)
            self.assertIsNone(entry.producer_run)
            self.assert_status(self.assess(candidate, workflow, currentness=()), "ready")

    def test_imported_generic_audio_respects_role_compatibility(self):
        """Catches rejecting accepted generic vocal audio or granting final instrumental authority."""
        entry = self.catalog.register_input(self.f.file("manual.wav", "audio"), "audio")
        self.catalog.entries[entry.id] = replace(entry, valid=True)
        vocal = OperationInput("vocal", Path(entry.path), "vocal-audio", entry.id, entry.revision)
        candidate = replace(self.mix, inputs=(self.mix.inputs[0], vocal))
        self.assert_status(self.assess(candidate, "mix-audio"), "ready")
        instrumental = replace(vocal, role="instrumental", type="instrumental-audio")
        candidate = replace(self.mix, inputs=(instrumental, self.mix.inputs[1]))
        self.assert_status(self.assess(candidate, "mix-audio"), "blocked", "artifact_type_incompatible")

    def test_native_and_preparation_audio_cannot_be_selected_as_final_instrumental(self):
        """Catches native/helper type collapse and preparation role-based relabeling."""
        original = self.f.entries["instrumental"]
        for stage, type_ in (
            ("yue2-native.generate", "yue2-audio"),
            ("yue2-instrumental.prepare", "audio"),
            ("yue2-instrumental.prepare", "instrumental-audio"),
        ):
            self.catalog.entries[original.id] = replace(original, stage=stage, type=type_)
            with self.subTest(stage=stage, type=type_):
                self.assert_status(self.assess(self.mix, "mix-audio"), "blocked",
                                   "artifact_type_incompatible")

    def test_unavailable_missing_disabled_and_invalid_configuration(self):
        """Catches conflating missing supplied evidence with known unavailable/configuration failure."""
        unavailable = self.capability(
            self.render, state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE,
            verification=Verification.SOURCE, reason="tool is absent",
        )
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, capabilities=(unavailable,),
        )), "blocked", "capability_unavailable")
        for configuration, code in (
            (StageState.NOT_CONFIGURED, "configuration_missing"),
            (StageState.DISABLED, "capability_unavailable"),
        ):
            self.assert_status(self.assess(evidence=self.evidence(
                self.render, configuration=configuration,
            )), "blocked", code)
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, validated=False,
        )), "blocked", "configuration_invalid")

    def test_missing_or_only_configured_capability_evidence_is_unknown(self):
        """Catches saved configured strings or source support masquerading as runtime readiness."""
        for capabilities in (
            (), (self.capability(self.render),),
            (self.capability(self.render, key="singer"), self.capability(self.render, "ffprobe")),
            (self.capability(
                self.render, state=State.CONFIGURED, readiness=Readiness.UNKNOWN,
                provenance=Provenance.CONFIGURED, verification=Verification.UNVERIFIED,
            ), self.capability(self.render, "ffprobe")),
        ):
            self.assert_status(self.assess(evidence=self.evidence(
                self.render, capabilities=capabilities,
            )), "unknown", "evidence_missing")
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, configuration=None,
        )), "unknown", "evidence_missing")
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, validated=None,
        )), "unknown", "evidence_missing")

    def test_discovery_error_does_not_prove_tool_unavailable(self):
        """Catches interpreting a failed metadata query with unknown readiness as definite absence."""
        error = self.capability(
            self.render, state=State.ERROR, readiness=Readiness.UNKNOWN,
            verification=Verification.UNVERIFIED, reason="metadata query failed",
        )
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, capabilities=(error, self.capability(self.render, "ffprobe")),
        )), "unknown", "evidence_missing")

    def test_unrelated_unavailable_tool_does_not_block(self):
        """Catches WORKFLOW_TOOLS/global reports becoming required selected capabilities."""
        bad = self.capability(
            self.abc, "openutau", state=State.UNAVAILABLE,
            readiness=Readiness.UNAVAILABLE, verification=Verification.SOURCE, reason="absent",
        )
        evidence = self.evidence(self.abc, capabilities=(self.capability(self.abc), bad))
        self.assert_status(self.assess(self.abc, "generate-abc", evidence=evidence), "ready")
        native = self.catalog.bind_inputs(self.f.native_candidate())
        bad_helper = replace(bad, source=replace(bad.source, tool="yue2-instrumental"))
        self.assert_status(self.assess(native, "generate-yue2-audio", evidence=self.evidence(
            native, capabilities=(*self.evidence(native).capabilities, bad_helper),
        )), "ready")

    def test_evidence_is_qualified_by_exact_candidate_and_source(self):
        """Catches borrowing validation or capability availability from another selected source."""
        candidate = replace(self.render, inputs=())
        self.assert_status(self.assess(candidate, evidence=self.evidence(self.render)),
                           "blocked", "required_input_missing", "evidence_missing")
        wrong = replace(self.capability(self.render), source=SourceIdentity(
            "openutau", "/different/source|/different/executable", "openutau-headless-phase1",
        ))
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, capabilities=(wrong, self.capability(self.render, "ffprobe")),
        )), "unknown", "evidence_missing")

    def test_exact_active_output_conflict_without_reservation(self):
        """Catches reserving paths or blocking all candidates when any execution is active."""
        path = self.render.expected[0].path
        self.catalog.active_paths[path] = 2
        before = dict(self.catalog.active_paths)
        self.assert_status(self.assess(), "blocked", "output_path_active")
        self.assertEqual(self.catalog.active_paths, before)
        self.catalog.active_paths = {self.f.root / "unrelated.wav": 1}
        self.assert_status(self.assess(), "ready")

    def test_registry_active_outputs_and_inputs_but_not_completed_outputs(self):
        """Catches ignoring registry-only owners or treating completed ownership as active."""
        registry = RunRegistry()
        run = Execution(self.render, output_limit=1024)
        registry.runs[run.id] = run
        self.assert_status(self.assess(registry=registry), "blocked", "output_path_active")
        operation = replace(self.render, expected=(ExpectedArtifact(self.render.inputs[0].path),))
        run.operation = operation
        self.assert_status(self.assess(registry=registry), "blocked", "input_path_active")
        run.result = OperationResult(
            operation, ProcessResult(operation.command, ProcessState.EXITED, 1), (),
        )
        self.assert_status(self.assess(registry=registry), "ready")

    def test_historical_output_uses_existing_no_overwrite_policy(self):
        """Catches blanket historical ownership blocking or allowing observed occupied outputs."""
        old = self.f.entries["vocal"]
        candidate = replace(self.render, expected=(ExpectedArtifact(Path(old.path), "audio"),))
        self.assert_status(self.assess(candidate), "blocked", "output_path_occupied")
        self.update_entry("vocal", exists=False)
        self.assert_status(self.assess(candidate), "ready")

    def test_wrong_type_role_path_identity_and_revision(self):
        """Catches ignoring selected type, role, locator/ID association or captured revision."""
        source = self.render.inputs[0]
        for changes, code in (
            ({"type": "abc"}, "artifact_type_incompatible"),
            ({"role": "abc"}, "artifact_role_mismatch"),
            ({"revision": "f" * 64}, "artifact_revision_mismatch"),
            ({"path": self.f.root / "other.ustx"}, "artifact_identity_mismatch"),
            ({"artifact_id": self.f.entries["abc"].id}, "artifact_identity_mismatch"),
        ):
            with self.subTest(changes=changes):
                candidate = replace(self.render, inputs=(replace(source, **changes),))
                self.assert_status(self.assess(candidate), "blocked", code)

    def test_unknown_id_cannot_fall_back_to_same_stage_or_path(self):
        """Catches falling back to any entry at a stage/path when the selected ID is absent."""
        candidate = replace(self.render, inputs=(replace(
            self.render.inputs[0], artifact_id="e" * 32,
        ),))
        self.assert_status(self.assess(candidate), "unknown", "evidence_missing")

    def test_wrong_assessment_artifact_revision_type_and_run_cannot_satisfy_selection(self):
        """Catches currentness evidence borrowed by stage, type alone or reused identity at new bytes."""
        entry = self.f.entries["ustx"]
        assessment = next(item for item in self.currentness if item.artifact_id == entry.id)
        for changes in (
            {"artifact_id": self.f.entries["abc"].id},
            {"artifact_type": "abc"},
            {"producer_run": "not-the-selected-production"},
            {"workflow": "generate-abc"},
        ):
            with self.subTest(changes=changes):
                self.assert_status(self.assess(currentness=(replace(assessment, **changes),)),
                                   "unknown", "artifact_currentness_unknown")
        self.catalog.entries[entry.id] = replace(entry, revision="f" * 64)
        candidate = replace(self.render, inputs=(replace(self.render.inputs[0], revision="f" * 64),))
        self.assert_status(self.assess(candidate, currentness=(assessment,)), "unknown",
                           "artifact_currentness_unknown")

    def test_conflicting_selected_roles_and_lineage_are_not_guessed(self):
        """Catches choosing the first duplicate role or arbitrarily choosing a producer run."""
        candidate = replace(self.render, inputs=(
            *self.render.inputs, replace(self.render.inputs[0], artifact_id="e" * 32),
        ))
        self.assert_status(self.assess(candidate), "blocked", "selected_lineage_ambiguous")
        assessment = next(item for item in self.currentness
                          if item.artifact_id == self.f.entries["ustx"].id)
        conflicting = replace(assessment, currentness=Currentness.UNKNOWN)
        self.assert_status(self.assess(currentness=(assessment, conflicting)), "unknown",
                           "artifact_currentness_unknown")
        run = self.catalog.runs[assessment.producer_run]
        self.catalog.runs["second-same-bytes"] = replace(run, id="second-same-bytes")
        second = replace(assessment, producer_run="second-same-bytes")
        self.assert_status(self.assess(currentness=(assessment, second)), "blocked",
                           "selected_lineage_ambiguous")

    def test_required_role_duplicates_agree_with_provenance(self):
        """Catches identical actual input occurrences disappearing before ambiguity checks."""
        for operation, workflow in (
            (self.render, "render-vocals"), (self.mix, "mix-audio"), (self.abc, "generate-abc"),
        ):
            for source in operation.inputs:
                for duplicate in (source, replace(source, revision="f" * 64)):
                    with self.subTest(stage=operation.stage, role=source.role, duplicate=duplicate):
                        candidate = replace(operation, inputs=(*operation.inputs, duplicate))
                        before = copy.deepcopy((
                            candidate, self.catalog.entries, self.catalog.runs, self.currentness,
                        ))
                        output_type = self.model.semantic_output_type(
                            candidate.stage, candidate.expected[0],
                        )
                        with self.assertRaisesRegex(ValueError, "duplicate production input roles"):
                            production_provenance(candidate, output_type)
                        self.assert_status(self.assess(candidate, workflow), "blocked",
                                           "selected_lineage_ambiguous")
                        self.assertEqual(
                            (candidate, self.catalog.entries, self.catalog.runs, self.currentness),
                            before,
                        )

    def test_duplicate_required_role_reasons_are_complete_and_order_independent(self):
        """Catches lost role multiplicity, first-error-only or order-dependent ambiguity reasons."""
        candidate = replace(self.mix, inputs=self.mix.inputs * 3)
        first = self.assert_status(self.assess(candidate, "mix-audio"), "blocked",
                                   "selected_lineage_ambiguous")
        second = self.assess(replace(
            candidate, inputs=tuple(reversed(candidate.inputs)),
        ), "mix-audio")
        self.assertEqual(first, second)
        self.assertEqual(len(first.reasons), 2)
        self.assertEqual(len(first.reasons), len(set(first.reasons)))
        self.assertEqual([item.input_role for item in first.reasons], ["instrumental", "vocal"])

    def test_duplicate_evidence_with_single_candidate_role_is_harmless(self):
        """Catches treating repeated supplied evidence as duplicate actual candidate roles."""
        self.assertEqual(len(self.render.inputs), 1)
        entry = self.catalog.get(self.render.inputs[0].artifact_id)
        evidence = self.evidence(self.render)
        before = copy.deepcopy((self.catalog.entries, self.currentness, evidence, self.render))
        first = self.assert_status(self.assess(), "ready")
        repeated = replace(evidence, capabilities=evidence.capabilities * 3)
        second = self.assess(evidence=repeated, currentness=self.currentness * 3)
        self.assertEqual(first, second)
        self.assertEqual(entry, self.catalog.get(entry.id))
        self.assertIsNotNone(production_provenance(self.render, "vocal-audio"))
        self.assertEqual((self.catalog.entries, self.currentness, evidence, self.render), before)

    def test_primary_executable_positive_records_must_agree(self):
        """Catches any-matching-record-wins for contradictory positive primary executable values."""
        ready = self.capability(self.render)
        contradictory = replace(ready, value="/synthetic/private-other-executable")
        probe = self.capability(self.render, "ffprobe")
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, capabilities=(ready, ready, probe, probe),
        )), "ready")
        for records in ((contradictory,), (ready, contradictory), (ready, contradictory, ready)):
            capabilities = (*records, probe)
            with self.subTest(records=records):
                first = self.assert_status(self.assess(evidence=self.evidence(
                    self.render, capabilities=capabilities,
                )), "unknown", "evidence_missing")
                self.assertEqual(first, self.assess(evidence=self.evidence(
                    self.render, capabilities=tuple(reversed(capabilities)),
                )))
                self.assertEqual(len(first.reasons), 1)
                serialized = json.dumps(asdict(first))
                self.assertNotIn(contradictory.value, serialized)
                self.assertNotIn(str(self.render.command.executable), serialized)

    def test_primary_executable_unavailable_wins_over_positive_conflicts(self):
        """Catches inconclusive executable conflicts overriding a definite unavailable blocker."""
        ready = self.capability(self.render)
        contradictory = replace(ready, value="/synthetic/other-executable")
        probe = self.capability(self.render, "ffprobe")
        for state, readiness in (
            (State.UNAVAILABLE, Readiness.UNAVAILABLE),
            (State.UNAUTHORIZED, Readiness.UNAUTHORIZED),
        ):
            unavailable = replace(ready, state=state, readiness=readiness, reason="unavailable")
            for records in ((unavailable,), (ready, unavailable), (ready, contradictory, unavailable)):
                capabilities = (*records, probe)
                with self.subTest(state=state, records=records):
                    first = self.assert_status(self.assess(evidence=self.evidence(
                        self.render, capabilities=capabilities,
                    )), "blocked", "capability_unavailable")
                    self.assertEqual(first, self.assess(evidence=self.evidence(
                        self.render, capabilities=tuple(reversed(capabilities)),
                    )))

    def test_primary_executable_evidence_preserves_source_and_observation_boundaries(self):
        """Catches tool-wide selection or unobserved/missing executable evidence becoming Ready."""
        ready = self.capability(self.render)
        probe = self.capability(self.render, "ffprobe")
        foreign = replace(ready, source=replace(ready.source, location="/other|/source"),
                          value="/private-foreign-executable")
        unavailable_foreign = replace(
            foreign, state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE, reason="absent",
        )
        self.assert_status(self.assess(evidence=self.evidence(
            self.render, capabilities=(ready, foreign, unavailable_foreign, probe),
        )), "ready")
        configured = replace(
            ready, state=State.CONFIGURED, readiness=Readiness.UNKNOWN,
            verification=Verification.UNVERIFIED,
        )
        for records in ((), (foreign,), (configured,)):
            self.assert_status(self.assess(evidence=self.evidence(
                self.render, capabilities=(*records, probe),
            )), "unknown", "evidence_missing")

    def test_supplied_operation_positive_records_must_match_candidate(self):
        """Catches wrong operation values and matching-plus-contradictory positive evidence."""
        candidate = self.helper_generation_candidate()
        baseline = self.evidence(candidate)
        matching = self.capability(candidate, key="operation", value="generate")
        contradictory = replace(matching, value="prepare")
        self.assert_status(self.assess(candidate, "generate-yue2-audio", evidence=replace(
            baseline, capabilities=(*baseline.capabilities, matching, matching),
        )), "ready")
        for records in ((contradictory,), (matching, contradictory), (contradictory, matching, matching)):
            capabilities = (*baseline.capabilities, *records)
            with self.subTest(records=records):
                first = self.assert_status(self.assess(
                    candidate, "generate-yue2-audio",
                    evidence=replace(baseline, capabilities=capabilities),
                ), "unknown", "evidence_missing")
                second = self.assess(candidate, "generate-yue2-audio", evidence=replace(
                    baseline, capabilities=tuple(reversed(capabilities)),
                ))
                self.assertEqual(first, second)
                self.assertEqual(len(first.reasons), 1)

    def test_supplied_operation_unavailable_wins_over_positive_conflicts(self):
        """Catches operation conflicts/unavailability being ignored or overriding Blocked precedence."""
        candidate = self.helper_generation_candidate()
        baseline = self.evidence(candidate)
        matching = self.capability(candidate, key="operation", value="generate")
        contradictory = replace(matching, value="prepare")
        for state, readiness in (
            (State.UNAVAILABLE, Readiness.UNAVAILABLE),
            (State.UNAUTHORIZED, Readiness.UNAUTHORIZED),
        ):
            unavailable = replace(matching, state=state, readiness=readiness, reason="absent")
            for records in ((unavailable,), (matching, unavailable),
                            (matching, contradictory, unavailable)):
                capabilities = (*baseline.capabilities, *records)
                with self.subTest(state=state, records=records):
                    first = self.assert_status(self.assess(
                        candidate, "generate-yue2-audio",
                        evidence=replace(baseline, capabilities=capabilities),
                    ), "blocked", "capability_unavailable")
                    self.assertEqual(first, self.assess(
                        candidate, "generate-yue2-audio", evidence=replace(
                            baseline, capabilities=tuple(reversed(capabilities)),
                        ),
                    ))

    def test_supplied_operation_evidence_is_optional_and_source_qualified(self):
        """Catches inventing operation requirements or applying foreign tool/source operation values."""
        candidate = self.helper_generation_candidate()
        baseline = self.evidence(candidate)
        self.assert_status(self.assess(candidate, "generate-yue2-audio", evidence=baseline), "ready")
        matching = self.capability(candidate, key="operation", value="generate")
        foreign = replace(
            matching, source=replace(matching.source, location="/unselected|/source"),
            value="not-selected", state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE,
            reason="unselected source",
        )
        unrelated = replace(foreign, source=replace(matching.source, tool="yue2-native"))
        capabilities = (*baseline.capabilities, foreign, unrelated)
        self.assert_status(self.assess(candidate, "generate-yue2-audio", evidence=replace(
            baseline, capabilities=capabilities,
        )), "ready")
        configured = replace(
            matching, state=State.CONFIGURED, readiness=Readiness.UNKNOWN,
            verification=Verification.UNVERIFIED,
        )
        self.assert_status(self.assess(candidate, "generate-yue2-audio", evidence=replace(
            baseline, capabilities=(*baseline.capabilities, configured),
        )), "unknown", "evidence_missing")

    def test_qualification_corrections_are_read_only_and_private(self):
        """Catches qualification-path I/O, mutation or leaking raw executable/operation evidence."""
        helper = self.helper_generation_candidate()
        private = "/synthetic/private-capability-value-not-for-diagnostics"
        duplicate = replace(self.render, inputs=self.render.inputs * 2)
        executable_evidence = self.evidence(self.render, capabilities=(
            *self.evidence(self.render).capabilities,
            self.capability(self.render, value=private),
        ))
        operation_evidence = self.evidence(helper, capabilities=(
            *self.evidence(helper).capabilities,
            self.capability(helper, key="operation", value=private),
        ))
        cases = (
            (duplicate, "render-vocals", self.evidence(duplicate), "blocked"),
            (self.render, "render-vocals", executable_evidence, "unknown"),
            (helper, "generate-yue2-audio", operation_evidence, "unknown"),
        )
        objects = (cases, self.currentness, self.catalog.entries,
                   self.catalog.runs, self.catalog.active_paths)
        before = copy.deepcopy(objects)
        bytes_before = self.f.history.read_bytes()
        with ExitStack() as stack:
            for owner, names in (
                (self.catalog, ("bind_inputs", "refresh", "begin", "end", "_commit")),
                (Path, ("open", "read_bytes", "read_text", "resolve", "stat", "exists",
                        "is_file", "unlink", "write_text", "write_bytes")),
                (RunRegistry, ("start",)),
            ):
                for name in names:
                    stack.enter_context(mock.patch.object(
                        owner, name, side_effect=AssertionError(f"forbidden {name}"),
                    ))
            for target in (
                "builtins.open", "os.open", "os.stat", "subprocess.Popen",
                "tools.orchestration.artifact_catalog.fingerprint",
                "tools.orchestration.discovery.DiscoveryService.discover",
                "tools.orchestration.configuration.validate_configuration",
                "tools.orchestration.yue2.verify_manifest",
            ):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("forbidden I/O")))
            for candidate, workflow, evidence, expected in cases:
                result = self.assert_status(
                    self.assess(candidate, workflow, evidence=evidence), expected,
                )
                serialized = json.dumps(asdict(result))
                for value in (private, str(self.f.root), "capabilities", "provenance"):
                    self.assertNotIn(value, serialized)
        self.assertEqual(objects, before)
        self.assertEqual(self.f.history.read_bytes(), bytes_before)

    def test_candidate_ownership_output_and_construction_contract(self):
        """Catches broad tool/workflow membership or nonempty outputs as production authority."""
        for candidate, workflow in (
            (self.abc, "generate-ustx"),
            (replace(self.render, name="unknown"), "render-vocals"),
            (replace(self.render, expected=()), "render-vocals"),
            (replace(self.render, expected=(ExpectedArtifact(self.f.root / "wrong.abc", "abc"),)),
             "render-vocals"),
            (self.abc, "full-pipeline"),
        ):
            with self.subTest(stage=candidate.stage, workflow=workflow):
                self.assert_status(self.assess(candidate, workflow), "blocked",
                                   "candidate_contract_invalid")
        self.assert_status(self.assess(replace(self.render, production=None)), "unknown",
                           "evidence_missing")

    def test_bundle_and_manifest_extensible_outputs_are_not_exhaustive_allowlists(self):
        """Catches M1 weakening and M2 required-output-as-exhaustive overvalidation."""
        bundle = self.catalog.bind_inputs(self.f.melisma.build(
            "render-inputs", self.f.midi, self.f.lyrics, self.f.root / "candidate-bundle",
        ))
        for workflow in ("generate-abc", "generate-ustx"):
            self.assert_status(self.assess(bundle, workflow), "ready")
        native = self.catalog.bind_inputs(self.f.native_candidate(cot="off"))
        optional = ExpectedArtifact(native.expected[0].path.parent / "extra.npy")
        native = replace(native, expected=(*native.expected, optional))
        self.assert_status(self.assess(native, "generate-yue2-audio"), "ready")
        native = replace(native, inputs=())
        self.assert_status(self.assess(native, "generate-yue2-audio"), "ready")

    def test_helper_generate_requires_its_selected_prepared_manifest_not_abc(self):
        """Catches treating a grouped workflow ABC edge as every helper operation's input."""
        candidate = self.catalog.bind_inputs(self.f.helper.generate(
            Path(self.f.entries["prepare"].path).parent, self.f.root / "new-helper",
            models_root=self.f.root / "models", device="cpu",
        ))
        self.assert_status(self.assess(candidate, "generate-yue2-audio"), "ready")
        self.assert_status(self.assess(replace(candidate, inputs=()), "generate-yue2-audio"),
                           "blocked", "required_input_missing")

    def test_adapter_requirements_match_actual_constructed_inputs(self):
        """Catches declarative required-role drift from actual adapter construction contracts."""
        for name, operation in self.f.operations.items():
            adapter = next(item for item in self.model._ADAPTERS if item.tool == operation.tool)
            required = dict(adapter.required_inputs[operation.name])
            actual = {item.role: item.type for item in operation.inputs}
            with self.subTest(operation=name):
                self.assertTrue(required.items() <= actual.items())

    def test_deterministic_complete_reasons_and_blocked_precedence(self):
        """Catches first-error-only, duplicate reasons and Unknown overriding a definite blocker."""
        candidate = replace(self.mix, inputs=tuple(reversed(self.mix.inputs)))
        self.update_entry("vocal", exists=False)
        self.update_entry("instrumental", valid=False)
        values = (*self.stale("native"), *self.currentness, *self.currentness)
        first = self.assert_status(self.assess(
            candidate, "mix-audio", evidence=None, currentness=values,
        ), "blocked", "artifact_missing", "artifact_invalid", "evidence_missing")
        second = self.assess(
            replace(candidate, inputs=tuple(reversed(candidate.inputs))), "mix-audio",
            evidence=None, currentness=tuple(reversed(values)),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first.reasons), len(set(first.reasons)))
        keys = [json.dumps(asdict(item), sort_keys=True) for item in first.reasons]
        self.assertEqual(keys, sorted(keys))

    def test_safe_reasons_never_copy_private_evidence(self):
        """Catches copying raw diagnostics, arbitrary identifiers/settings or provenance into reasons."""
        private = "Synthetic complete lyrics, private environment and raw command contents"
        candidate = replace(
            self.render, metadata=(("arbitrary", private),),
            command=replace(self.render.command, arguments=("--lyrics", private),
                            environment=(("SYNTHETIC_PRIVATE", private),)),
        )
        reason = CurrentnessReason(
            "setting_changed", field=private, upstream_artifact=private,
            upstream_workflow=private, upstream_run=private,
        )
        result = self.assert_status(self.assess(
            candidate, currentness=self.stale("ustx", reasons=(reason,)),
        ), "blocked", "artifact_stale")
        serialized = json.dumps(asdict(result))
        for value in (private, "--lyrics", "SYNTHETIC_PRIVATE", str(self.f.root), "provenance"):
            self.assertNotIn(value, serialized)

    def test_unsafe_unknown_identifiers_are_not_echoed(self):
        """Catches leaking user-controlled invalid roles, types, IDs, workflow or stage labels."""
        private = "synthetic-private-identifier-not-a-contract"
        candidate = replace(self.render, inputs=(replace(
            self.render.inputs[0], role=private, type=private, artifact_id=private,
        ),))
        result = self.assert_status(self.assess(candidate), "blocked", "artifact_role_mismatch")
        self.assertNotIn(private, json.dumps(asdict(result)))
        result = self.assess(replace(candidate, name=private), workflow=private)
        self.assertEqual(result.readiness.value, "blocked")
        self.assertNotIn(private, json.dumps(asdict(result)))

    def test_no_io_no_mutation_no_stage_state_replacement(self):
        """Catches refresh/hash/read/discover/begin/start/write and persisted readiness/stale deletion."""
        configuration = Configuration()
        document = stage_document("openutau.render", StageState.FAILED, catalog=self.catalog,
                                  run_id="retained-failure")
        evidence = self.evidence(self.render)
        assessments = self.stale("ustx")
        objects = (configuration, document, self.render, evidence, assessments,
                   self.catalog.entries, self.catalog.runs, self.catalog.active_paths)
        before = copy.deepcopy(objects)
        bytes_before = self.f.history.read_bytes()
        with ExitStack() as stack:
            for owner, names in (
                (self.catalog, ("bind_inputs", "refresh", "begin", "_commit", "end")),
                (Path, ("open", "resolve", "stat", "exists", "is_file", "unlink", "write_text")),
                (RunRegistry, ("start",)),
            ):
                for name in names:
                    stack.enter_context(mock.patch.object(
                        owner, name, side_effect=AssertionError(f"forbidden {name}"),
                    ))
            for target in (
                "builtins.open", "os.open", "os.stat", "subprocess.Popen",
                "tools.orchestration.artifact_catalog.fingerprint",
                "tools.orchestration.discovery.DiscoveryService.discover",
                "tools.orchestration.configuration.validate_configuration",
                "tools.orchestration.yue2.verify_manifest",
            ):
                stack.enter_context(mock.patch(target, side_effect=AssertionError("forbidden I/O")))
            self.assert_status(self.assess(evidence=evidence, currentness=assessments), "blocked",
                               "artifact_stale")
        self.assertEqual(objects, before)
        self.assertEqual(self.f.history.read_bytes(), bytes_before)
        self.assertTrue(Path(self.f.entries["ustx"].path).is_file())
        self.assertEqual(document["state"], "failed")

    def test_duplicate_cyclic_defensive_evidence_terminates(self):
        """Catches recursive dependency traversal and unbounded immediate-reason expansion."""
        entry = self.f.entries["ustx"]
        assessment = next(item for item in self.currentness if item.artifact_id == entry.id)
        cycle = replace(assessment, currentness=Currentness.STALE, reasons=(
            CurrentnessReason("upstream_stale", upstream_artifact=entry.id,
                              upstream_workflow="generate-ustx"),
        ))
        handler = signal.signal(signal.SIGALRM, lambda *_: self.fail("readiness did not terminate"))
        signal.alarm(5)
        try:
            result = self.assert_status(self.assess(currentness=(cycle,) * 30), "blocked",
                                        "artifact_stale")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, handler)
        self.assertEqual(len(result.reasons), 1)


if __name__ == "__main__":
    unittest.main()
