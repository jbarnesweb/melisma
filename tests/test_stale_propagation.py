"""Read-only currentness of selected successful productions, not rerun planning."""

import hashlib
import importlib
import json
import signal
import sys
import tempfile
import unittest
import wave
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from tools.orchestration.artifact_catalog import ArtifactCatalog, ArtifactProduction
from tools.orchestration.artifacts import inspect_artifact
from tools.orchestration.execution import RunRegistry, StageState, stage_document
from tools.orchestration.playback import FFplayAdapter
from tools.orchestration.process import (
    ExpectedArtifact, OperationInput, OperationResult, ProcessResult, ProcessRunner, ProcessState,
)
from tools.orchestration.provenance import ProductionInput
from tools.orchestration.yue2 import (
    HELPER_FILES, NATIVE_FILES, PREPARED_FILES, InstrumentalAdapter, NativeYuE2Adapter,
)
from tools.web_ui.workflows import accepted_artifact_types
from test_execution import eventually


class StalePropagationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "history.json"
        self.catalog = ArtifactCatalog(self.history)
        self.midi = self.file("input.mid", "MIDI fixture")
        self.lyrics = self.file("lyrics.txt", "file-backed lyric fixture")
        self.executable = self.file("melisma-fixture", (
            f"#!{sys.executable}\nimport sys\n"
            "if sys.argv[2]=='abc':\n"
            " print('X:1\\nM:4/4\\nL:1/32\\nK:C\\nV: Vocal\\nV: Ins')\n"
            "else:\n"
            " print('ustx_version: 0.7\\nresolution: 480\\ntracks:\\nvoice_parts:')\n"
        ))
        self.executable.chmod(0o755)
        self.melisma = MelismaAdapter(self.executable, self.root)
        self.helper = InstrumentalAdapter(Path(sys.executable), self.root)
        self.native = NativeYuE2Adapter(Path(sys.executable), self.root)
        self.operations = {}
        self.entries = {}
        self.workflows = {
            "abc": "generate-abc", "ustx": "generate-ustx", "vocal": "render-vocals",
            "prepare": "generate-yue2-audio", "instrumental": "generate-yue2-audio",
            "native": "generate-yue2-audio", "scored": "generate-yue2-audio", "mix": "mix-audio",
        }
        for name in ("abc", "ustx"):
            self.record(name, self.melisma.build(
                name, self.midi, self.lyrics, self.root / f"initial.{name}",
                publish_stdout=True, **({"singer": "first-singer"} if name == "ustx" else {}),
            ), name, execute=True)
        self.record("vocal", OpenUtauAdapter(self.executable, self.root).build(
            Path(self.entries["ustx"].path), self.root / "vocal.wav",
        ), "vocal-audio")
        self.record("prepare", self.helper.prepare(
            Path(self.entries["abc"].path), self.root / "preparation",
            style="piano", run_id="instrumental", seed=42,
        ), "configuration")
        self.record("instrumental", self.helper.generate(
            Path(self.entries["prepare"].path).parent, self.root / "instrumental",
            models_root=self.root / "models", device="cpu",
        ), "instrumental-audio")
        for name in ("native", "scored"):
            self.record(name, self.native_candidate(name), "yue2-audio")
        self.record("mix", self.mix_candidate("initial-mix.wav"), "mixed-audio")
        # Every RED test reaches the missing API only after these actual adapter
        # productions, input capture, validation and persistence have succeeded.
        self.assertTrue(all(entry.provenance is not None for entry in self.entries.values()))
        self.assertTrue(all(run.success for run in self.catalog.runs.values()))

    def file(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def record(self, name, operation, output_type, *, execute=False, completion=()):
        run_id = name
        suffix = 1
        while run_id in self.catalog.runs:
            run_id = f"{name}-{suffix}"
            suffix += 1
        bound = self.catalog.begin(operation)
        try:
            if execute:
                result = self.melisma.execute(bound, ProcessRunner())
            else:
                # Use validated adapter/catalog records for model-free audio
                # and helper receipts; these are not real renderer integrations.
                outputs = (*bound.expected, *completion)
                for output in outputs:
                    output.path.parent.mkdir(parents=True, exist_ok=True)
                    if output.kind in ("abc", "ustx"):
                        contents = Path(self.entries[output.kind].path).read_text()
                    elif output.kind == "ir":
                        contents = '{"type":"header","version":1,"ticks_per_quarter":480}\n'
                    else:
                        contents = (
                            "{}" if output.kind == "json" or output.path.suffix == ".json"
                            else "audio fixture"
                        )
                    output.path.write_text(contents)
                result = OperationResult(
                    bound, ProcessResult(bound.command, ProcessState.EXITED, 0, pid=123),
                    tuple(inspect_artifact(item) for item in outputs),
                )
            self.assertTrue(result.success, result.errors)
            self.catalog.register_result(result, run_id)
        finally:
            self.catalog.end(bound)
        self.operations[name] = bound
        self.entries[name] = next(entry for entry in self.catalog.entries.values()
                                  if entry.producer_run == run_id and entry.type == output_type)
        return self.entries[name]

    def native_candidate(self, name="native", **updates):
        settings = dict(
            style="piano", lyrics="Synthetic native refrain", run_id="song", seed=42,
            model=self.root / "model", vae=self.root / "vae", device="cpu", cot="full",
            abc=Path(self.entries["abc"].path) if name == "scored" else None,
        )
        settings.update(updates)
        return self.native.build(self.root / f"candidate-{name}-{len(self.catalog.runs)}", **settings)

    def mix_candidate(self, name="candidate-mix.wav", **updates):
        settings = dict(
            instrumental_gain=1.0, vocal_gain=0.5, master_gain=0.7,
            duration="first", normalize=False,
        )
        settings.update(updates)
        return FFmpegAdapter(self.executable, self.root).build(
            Path(self.entries["instrumental"].path), Path(self.entries["vocal"].path),
            self.root / name, **settings,
        )

    def singer_candidate(self, **updates):
        settings = dict(singer="second-singer")
        settings.update(updates)
        return self.melisma.build(
            "ustx", self.midi, self.lyrics, self.root / "candidate.ustx",
            publish_stdout=True, **settings,
        )

    def model(self):
        return importlib.import_module("tools.web_ui.currentness")

    def selections(self, changes=None, *, omit=()):
        model = self.model()
        changes = changes or {}
        return tuple(
            model.ProductionSelection(
                self.workflows[name], entry.id,
                changes.get(name, self.operations[name]), entry.producer_run,
            ) for name, entry in self.entries.items() if name not in omit
        )

    def assess(self, changes=None, *, omit=()):
        return self.model().assess_currentness(self.catalog, self.selections(changes, omit=omit))

    def by_name(self, assessments):
        return {
            name: next(item for item in assessments
                       if item.artifact_id == entry.id and item.producer_run == entry.producer_run)
            for name, entry in self.entries.items()
        }

    def assert_states(self, assessments, *, stale=(), unknown=()):
        values = self.by_name(assessments)
        for name in self.entries:
            with self.subTest(artifact=name):
                expected = "stale" if name in stale else "unknown" if name in unknown else "current"
                self.assertEqual(values[name].currentness.value, expected)
        return values

    def history_snapshot(self):
        return (
            self.history.read_bytes(),
            {key: asdict(value) for key, value in self.catalog.entries.items()},
            {key: asdict(value) for key, value in self.catalog.runs.items()},
        )

    def complete_manifest_production(self, kind, extras=(), *, cot="full"):
        run_id = f"validated-{kind}-{len(self.catalog.runs)}"
        destination = self.root / run_id
        if kind == "native":
            adapter = self.native
            operation = adapter.build(
                destination, style="piano", lyrics="Synthetic completion fixture",
                run_id="song", seed=42, model=self.root / "model",
                vae=self.root / "vae", device="cpu", cot=cot,
            )
        elif kind == "prepare":
            adapter = self.helper
            operation = adapter.prepare(
                Path(self.entries["abc"].path), destination,
                style="piano", run_id="prepared", seed=42,
            )
        else:
            self.assertEqual(kind, "helper")
            adapter = self.helper
            operation = adapter.generate(
                Path(self.entries["prepare"].path).parent, destination,
                models_root=self.root / "models", device="cpu",
            )

        def write_files(root, names):
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.suffix.lower() == ".wav":
                    with wave.open(str(path), "wb") as audio:
                        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                        audio.writeframes(b"\0\0" * 80)
                else:
                    path.write_bytes(b"{}" if path.suffix == ".json" else b"manifest fixture")

        def manifest(root, names):
            return {
                name: {
                    "bytes": (root / name).stat().st_size,
                    "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
                } for name in names
            }

        def native_receipt(root, names):
            (root / "result.json").write_text(json.dumps({
                "status": "complete", "truncated": {"abc": False, "semantic": False},
                "artifacts": manifest(root, names),
            }))

        bound = self.catalog.begin(operation)
        try:
            root = Path(dict(bound.metadata)["result_root"])
            if kind == "native":
                names = (*NATIVE_FILES, *(("score.abc",) if cot != "off" else ()), *extras)
                write_files(root, names)
                native_receipt(root, names)
            elif kind == "prepare":
                root /= "prepared"
                names = (*PREPARED_FILES, *extras)
                write_files(root, names)
                (root / "prepared-manifest.json").write_text(json.dumps({
                    "files": manifest(root, names),
                }))
            else:
                names = (*HELPER_FILES, *extras)
                write_files(root, names)
                native_names = (*NATIVE_FILES, "score.abc")
                write_files(root / "native", native_names)
                native_receipt(root / "native", native_names)
                (root / "summary.json").write_text(json.dumps({
                    "status": "complete", "truncated": {"abc": False, "semantic": False},
                    "package_version": "0.1.5", "exact_prefix_verified": True,
                    "exact_abc_verified": True,
                }))
                (root / "delivery-manifest.json").write_text(json.dumps({
                    "files": manifest(root, names),
                }))
            result = OperationResult(
                bound, ProcessResult(bound.command, ProcessState.EXITED, 0, pid=124),
                tuple(inspect_artifact(output) for output in bound.expected),
            )
            self.assertTrue(result.success)
            runner = mock.Mock()
            runner.run.side_effect = AssertionError("completion must not execute a process")
            completed = adapter.complete(result, runner)
            runner.run.assert_not_called()
            self.assertTrue(completed.success, completed.errors)
            self.catalog.register_result(completed, run_id)
        finally:
            self.catalog.end(bound)
        self.catalog = ArtifactCatalog(self.history)
        self.assertTrue(self.catalog.runs[run_id].success)
        entries = tuple(
            entry for entry in self.catalog.entries.values() if entry.producer_run == run_id
        )
        self.assertTrue(all(entry.provenance is not None for entry in entries))
        return bound, entries

    def optional_manifest_selection(self, kind, name, semantic_type, *, cot="full"):
        operation, entries = self.complete_manifest_production(kind, (name,), cot=cot)
        entry, = (entry for entry in entries if Path(entry.path).name == name)
        self.assertEqual(entry.type, semantic_type)
        return self.model().ProductionSelection(
            "generate-yue2-audio", entry.id, operation, entry.producer_run,
        )

    def assert_optional_manifest_current(self, kind, name, semantic_type, *, cot="full"):
        selection = self.optional_manifest_selection(kind, name, semantic_type, cot=cot)
        before = self.history_snapshot()
        assessment, = self.model().assess_currentness(self.catalog, (selection,))
        self.assertEqual(assessment.currentness.value, "current")
        self.assertEqual(before, self.history_snapshot())

    def test_validated_reloaded_native_optional_notes_remain_assessable(self):
        self.assert_optional_manifest_current("native", "notes.txt", "text")

    def test_validated_reloaded_helper_optional_lyrics_remain_assessable(self):
        self.assert_optional_manifest_current("helper", "lyrics.txt", "lyrics")

    def test_validated_reloaded_preparation_optional_array_remains_assessable(self):
        self.assert_optional_manifest_current("prepare", "extra.npy", "file")

    def test_validated_reloaded_native_cot_off_optional_score_remains_assessable(self):
        self.assert_optional_manifest_current("native", "score.abc", "abc", cot="off")

    def test_validated_required_manifest_outputs_and_receipts_remain_assessable(self):
        for kind, cot in (("native", "full"), ("native", "off"),
                          ("prepare", "full"), ("helper", "full")):
            operation, entries = self.complete_manifest_production(kind, cot=cot)
            self.assertGreater(len(entries), len(operation.expected))
            before = self.history_snapshot()
            for entry in entries:
                with self.subTest(kind=kind, cot=cot, output=Path(entry.path).name):
                    selection = self.model().ProductionSelection(
                        "generate-yue2-audio", entry.id, operation, entry.producer_run,
                    )
                    assessment, = self.model().assess_currentness(self.catalog, (selection,))
                    self.assertEqual(assessment.currentness.value, "current")
            self.assertEqual(before, self.history_snapshot())

    def test_optional_json_is_accepted_by_each_actual_manifest_contract(self):
        for kind in ("native", "helper", "prepare"):
            with self.subTest(kind=kind):
                self.assert_optional_manifest_current(kind, "additional.json", "configuration")

    def test_manifest_extension_does_not_authorize_other_workflows_or_stages(self):
        selection = self.optional_manifest_selection("native", "notes.txt", "text")
        before = self.history_snapshot()
        for invalid, message in (
            (replace(selection, workflow="generate-abc"), "Selected workflow.*production/output"),
            (replace(selection, candidate=replace(selection.candidate, name="unimplemented")),
             "Candidate production/output.*workflow"),
            (replace(selection, candidate=replace(selection.candidate, tool="yue2-instrumental",
                                                  name="verify")),
             "Candidate production/output.*workflow"),
            (replace(selection, candidate=replace(selection.candidate, tool="melisma", name="ustx")),
             "Candidate production/output.*workflow"),
        ):
            with self.subTest(candidate=invalid.candidate.stage), self.assertRaisesRegex(ValueError, message):
                self.model().assess_currentness(self.catalog, (invalid,))
            self.assertEqual(before, self.history_snapshot())

    def test_manifest_contracts_preserve_producer_specific_audio_types(self):
        operation, entries = self.complete_manifest_production("native")
        entry, = (entry for entry in entries if entry.type == "yue2-audio")
        selection = self.model().ProductionSelection(
            "generate-yue2-audio", entry.id, operation, entry.producer_run,
        )
        before = self.history_snapshot()
        for candidate in (self.operations["instrumental"], self.operations["prepare"]):
            with self.subTest(candidate=candidate.stage):
                with self.assertRaisesRegex(ValueError, "Candidate production/output.*workflow"):
                    self.model().assess_currentness(
                        self.catalog, (replace(selection, candidate=candidate),),
                    )
                self.assertEqual(before, self.history_snapshot())

    def test_completed_wavs_have_operation_qualified_semantics_and_remain_inspectable(self):
        for kind, expected_type in (
            ("prepare", "audio"), ("helper", "instrumental-audio"), ("native", "yue2-audio"),
        ):
            with self.subTest(operation=kind):
                operation, entries = self.complete_manifest_production(kind, ("optional.wav",))
                entry, = (entry for entry in entries if Path(entry.path).name == "optional.wav")
                self.assertEqual(entry.type, expected_type)
                self.assertEqual(entry.provenance.output_type, expected_type)
                before = self.history_snapshot()
                contents = Path(entry.path).read_bytes()
                selection = self.model().ProductionSelection(
                    "generate-yue2-audio", entry.id, operation, entry.producer_run,
                )
                with mock.patch.object(Path, "open", side_effect=AssertionError("assessment I/O")), \
                     mock.patch.object(self.catalog, "_commit", side_effect=AssertionError("history write")):
                    assessment, = self.model().assess_currentness(self.catalog, (selection,))
                    choices = self.catalog.compatible(accepted_artifact_types("mix-audio", "instrumental"))
                self.assertEqual(assessment.currentness.value, "current")
                self.assertEqual(entry.id in {item["id"] for item in choices}, kind == "helper")
                self.assertIn(entry.id, {item["id"] for item in self.catalog.compatible(
                    accepted_artifact_types("play-audio", "audio"),
                )})
                self.assertEqual(self.catalog.get(entry.id), entry)
                self.assertEqual(before, self.history_snapshot())
                self.assertEqual(Path(entry.path).read_bytes(), contents)
                with wave.open(entry.path, "rb") as audio:
                    self.assertEqual(audio.getnframes(), 80)
                    self.assertEqual(audio.readframes(80), b"\0\0" * 80)

    def test_preparation_candidate_cannot_assess_generated_instrumental_wav(self):
        operation, entries = self.complete_manifest_production("helper", ("generated.wav",))
        entry, = (entry for entry in entries if Path(entry.path).name == "generated.wav")
        selection = self.model().ProductionSelection(
            "generate-yue2-audio", entry.id, operation, entry.producer_run,
        )
        before = self.history_snapshot()
        with self.assertRaisesRegex(
            ValueError, "^Candidate production/output does not belong to selected workflow$",
        ):
            self.model().assess_currentness(
                self.catalog, (replace(selection, candidate=self.operations["prepare"]),),
            )
        self.assertEqual(before, self.history_snapshot())

    def test_legacy_preparation_wav_is_not_an_instrumental_choice_without_history_rewrite(self):
        _, entries = self.complete_manifest_production("prepare", ("legacy.wav",))
        entry, = (entry for entry in entries if Path(entry.path).name == "legacy.wav")
        # Reproduce history written by the former tool-only classifier.
        legacy = asdict(replace(entry.provenance, output_type="instrumental-audio"))
        raw = json.loads(self.history.read_text())
        for artifact in raw["artifacts"]:
            if artifact["id"] == entry.id:
                artifact["type"] = "instrumental-audio"
                artifact["provenance"] = legacy
        for run in raw["runs"]:
            for production in run.get("productions", ()):
                if production["artifact_id"] == entry.id:
                    production["provenance"] = legacy
        self.history.write_text(json.dumps(raw))
        self.catalog = ArtifactCatalog(self.history)
        before = self.history_snapshot()
        choices = self.catalog.compatible(accepted_artifact_types("mix-audio", "instrumental"))
        self.assertNotIn(entry.id, {item["id"] for item in choices})
        self.assertEqual(self.catalog.get(entry.id).type, "instrumental-audio")
        self.assertTrue(self.catalog.get(entry.id).valid)
        self.assertTrue(self.catalog.runs[entry.producer_run].success)
        self.assertIn(entry.id, {item["id"] for item in self.catalog.document()["artifacts"]})
        with wave.open(entry.path, "rb") as audio:
            self.assertEqual(audio.getnframes(), 80)
        self.assertEqual(before, self.history_snapshot())

    def test_only_generated_instrumental_wav_propagates_to_mix(self):
        for kind, expected_mix in (("prepare", "current"), ("helper", "stale")):
            with self.subTest(operation=kind):
                operation, entries = self.complete_manifest_production(kind, ("source.wav",))
                entry, = (entry for entry in entries if Path(entry.path).name == "source.wav")
                name = f"wav-mix-{kind}"
                # A direct-path consumer's claimed role cannot grant final-output
                # semantics to preparation audio that is ineligible for selection.
                self.record(name, FFmpegAdapter(self.executable, self.root).build(
                    Path(entry.path), Path(self.entries["vocal"].path), self.root / f"{name}.wav",
                    instrumental_gain=1.0, vocal_gain=0.5, master_gain=0.7,
                    duration="first", normalize=False,
                ), "mixed-audio")
                candidate = replace(
                    operation, production=replace(operation.production, contract="changed-contract"),
                )
                selections = (
                    self.model().ProductionSelection(
                        "generate-yue2-audio", entry.id, candidate, entry.producer_run,
                    ),
                    self.model().ProductionSelection(
                        "mix-audio", self.entries[name].id, self.operations[name],
                        self.entries[name].producer_run,
                    ),
                )
                before = self.history_snapshot()
                assessments = {
                    item.artifact_id: item for item in self.model().assess_currentness(
                        self.catalog, selections,
                    )
                }
                self.assertEqual(assessments[entry.id].currentness.value, "stale")
                self.assertEqual(assessments[self.entries[name].id].currentness.value, expected_mix)
                self.assertEqual(before, self.history_snapshot())

    def test_optional_manifest_context_changes_remain_valid_comparison_differences(self):
        selection = self.optional_manifest_selection("prepare", "extra.npy", "file")
        operation = selection.candidate
        before = self.history_snapshot()
        for candidate in (
            replace(operation, production=replace(operation.production, contract="another-contract")),
            replace(operation, command=replace(operation.command, executable=Path("/different/tool"))),
            replace(operation, command=replace(operation.command, cwd=Path("/different/source"))),
            replace(operation, inputs=tuple(replace(item, revision="f" * 64) for item in operation.inputs)),
            replace(operation, production=replace(operation.production, settings=tuple(
                (key, "strings" if key == "style" else value)
                for key, value in operation.production.settings
            ))),
        ):
            assessment, = self.model().assess_currentness(
                self.catalog, (replace(selection, candidate=candidate),),
            )
            self.assertEqual(assessment.currentness.value, "stale")
            self.assertEqual(before, self.history_snapshot())

    def test_optional_manifest_missing_candidate_evidence_remains_unknown(self):
        selection = self.optional_manifest_selection("native", "notes.txt", "text")
        before = self.history_snapshot()
        for candidate in (None, replace(selection.candidate, production=None)):
            assessment, = self.model().assess_currentness(
                self.catalog, (replace(selection, candidate=candidate),),
            )
            self.assertEqual(assessment.currentness.value, "unknown")
            self.assertEqual(before, self.history_snapshot())

    def test_matching_contexts_are_current_without_guessing_from_stage_names(self):
        self.assert_states(self.assess())
        model = self.model()
        self.assertEqual({item.value for item in model.Currentness}, {"current", "stale", "unknown"})

    def test_candidate_must_declare_the_selected_semantic_output(self):
        candidate = replace(self.operations["ustx"], expected=self.operations["abc"].expected)
        before = self.history_snapshot()
        with self.assertRaisesRegex(ValueError, "Candidate.*output"):
            self.assess({"ustx": candidate})
        self.assertEqual(before, self.history_snapshot())

    def test_historical_production_must_belong_to_selected_workflow(self):
        model = self.model()
        for name, workflow in (
            ("abc", "generate-ustx"), ("vocal", "generate-ustx"),
            ("abc", "full-pipeline"),
        ):
            for candidate in (self.operations[name], None):
                selection = model.ProductionSelection(
                    workflow, self.entries[name].id, candidate, self.entries[name].producer_run,
                )
                with self.subTest(artifact=name, workflow=workflow, candidate=candidate is not None):
                    with self.assertRaisesRegex(ValueError, "workflow.*production"):
                        model.assess_currentness(self.catalog, (selection,))

    def test_relabeling_stale_ustx_cannot_suppress_vocal_and_mix_propagation(self):
        changes = {"ustx": self.catalog.bind_inputs(self.singer_candidate())}
        self.assert_states(self.assess(changes), stale=("ustx", "vocal", "mix"))
        selections = tuple(
            replace(item, workflow="generate-abc")
            if item.artifact_id == self.entries["ustx"].id else item
            for item in self.selections(changes)
        )
        before = self.history_snapshot()
        with self.assertRaisesRegex(ValueError, "workflow.*production"):
            self.model().assess_currentness(self.catalog, selections)
        self.assertEqual(before, self.history_snapshot())

    def test_candidate_operation_must_belong_to_selected_workflow(self):
        candidate = replace(self.operations["ustx"], name="abc")
        with self.assertRaisesRegex(ValueError, "Candidate.*workflow"):
            self.assess({"ustx": candidate})

    def test_historical_output_must_be_compatible_with_its_producer(self):
        entry = self.entries["ustx"]
        evidence = replace(entry.provenance, output_type="abc")
        self.catalog.entries[entry.id] = replace(entry, type="abc", provenance=evidence)
        self.catalog.runs["ustx"] = replace(
            self.catalog.runs["ustx"], productions=(ArtifactProduction(entry.id, evidence),),
        )
        with self.assertRaisesRegex(ValueError, "workflow.*production"):
            self.assess({"ustx": None})

    def test_bundle_outputs_remain_qualified_by_semantic_workflow(self):
        operation = self.melisma.build(
            "render-inputs", self.midi, self.lyrics, self.root / "bundle",
            singer="first-singer",
        )
        self.record("bundle", operation, "abc")
        model = self.model()
        entries = [entry for entry in self.catalog.entries.values() if entry.producer_run == "bundle"]
        self.assertEqual({entry.type for entry in entries}, {"abc", "ustx", "ir"})
        for entry in entries:
            workflow = "generate-ustx" if entry.type == "ustx" else "generate-abc"
            selection = model.ProductionSelection(workflow, entry.id, self.operations["bundle"], "bundle")
            assessment, = model.assess_currentness(self.catalog, (selection,))
            self.assertEqual(assessment.currentness.value, "current")
            if entry.type in ("abc", "ustx"):
                wrong_workflow = "generate-abc" if entry.type == "ustx" else "generate-ustx"
                with self.assertRaisesRegex(ValueError, "workflow.*production"):
                    model.assess_currentness(self.catalog, (replace(selection, workflow=wrong_workflow),))

    def test_declared_completion_and_receipt_outputs_remain_valid(self):
        model = self.model()
        for name, primary_type, files in (
            ("native", "yue2-audio", (*NATIVE_FILES, "score.abc")),
            ("prepare", "configuration", PREPARED_FILES),
            ("instrumental", "instrumental-audio", HELPER_FILES),
        ):
            operation = self.operations[name]
            root = Path(dict(operation.metadata)["result_root"])
            if name == "prepare":
                root /= "prepared"
            run_id = f"{name}-completion"
            self.record(run_id, operation, primary_type, completion=tuple(
                ExpectedArtifact(root / filename, allow_empty=filename == "lyrics.txt")
                for filename in files
            ))
            entries = [
                entry for entry in self.catalog.entries.values() if entry.producer_run == run_id
            ]
            self.assertGreater(len(entries), len(operation.expected))
            for entry in entries:
                selection = model.ProductionSelection(
                    "generate-yue2-audio", entry.id, self.operations[run_id], run_id,
                )
                with self.subTest(operation=name, output=Path(entry.path).name):
                    assessment, = model.assess_currentness(self.catalog, (selection,))
                    self.assertEqual(assessment.currentness.value, "current")

    def test_changed_resources_and_contracts_remain_valid_context_differences(self):
        operation = self.operations["ustx"]
        for candidate in (
            replace(operation, production=replace(operation.production, contract="another-contract")),
            replace(operation, command=replace(operation.command, executable=Path("/different/tool"))),
            replace(operation, command=replace(operation.command, cwd=Path("/different/source"))),
        ):
            self.assert_states(self.assess({"ustx": candidate}), stale=("ustx", "vocal", "mix"))

    def test_singer_phonemizer_renderer_stale_only_ustx_vocal_and_mix(self):
        for setting, value in (
            ("singer", "second-singer"), ("phonemizer", "Another.Phonemizer"),
            ("renderer", "DIFFSINGER"),
        ):
            candidate = self.catalog.bind_inputs(self.singer_candidate(
                **({"singer": "first-singer", setting: value}),
            ))
            with self.subTest(setting=setting):
                values = self.assert_states(self.assess({"ustx": candidate}), stale=("ustx", "vocal", "mix"))
                self.assertTrue(any(reason.field == f"settings.{setting}" for reason in values["ustx"].reasons))
                reason, = (reason for reason in values["vocal"].reasons if reason.code == "upstream_stale")
                self.assertEqual((reason.upstream_artifact, reason.input_role, reason.artifact_type),
                                 (self.entries["ustx"].id, "ustx", "ustx"))
                self.assertTrue(any(reason.upstream_artifact == self.entries["vocal"].id
                                    for reason in values["mix"].reasons))

    def test_native_and_helper_contracts_remain_independent(self):
        for name in ("native", "scored"):
            candidate = self.catalog.bind_inputs(self.native_candidate(name, seed=43))
            with self.subTest(contract=name):
                self.assert_states(self.assess({name: candidate}), stale=(name,))
        candidate = self.catalog.bind_inputs(self.helper.generate(
            Path(self.entries["prepare"].path).parent, self.root / "changed-helper",
            models_root=self.root / "models", device="cuda",
        ))
        self.assert_states(self.assess({"instrumental": candidate}), stale=("instrumental", "mix"))

    def test_helper_prepare_context_propagates_through_manifest_within_workflow(self):
        candidate = self.catalog.bind_inputs(self.helper.prepare(
            Path(self.entries["abc"].path), self.root / "changed-prepare",
            style="strings", run_id="other", seed=42,
        ))
        values = self.assert_states(self.assess({"prepare": candidate}), stale=("prepare", "instrumental", "mix"))
        reason, = (reason for reason in values["instrumental"].reasons if reason.code == "upstream_stale")
        self.assertEqual((reason.upstream_artifact, reason.input_role, reason.artifact_type),
                         (self.entries["prepare"].id, "prepared-manifest", "configuration"))

    def test_abc_staleness_skips_ustx_and_unscored_native_but_reaches_helper_mix(self):
        candidate = self.catalog.bind_inputs(self.melisma.build(
            "abc", self.file("abc-only.mid", "different selected MIDI"),
            self.lyrics, self.root / "candidate.abc", publish_stdout=True,
        ))
        self.assert_states(self.assess({"abc": candidate}),
                           stale=("abc", "prepare", "instrumental", "scored", "mix"))

    def test_every_mix_setting_stales_only_mix(self):
        for setting, value in (
            ("instrumental_gain", 0.8), ("vocal_gain", 0.8), ("master_gain", 0.8),
            ("duration", "longest"), ("normalize", True),
        ):
            with self.subTest(setting=setting):
                candidate = self.catalog.bind_inputs(self.mix_candidate(**{setting: value}))
                self.assert_states(self.assess({"mix": candidate}), stale=("mix",))

    def test_midi_and_file_lyrics_revisions_affect_only_selected_consumers(self):
        for path in (self.midi, self.lyrics):
            original = path.read_bytes()
            path.write_text("changed selected input", encoding="utf-8")
            changes = {
                name: self.catalog.bind_inputs(self.operations[name]) for name in ("abc", "ustx")
            }
            with self.subTest(input=path.name):
                self.assert_states(self.assess(changes), stale=(
                    "abc", "ustx", "vocal", "prepare", "instrumental", "scored", "mix",
                ))
            path.write_bytes(original)

    def test_edited_abc_stales_score_consumers_not_unscored_native(self):
        path = Path(self.entries["abc"].path)
        path.write_text(path.read_text() + "\n% selected score edit\n")
        self.catalog.refresh()
        changes = {
            name: self.catalog.bind_inputs(self.operations[name]) for name in ("prepare", "scored")
        }
        values = self.assert_states(self.assess(changes),
                                    stale=("prepare", "instrumental", "scored", "mix"))
        self.assertEqual(values["abc"].integrity.value, "modified")
        self.assertEqual(values["abc"].currentness.value, "current")

    def test_insufficient_candidate_capture_is_explicitly_unknown(self):
        for name, candidate, unknown in (
            ("native", replace(self.operations["native"], production=None), ("native",)),
            ("vocal", replace(self.operations["vocal"], inputs=tuple(
                replace(item, revision=None) for item in self.operations["vocal"].inputs
            )), ("vocal", "mix")),
        ):
            with self.subTest(context=name):
                self.assert_states(self.assess({name: candidate}), unknown=unknown)

    def test_entry_evidence_remains_usable_without_a_retained_run_record(self):
        raw = json.loads(self.history.read_text())
        raw["runs"] = [run for run in raw["runs"] if run["id"] != "ustx"]
        self.history.write_text(json.dumps(raw))
        self.catalog = ArtifactCatalog(self.history)
        self.assert_states(self.assess())
        for entry in raw["artifacts"]:
            if entry["id"] == self.entries["ustx"].id:
                entry.pop("provenance")
        self.history.write_text(json.dumps(raw))
        self.catalog = ArtifactCatalog(self.history)
        before = self.history_snapshot()
        self.assert_states(self.assess(), unknown=("ustx", "vocal", "mix"))
        self.assertEqual(before, self.history_snapshot())

    def test_selected_imported_inputs_start_at_the_actual_consuming_context(self):
        imported = self.file("imported.ustx", "manual USTX")
        self.catalog.register_input(imported, "ustx")
        self.record("vocal", OpenUtauAdapter(self.executable, self.root).build(
            imported, self.root / "imported-vocal.wav",
        ), "vocal-audio")
        self.record("mix", self.mix_candidate("imported-mix.wav"), "mixed-audio")
        imported.write_text("edited imported USTX")
        candidate = self.catalog.bind_inputs(self.operations["vocal"])
        self.assert_states(self.assess({"vocal": candidate}), stale=("vocal", "mix"))
        source = next(item for item in self.catalog.entries.values() if item.path == str(imported))
        self.assertIsNone(source.stage)
        self.assertIsNone(source.provenance)

    def test_selected_manifest_and_audio_content_revisions_affect_only_consumers(self):
        for name, consumer, downstream in (
            ("prepare", "instrumental", ("instrumental", "mix")),
            ("vocal", "mix", ("mix",)), ("instrumental", "mix", ("mix",)),
        ):
            path = Path(self.entries[name].path)
            original = path.read_bytes()
            path.write_text('{"changed":"content"}' if name == "prepare" else "changed audio")
            candidate = self.catalog.bind_inputs(self.operations[consumer])
            with self.subTest(input=name):
                self.assert_states(self.assess({consumer: candidate}), stale=downstream)
            path.write_bytes(original)

    def test_unselected_artifact_from_same_stage_does_not_contaminate_consumers(self):
        self.workflows["other-ustx"] = "generate-ustx"
        self.record("other-ustx", self.melisma.build(
            "ustx", self.midi, self.lyrics, self.root / "other.ustx",
            singer="first-singer", publish_stdout=True,
        ), "ustx", execute=True)
        candidate = self.catalog.bind_inputs(self.singer_candidate())
        self.assert_states(self.assess({"other-ustx": candidate}), stale=("other-ustx",))

    def test_wrong_role_or_semantic_type_cannot_transmit_staleness(self):
        for field, value in (("role", "not-ustx"), ("type", "abc")):
            # An unsupported selected render contract is still representable;
            # it must not silently acquire the actual ustx edge by artifact ID.
            operation = self.operations["vocal"]
            changed = replace(operation, inputs=(replace(operation.inputs[0], **{field: value}),))
            self.record("vocal", replace(
                changed, expected=(replace(changed.expected[0], path=self.root / f"wrong-{field}.wav"),),
            ), "vocal-audio")
            self.record("mix", self.mix_candidate(f"wrong-{field}-mix.wav"), "mixed-audio")
            with self.subTest(field=field):
                candidate = self.catalog.bind_inputs(self.singer_candidate())
                self.assert_states(self.assess({"ustx": candidate}), stale=("ustx",))
            self.operations["vocal"] = operation

    def test_unknown_evidence_and_absent_candidates_are_not_stale_or_current(self):
        raw = json.loads(self.history.read_text())
        for entry in raw["artifacts"]:
            if entry["id"] == self.entries["ustx"].id:
                entry.pop("provenance")
        for run in raw["runs"]:
            if run["id"] == "ustx":
                run.pop("productions")
        self.history.write_text(json.dumps(raw))
        self.catalog = ArtifactCatalog(self.history)
        before = self.history_snapshot()
        self.assert_states(self.assess(), unknown=("ustx", "vocal", "mix"))
        self.assertEqual(before, self.history_snapshot())
        self.assert_states(self.assess({"native": None}), unknown=("ustx", "vocal", "mix", "native"))

    def test_unknown_consumer_does_not_gain_definite_stale_history(self):
        entry = self.entries["vocal"]
        self.catalog.entries[entry.id] = replace(entry, provenance=None)
        self.catalog.runs["vocal"] = replace(self.catalog.runs["vocal"], productions=())
        self.catalog._commit(self.catalog.entries, self.catalog.runs)
        candidate = self.catalog.bind_inputs(self.singer_candidate())
        self.assert_states(self.assess({"ustx": candidate}), stale=("ustx",), unknown=("vocal", "mix"))

    def test_no_candidate_cannot_be_overridden_by_upstream_staleness(self):
        candidate = self.catalog.bind_inputs(self.singer_candidate())
        self.assert_states(self.assess({"ustx": candidate, "vocal": None}),
                           stale=("ustx",), unknown=("vocal", "mix"))

    def test_missing_and_edited_outputs_remain_separate_from_context_mismatch(self):
        candidate = self.catalog.bind_inputs(self.singer_candidate())
        path = Path(self.entries["ustx"].path)
        original = self.entries["ustx"].provenance
        path.write_text("externally edited USTX")
        self.catalog.refresh()
        before = self.history_snapshot()
        values = self.assert_states(self.assess())
        self.assertEqual(values["ustx"].integrity.value, "modified")
        self.assertEqual(values["ustx"].currentness.value, "current")
        self.assertEqual(self.catalog.get(self.entries["ustx"].id).provenance, original)
        self.assertEqual(before, self.history_snapshot())
        path.unlink()
        self.catalog.refresh()
        values = self.assert_states(self.assess({"ustx": candidate}), unknown=("ustx", "vocal", "mix"))
        self.assertEqual(values["ustx"].availability.value, "missing")
        self.assertEqual(self.catalog.get(self.entries["ustx"].id).provenance.output_revision, original.output_revision)

    def test_edited_output_does_not_replace_its_production_context(self):
        path = Path(self.entries["ustx"].path)
        path.write_text("manual edit distinct from generation settings")
        self.catalog.refresh()
        candidate = self.catalog.bind_inputs(self.operations["vocal"])
        values = self.assert_states(self.assess({"vocal": candidate}), stale=("vocal", "mix"))
        self.assertEqual(values["ustx"].integrity.value, "modified")
        self.assertEqual(self.catalog.changed_inputs("vocal"), [self.entries["ustx"].id])

    def test_assessment_neither_refreshes_nor_rewrites_or_deletes_history(self):
        candidate = self.catalog.bind_inputs(self.singer_candidate())
        before = self.history_snapshot()
        with mock.patch.object(self.catalog, "refresh", side_effect=AssertionError("implicit refresh")), \
             mock.patch.object(self.catalog, "bind_inputs", side_effect=AssertionError("implicit bind")), \
             mock.patch.object(self.catalog, "_commit", side_effect=AssertionError("history write")), \
             mock.patch.object(Path, "open", side_effect=AssertionError("filesystem read")):
            self.assert_states(self.assess({"ustx": candidate}), stale=("ustx", "vocal", "mix"))
        self.assertEqual(before, self.history_snapshot())
        for entry in self.entries.values():
            self.assertTrue(Path(entry.path).is_file())
            self.assertEqual(self.catalog.get(entry.id).production_status, "successful")
        self.assertTrue(self.catalog.read_text(self.entries["ustx"].id)["text"])

    def test_reasons_are_deterministic_and_never_expose_setting_values(self):
        lyric = "Synthetic full lyric content not allowed in stale reasons"
        secret = "synthetic-unsafe-metadata-not-a-real-credential"
        candidate = self.catalog.bind_inputs(self.native_candidate(
            lyrics=lyric, style="a distinctive private style selection", seed=43,
        ))
        candidate = replace(candidate, metadata=(*candidate.metadata, ("api_token", secret)))
        selections = self.selections({"native": candidate})
        model = self.model()
        first = model.assess_currentness(self.catalog, selections)
        second = model.assess_currentness(self.catalog, tuple(reversed(selections)))
        self.assertEqual(first, second)
        serialized = json.dumps([asdict(value) for value in first])
        for value in (lyric, secret, "a distinctive private style selection", "--lyrics", "canonical"):
            self.assertNotIn(value, serialized)
        reason_fields = {reason.field for reason in self.by_name(first)["native"].reasons}
        self.assertTrue({"settings.lyrics_revision", "settings.style", "settings.seed"} <= reason_fields)

    def test_canonical_scalar_type_changes_have_named_setting_reasons(self):
        # Canonical JSON distinguishes these scalar types even when Python's
        # value equality does not. Explanations must follow the signature.
        for key, value in (("offline", 1), ("seed", 42.0)):
            operation = self.operations["native"]
            candidate = replace(operation, production=replace(
                operation.production, settings=tuple(
                    (name, value if name == key else old)
                    for name, old in operation.production.settings
                ),
            ))
            assessment = self.by_name(self.assess({"native": candidate}))["native"]
            with self.subTest(setting=key):
                self.assertEqual(assessment.currentness.value, "stale")
                self.assertTrue(any(reason.field == f"settings.{key}" for reason in assessment.reasons))

    def test_idle_stage_projection_is_stale_without_overwriting_attempt_states(self):
        model = self.model()
        assessments = self.assess({"ustx": self.catalog.bind_inputs(self.singer_candidate())})
        for state in StageState:
            for run_id in (None, "retained-attempt"):
                document = stage_document("melisma.ustx", state, catalog=self.catalog, run_id=run_id)
                original = json.loads(json.dumps(document))
                projected = model.assessed_stage_document(document, assessments)
                expected = "stale" if state == StageState.READY and run_id is None else state.value
                with self.subTest(state=state, run_id=run_id):
                    self.assertEqual(projected["state"], expected)
                    self.assertEqual(projected["run_id"], run_id)
                    self.assertEqual(projected["currentness"][0]["currentness"], "stale")
                    self.assertEqual(json.loads(json.dumps(document)), original)
        unknown = self.assess({"ustx": None})
        document = stage_document("melisma.ustx", StageState.READY, catalog=self.catalog)
        self.assertEqual(model.assessed_stage_document(document, unknown)["state"], "ready")

    def test_registry_failed_attempt_and_selected_stale_artifact_remain_separate(self):
        registry = RunRegistry(catalog=self.catalog)
        try:
            # An occupied output is rejected, preserving the earlier success.
            operation = self.operations["ustx"]
            run = registry.get(registry.start(self.melisma, operation)["id"])
            eventually(lambda: run.is_complete)
            document = registry.stage_snapshot("melisma.ustx", idle_state=StageState.READY)
            self.assert_states(self.assess())
            assessments = self.assess({"ustx": self.catalog.bind_inputs(self.singer_candidate())})
            projected = self.model().assessed_stage_document(document, assessments)
            self.assertEqual(projected["state"], "failed")
            self.assertEqual(projected["currentness"][0]["currentness"], "stale")
            self.assertTrue(self.catalog.runs["ustx"].success)
            self.assertFalse(self.catalog.runs[run.id].success)
        finally:
            registry.close()

    def test_historical_run_selection_does_not_guess_latest_same_path_production(self):
        original_entry = self.entries["ustx"]
        Path(original_entry.path).unlink()
        self.record("replacement", self.operations["ustx"], "ustx", execute=True)
        self.workflows["replacement"] = "generate-ustx"
        self.operations["replacement"] = self.catalog.bind_inputs(self.singer_candidate())
        selections = self.selections()
        model = self.model()
        original, = model.assess_currentness(self.catalog, tuple(
            item for item in selections if item.artifact_id == original_entry.id
            and item.producer_run == "ustx"
        ))
        replacement, = model.assess_currentness(self.catalog, tuple(
            item for item in selections if item.artifact_id == original_entry.id
            and item.producer_run == "replacement"
        ))
        self.assertEqual(original.currentness.value, "current")
        self.assertEqual(replacement.currentness.value, "stale")
        # Both receipts have the same output bytes. Ambiguous selected producers
        # for one input must be rejected rather than guessing which run it used.
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.model().assess_currentness(self.catalog, selections)

    def test_different_revision_of_same_artifact_identity_is_not_the_consumed_source(self):
        original = self.entries["ustx"]
        Path(original.path).unlink()
        self.executable.write_text(self.executable.read_text().replace("resolution: 480", "resolution: 960"))
        replacement = self.record("replacement", self.operations["ustx"], "ustx", execute=True)
        self.workflows["replacement"] = "generate-ustx"
        self.assertEqual(original.id, replacement.id)
        self.assertNotEqual(original.revision, replacement.revision)
        self.operations["replacement"] = self.catalog.bind_inputs(self.singer_candidate())
        values = self.model().assess_currentness(self.catalog, self.selections(omit=("ustx",)))
        by_run = {item.producer_run: item for item in values}
        self.assertEqual(by_run["replacement"].currentness.value, "stale")
        self.assertEqual(by_run["vocal"].currentness.value, "current")
        self.assertEqual(by_run["mix"].currentness.value, "current")

    def test_invalid_and_duplicate_selections_fail_explicitly(self):
        model = self.model()
        selection = self.selections()[0]
        for bad in (
            replace(selection, workflow="not-a-workflow"),
            replace(selection, artifact_id="f" * 32),
            replace(selection, producer_run="missing-run"),
        ):
            with self.subTest(selection=bad), self.assertRaises((ValueError, KeyError)):
                model.assess_currentness(self.catalog, (bad,))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            model.assess_currentness(self.catalog, (selection, selection))

    def test_playback_has_no_produced_artifact_to_classify(self):
        play = FFplayAdapter(self.executable, self.root).build(Path(self.entries["vocal"].path))
        self.assertEqual(play.expected, ())
        model = self.model()
        with self.assertRaisesRegex(ValueError, "generation"):
            model.assess_currentness(self.catalog, (
                model.ProductionSelection("play-audio", self.entries["vocal"].id, play),
            ))

    def test_cycles_and_duplicate_paths_terminate_with_immediate_reasons_once(self):
        # Defensive fixture: actual catalog types with deliberately cyclic
        # selected receipts; real executions cannot consume their own future.
        self.workflows["other-ustx"] = "generate-ustx"
        self.record("other-ustx", self.melisma.build(
            "ustx", self.midi, self.lyrics, self.root / "cycle.ustx",
            singer="first-singer", publish_stdout=True,
        ), "ustx", execute=True)
        for name, other in (("ustx", "other-ustx"), ("other-ustx", "ustx")):
            entry, source = self.entries[name], self.entries[other]
            operation = self.operations[name]
            self.operations[name] = replace(operation, inputs=(*operation.inputs, OperationInput(
                "previous", Path(source.path), "ustx", source.id, source.revision,
            )))
            evidence = replace(entry.provenance, inputs=(*entry.provenance.inputs, ProductionInput(
                "previous", "ustx", source.id, source.revision,
            )))
            self.catalog.entries[entry.id] = replace(entry, provenance=evidence)
            run = self.catalog.runs[name]
            self.catalog.runs[name] = replace(run, productions=(ArtifactProduction(entry.id, evidence),))
        candidate = replace(self.operations["ustx"], production=self.singer_candidate().production)
        old_handler = signal.signal(signal.SIGALRM, lambda *_: self.fail("propagation did not terminate"))
        signal.alarm(5)
        try:
            values = self.assert_states(self.assess({"ustx": candidate}),
                                       stale=("ustx", "other-ustx", "vocal", "mix"))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        for value in values.values():
            keys = [json.dumps(asdict(reason), sort_keys=True) for reason in value.reasons]
            self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
