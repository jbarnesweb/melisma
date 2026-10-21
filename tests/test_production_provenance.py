"""Production-context evidence, not stale assignment or artifact reuse policy."""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration import artifact_catalog as catalog_module
from tools.orchestration.adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from tools.orchestration.artifact_catalog import ArtifactCatalog, CatalogError, fingerprint
from tools.orchestration.artifacts import inspect_artifact
from tools.orchestration.configuration import Configuration
from tools.orchestration.discovery import DiscoverySource
from tools.orchestration.execution import (
    RunRegistry, StageState, command_document, operation_document, process_document, stage_document,
)
from tools.orchestration.playback import FFplayAdapter
from tools.orchestration.process import (
    OperationResult, ProcessResult, ProcessRunner, ProcessState,
)
from tools.orchestration.provenance import ProductionConfiguration, provenance_from_document
from tools.orchestration.yue2 import InstrumentalAdapter, NativeYuE2Adapter
from tools.web_ui.execution import ExecutionAPI
from test_execution import eventually


class ProductionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "state" / "artifacts.json"
        self.catalog = ArtifactCatalog(self.history)
        self.executable = self.root / "melisma-fixture"
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import sys\n"
            "if sys.argv[2] == 'abc':\n"
            " print('X:1\\nM:4/4\\nL:1/32\\nK:C\\nV: Vocal\\nV: Ins')\n"
            "else:\n"
            " print('ustx_version: 0.7\\nresolution: 480\\ntracks:\\nvoice_parts:')\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        self.midi = self.file("input.mid", "MIDI fixture")
        self.lyrics = self.file("lyrics.txt", "lyric fixture")
        self.abc = self.file("score.abc", "ABC fixture")
        self.ustx = self.file("input.ustx", "USTX fixture")
        self.vocal = self.file("vocal.wav", "vocal fixture")
        self.instrumental = self.file("instrumental.flac", "instrumental fixture")
        self.manifest = self.file("prepared/prepared-manifest.json", '{"files":{}}')
        self.melisma = MelismaAdapter(self.executable, self.root)
        self.native_adapter = NativeYuE2Adapter(Path(sys.executable), self.root)
        self.helper = InstrumentalAdapter(Path(sys.executable), self.root)
        self.first_operation = self.melisma_operation("first")
        self.first_result = self.produce(self.first_operation, "first-run")
        self.first = next(
            entry for entry in self.catalog.entries.values() if entry.producer_run == "first-run"
        )
        # Every RED test reaches its assertions only after actual fixture
        # execution, validation, publication and persisted catalog registration.
        self.assertTrue(self.first_result.success)
        self.assertTrue(self.first.exists)
        self.assertEqual(self.first.production_status, "successful")

    def file(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def melisma_operation(self, name, mode="abc", **settings):
        return self.melisma.build(
            mode, self.midi, self.lyrics, self.root / name,
            publish_stdout=mode != "render-inputs", **settings,
        )

    def native(self, name="native", **updates):
        settings = dict(
            style="piano", lyrics="words", run_id="song", seed=42,
            model=self.root / "model", vae=self.root / "vae", device="cpu",
            cot="full", abc=self.abc,
        )
        settings.update(updates)
        return self.native_adapter.build(self.root / name, **settings)

    def mix(self, name="mixed.wav", **updates):
        settings = dict(
            instrumental_gain=1.0, vocal_gain=0.5, master_gain=0.7,
            duration="first", normalize=False,
        )
        settings.update(updates)
        return FFmpegAdapter(self.executable, self.root).build(
            self.instrumental, self.vocal, self.root / name, **settings,
        )

    def candidate(self, operation, output_type):
        value = catalog_module.production_provenance(
            self.catalog.bind_inputs(operation), output_type,
        )
        self.assertIsNotNone(value)
        return value

    def produce(self, operation, run_id):
        bound = self.catalog.begin(self.catalog.bind_inputs(operation))
        try:
            result = self.melisma.execute(bound, ProcessRunner())
            self.assertTrue(result.success, result.errors)
            self.catalog.register_result(result, run_id)
            return result
        finally:
            self.catalog.end(bound)

    def record_native(self, operation, run_id):
        bound = self.catalog.begin(self.catalog.bind_inputs(operation))
        try:
            # Exercise persistence at the validated adapter/catalog boundary,
            # without requiring model execution or audio probing.
            for output in bound.expected:
                output.path.parent.mkdir(parents=True, exist_ok=True)
                output.path.write_text("{}" if output.kind == "json" else "audio fixture")
            result = OperationResult(
                bound, ProcessResult(bound.command, ProcessState.EXITED, 0, pid=123),
                tuple(inspect_artifact(item) for item in bound.expected),
            )
            self.assertTrue(result.success)
            self.catalog.register_result(result, run_id)
        finally:
            self.catalog.end(bound)
        return next(entry for entry in self.catalog.entries.values()
                    if entry.producer_run == run_id and entry.type == "yue2-audio")

    def native_cli_fixture(self):
        self.file("src/yue2/__init__.py", "")
        self.file("src/yue2/cli.py", (
            "import json,pathlib,sys,time\n"
            "root=pathlib.Path(__file__).resolve().parents[2]\n"
            "(root/'received.json').write_text(json.dumps(sys.argv[1:]))\n"
            "if '--validation' in sys.argv:\n"
            " print('validation-ready',flush=True)\n"
            " while not (root/'release-validation').exists(): time.sleep(.005)\n"
            " sys.exit(9)\n"
            "if (root/'wait-primary').exists():\n"
            " print('primary-ready',flush=True)\n"
            " while True: time.sleep(.005)\n"
            "sys.exit(7)\n"
        ))

    def assert_safe_lyric_command(self, document, lyrics):
        self.assertNotIn(lyrics, json.dumps(document))
        self.assertIn("[REDACTED:inline-content]", document["display"])
        self.assertTrue(any(
            argument in ("--lyrics=[REDACTED:inline-content]", "[REDACTED:inline-content]")
            for argument in document["arguments"]
        ))

    def test_diagnostic_commands_redact_lyrics_without_mutating_trusted_command(self):
        lyrics = "Synthetic private refrain for structured diagnostic tests"
        operation = self.native(lyrics=lyrics)
        for split in (False, True):
            arguments = tuple(
                part for argument in operation.command.arguments
                for part in (("--lyrics", lyrics) if split and argument.startswith("--lyrics=")
                             else (argument,))
            )
            trusted = replace(operation.command, arguments=arguments)
            projected = replace(operation, command=trusted)
            original = asdict(trusted)
            documents = (
                command_document(trusted), operation_document(projected)["command"],
                process_document(ProcessResult(trusted, ProcessState.REJECTED, None))["command"],
            )
            for index, document in enumerate(documents):
                with self.subTest(split=split, projection=index):
                    self.assert_safe_lyric_command(document, lyrics)
                    self.assertNotIn("environment", document)
            self.assertEqual(asdict(trusted), original)
            self.assertIn(lyrics if split else f"--lyrics={lyrics}", trusted.arguments)
        production = operation_document(operation)["production"]
        self.assertNotIn(lyrics, json.dumps(production))
        self.assertEqual(
            dict(production["settings"])["lyrics_revision"],
            hashlib.sha256(lyrics.encode("utf-8")).hexdigest(),
        )

    def test_diagnostic_metadata_allowlist_preserves_only_known_safe_fields(self):
        secret = "synthetic-diagnostic-metadata-value"
        operations = (
            self.native(), self.melisma_operation("stdout-metadata"),
            self.mix(), FFplayAdapter(self.executable, self.root).build(self.vocal),
        )
        expected_keys = (
            {"contract_version", "cot", "result_root", "stage"},
            {"stdout_artifact"}, {"duration", "normalize"}, {"input", "stop_policy"},
        )
        for operation, keys in zip(operations, expected_keys):
            metadata = (*operation.metadata, ("unknown_arbitrary", secret), *(
                (key, secret) for key in (
                    "password", "private_key", "api_key", "bearer", "credential", "api_token", "secret",
                )
            ))
            changed = replace(operation, metadata=metadata)
            document = operation_document(changed)
            with self.subTest(tool=operation.tool):
                self.assertEqual(set(document["metadata"]), keys)
                self.assertEqual(document["metadata"], dict(operation.metadata))
                self.assertNotIn(secret, json.dumps(document))
                self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), json.dumps(document))
                self.assertEqual(changed.metadata, metadata)

    def test_allowlisted_diagnostic_metadata_still_rejects_credential_values(self):
        for key in (
            "contract_version", "cot", "result_root", "stage", "stdout_artifact",
            "duration", "normalize", "input", "stop_policy",
        ):
            with self.subTest(key=key):
                operation = replace(self.native(), metadata=((key, "Bearer synthetic-diagnostic-value"),))
                self.assertEqual(operation_document(operation)["metadata"], {})

    def test_automatic_native_diagnostics_are_safe_across_failure_paths(self):
        self.native_cli_fixture()
        lyrics = "Synthetic inline refrain that must not reach saved diagnostics"
        secret = "synthetic-private-metadata-marker"
        for outcome in ("rejected", "failed", "start-failed", "stopped", "cancelled"):
            with self.subTest(outcome=outcome):
                release = threading.Event()

                class GatedNative(NativeYuE2Adapter):
                    def execute(self, operation, runner):
                        if not release.wait(5):
                            raise RuntimeError("fixture cancellation gate deadline")
                        return super().execute(operation, runner)

                adapter = GatedNative(Path(sys.executable), self.root) if outcome == "cancelled" else self.native_adapter
                operation = self.native(f"diagnostic-{outcome}", lyrics=lyrics)
                operation = replace(
                    operation,
                    metadata=(*operation.metadata, ("api_token", secret), ("unknown_arbitrary", secret)),
                    command=replace(operation.command, environment=(
                        *operation.command.environment, ("PRIVATE_TOKEN", secret),
                    )),
                )
                if outcome == "rejected":
                    operation.expected[0].path.parent.mkdir(parents=True)
                    operation.expected[0].path.write_text("existing output")
                elif outcome == "start-failed":
                    operation = replace(operation, command=replace(
                        operation.command, executable=self.root / "missing-native-executable",
                    ))
                elif outcome == "stopped":
                    self.file("wait-primary", "")
                registry = RunRegistry(catalog=self.catalog)
                try:
                    submitted = registry.start(adapter, operation)
                    run = registry.get(submitted["id"])
                    if outcome == "cancelled":
                        run.stop()
                        release.set()
                    elif outcome == "stopped":
                        eventually(lambda: "primary-ready" in run.snapshot()["process"]["stdout"])
                        run.stop()
                    completed = eventually(lambda: (value := run.snapshot())["complete"] and value)
                    expected_state = {
                        "failed": ProcessState.EXITED, "stopped": ProcessState.SIGNALLED,
                    }.get(outcome, outcome)
                    self.assertEqual(completed["process"]["state"], expected_state)
                    self.assertIsNone(completed["catalog_error"])
                    diagnostic, = (
                        item for item in self.catalog.entries.values()
                        if item.stage == "execution.diagnostics" and item.producer_run == run.id
                    )
                    saved = json.loads(Path(diagnostic.path).read_text())
                    for label, document in (("submitted", submitted), ("final", completed), ("persisted", saved)):
                        with self.subTest(document=label):
                            serialized = json.dumps(document)
                            self.assertNotIn(lyrics, serialized)
                            self.assertNotIn(secret, serialized)
                            self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), serialized)
                            self.assertNotIn("PRIVATE_TOKEN", serialized)
                            self.assertNotIn("api_token", document["metadata"])
                            self.assertNotIn("unknown_arbitrary", document["metadata"])
                            self.assertEqual(document["metadata"]["cot"], "full")
                            self.assert_safe_lyric_command(document["command"], lyrics)
                            self.assert_safe_lyric_command(document["process"]["command"], lyrics)
                    self.assertIn(f"--lyrics={lyrics}", run.operation.command.arguments)
                    self.assertIn(("api_token", secret), run.operation.metadata)
                    if outcome in ("failed", "stopped"):
                        self.assertIn(f"--lyrics={lyrics}", json.loads((self.root / "received.json").read_text()))
                finally:
                    release.set()
                    registry.close()
                    (self.root / "wait-primary").unlink(missing_ok=True)

    def test_active_and_final_validation_command_diagnostics_are_safe(self):
        self.native_cli_fixture()
        lyrics = "Synthetic lyric content also present in validation command copies"

        class ValidatingNative(NativeYuE2Adapter):
            def complete(self, result, runner):
                validation = runner.run(replace(
                    result.operation.command,
                    arguments=(*result.operation.command.arguments, "--validation"),
                ))
                return replace(result, validation_processes=(validation,))

        registry = RunRegistry(catalog=self.catalog)
        try:
            adapter = ValidatingNative(Path(sys.executable), self.root)
            operation = self.native("validation-diagnostics", lyrics=lyrics)
            run = registry.get(registry.start(adapter, operation)["id"])
            live = eventually(lambda: (
                value if (value := run.snapshot())["active_validation"] is not None
                and "validation-ready" in value["active_validation"]["stdout"] else None
            ))
            self.file("release-validation", "")
            completed = eventually(lambda: (value := run.snapshot())["complete"] and value)
            saved_entry, = (
                item for item in self.catalog.entries.values()
                if item.stage == "execution.diagnostics" and item.producer_run == run.id
            )
            saved = json.loads(Path(saved_entry.path).read_text())
            self.assertEqual(completed["validation_processes"][0]["exit_status"], 9)
            for label, document in (("active", live), ("final", completed), ("persisted", saved)):
                with self.subTest(document=label):
                    self.assertNotIn(lyrics, json.dumps(document))
                    self.assert_safe_lyric_command(document["command"], lyrics)
                    self.assert_safe_lyric_command(document["process"]["command"], lyrics)
                    for process in document["validation_processes"]:
                        self.assert_safe_lyric_command(process["command"], lyrics)
                    if document["active_validation"] is not None:
                        self.assert_safe_lyric_command(document["active_validation"]["command"], lyrics)
            self.assertIn(f"--lyrics={lyrics}", run.operation.command.arguments)
            self.assertIn(f"--lyrics={lyrics}", json.loads((self.root / "received.json").read_text()))
        finally:
            self.file("release-validation", "")
            registry.close()

    def test_melisma_diagnostic_commands_keep_file_inputs_and_ustx_settings(self):
        for mode in ("abc", "ustx"):
            operation = self.melisma_operation(
                f"diagnostic-{mode}", mode, **({"singer": "selected-singer"} if mode == "ustx" else {}),
            )
            document = operation_document(operation)
            self.assertEqual(document["command"], {
                "executable": str(operation.command.executable),
                "arguments": list(operation.command.arguments),
                "cwd": str(operation.command.cwd), "display": operation.command.display,
            })
            self.assertIn(str(self.lyrics), document["command"]["arguments"])
            if mode == "ustx":
                self.assertIn("selected-singer", document["command"]["display"])
            self.assertEqual(document["metadata"]["stdout_artifact"], str(operation.expected[0].path))

    def test_malformed_setting_pairs_reject_direct_decode_and_catalog_reload(self):
        original = json.loads(self.history.read_text())
        evidence = replace(self.first.provenance, settings=(("a", "b"),))
        control = json.loads(json.dumps(asdict(evidence)))
        cases = (
            ("string", ["ab"]), ("object", [{"a": "b"}]),
            ("two-key-object", [{"a": "ignored", "b": "ignored"}]),
            ("null-pair", [None]), ("empty", [[]]), ("one-element", [["a"]]),
            ("three-elements", [["a", "b", "c"]]), ("non-string-key", [[1, "b"]]),
            ("empty-key", [["", "b"]]), ("nul-key", [["a\0", "b"]]),
            ("nul-value", [["a", "b\0"]]), ("nested-list", [["a", []]]),
            ("nested-object", [["a", {}]]), ("nan", [["a", float("nan")]]),
            ("infinity", [["a", float("inf")]]), ("negative-infinity", [["a", -float("inf")]]),
            ("duplicates", [["a", "b"], ["a", "b"]]),
        )
        try:
            for label, settings in cases:
                malformed = {**control, "settings": settings}
                with self.subTest(case=label, boundary="decoder"), self.assertRaises(ValueError):
                    provenance_from_document(malformed)
                for location in ("entry", "run"):
                    document = json.loads(json.dumps(original))
                    if location == "entry":
                        target = next(item for item in document["artifacts"] if item["id"] == self.first.id)
                    else:
                        target = document["runs"][0]["productions"][0]
                    target["provenance"] = malformed
                    self.history.write_text(json.dumps(document))
                    before = self.history.read_bytes()
                    with self.subTest(case=label, boundary=location), self.assertRaises(CatalogError):
                        ArtifactCatalog(self.history)
                    self.assertEqual(self.history.read_bytes(), before)
        finally:
            self.history.write_text(json.dumps(original))

    def test_decoder_rejects_non_json_pair_and_scalar_types(self):
        control = json.loads(json.dumps(asdict(replace(
            self.first.provenance, settings=(("a", "b"),),
        ))))
        for pair in (("a", "b"), ["a", b"bytes"], ["a", {"set"}], ["a", complex(1, 2)]):
            with self.subTest(pair=repr(pair)), self.assertRaises(ValueError):
                provenance_from_document({**control, "settings": [pair]})

    def test_valid_json_setting_pairs_round_trip_through_decoder_and_catalog(self):
        evidence = replace(self.first.provenance, settings=(
            ("a", "b"), ("boolean", True), ("integer", 7), ("float", 0.5), ("null", None),
        ))
        control = json.loads(json.dumps(asdict(evidence)))
        self.assertEqual(provenance_from_document(control), evidence)
        document = json.loads(self.history.read_text())
        next(item for item in document["artifacts"] if item["id"] == self.first.id)["provenance"] = control
        document["runs"][0]["productions"][0]["provenance"] = control
        self.history.write_text(json.dumps(document))
        before = self.history.read_bytes()
        loaded = ArtifactCatalog(self.history)
        self.assertEqual(loaded.get(self.first.id).provenance, evidence)
        self.assertEqual(loaded.runs["first-run"].productions[0].provenance, evidence)
        self.assertEqual(self.history.read_bytes(), before)

    def test_native_lyrics_are_only_content_revisions_in_provenance(self):
        lyrics = "These complete lyrical words must not be duplicated in provenance"
        operation = self.native(lyrics=lyrics)
        evidence = self.candidate(operation, "yue2-audio")
        for settings in (dict(operation.production.settings), evidence.canonical["settings"]):
            self.assertNotIn("lyrics", settings)
            self.assertEqual(
                settings["lyrics_revision"], hashlib.sha256(lyrics.encode("utf-8")).hexdigest(),
            )
        self.assertNotIn(lyrics, json.dumps(evidence.canonical))
        self.assertNotIn(lyrics, json.dumps(asdict(evidence)))
        self.assertNotIn(lyrics, json.dumps(asdict(operation.production)))
        self.assertIn(f"--lyrics={lyrics}", operation.command.arguments)

    def test_identical_and_changed_native_lyrics_have_corresponding_contexts(self):
        lyrics = "An unchanged refrain"
        first = self.candidate(self.native("one", lyrics=lyrics), "yue2-audio")
        same = self.candidate(self.native("two", lyrics="".join(("An unchanged ", "refrain"))), "yue2-audio")
        changed = self.candidate(self.native("three", lyrics=lyrics + "!"), "yue2-audio")
        self.assertEqual(first.canonical["settings"]["lyrics_revision"], same.canonical["settings"]["lyrics_revision"])
        self.assertEqual(first.signature, same.signature)
        self.assertNotEqual(first.canonical["settings"]["lyrics_revision"], changed.canonical["settings"]["lyrics_revision"])
        self.assertNotEqual(first.signature, changed.signature)

    def test_empty_native_lyrics_have_deterministic_content_evidence(self):
        empty = self.candidate(self.native(lyrics=""), "yue2-audio")
        self.assertEqual(
            empty.canonical["settings"]["lyrics_revision"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(empty.signature, self.candidate(self.native("other", lyrics=""), "yue2-audio").signature)
        for lyrics in (" ", "\n", "words"):
            with self.subTest(lyrics=repr(lyrics)):
                other = self.candidate(self.native(lyrics=lyrics), "yue2-audio")
                self.assertNotEqual(empty.signature, other.signature)
                self.assertNotEqual(empty.canonical["settings"]["lyrics_revision"], other.canonical["settings"]["lyrics_revision"])

    def test_native_unicode_lyrics_use_exact_utf8_without_normalization(self):
        for lyrics in ("Caf\u00e9 \u6708 \U0001f3b5\n", "Cafe\u0301 \u6708 \U0001f3b5\n"):
            with self.subTest(lyrics=repr(lyrics)):
                evidence = self.candidate(self.native(lyrics=lyrics), "yue2-audio")
                self.assertEqual(
                    evidence.canonical["settings"]["lyrics_revision"],
                    hashlib.sha256(lyrics.encode("utf-8")).hexdigest(),
                )
                self.assertNotIn("lyrics", evidence.canonical["settings"])
        self.assertNotEqual(
            self.candidate(self.native(lyrics="Caf\u00e9"), "yue2-audio").signature,
            self.candidate(self.native(lyrics="Cafe\u0301"), "yue2-audio").signature,
        )

    def test_credential_like_lyrics_are_accepted_without_persisting_text(self):
        for index, lyrics in enumerate((
            "Bearer of the light", "The secret = love", "I lost the token of remembrance",
        )):
            with self.subTest(lyrics=lyrics):
                operation = self.native(f"lyrics-{index}", lyrics=lyrics)
                entry = self.record_native(operation, f"lyrics-{index}")
                evidence = entry.provenance
                self.assertIsNotNone(evidence)
                self.assertEqual(
                    evidence.canonical["settings"]["lyrics_revision"],
                    hashlib.sha256(lyrics.encode("utf-8")).hexdigest(),
                )
                self.assertNotIn(lyrics, json.dumps(evidence.canonical))
                self.assertNotIn(lyrics, json.dumps(asdict(evidence)))
                self.assertNotIn(lyrics, self.history.read_text(encoding="utf-8"))
                loaded = ArtifactCatalog(self.history)
                self.assertEqual(loaded.get(entry.id).provenance, evidence)
                self.assertTrue(any(
                    item.provenance == evidence for item in loaded.runs[f"lyrics-{index}"].productions
                ))

    def test_lyric_content_exception_does_not_bypass_configuration_guards(self):
        for name in (
            "password", "api_key", "access_token", "bearer_token",
            "private_key", "credentials", "secret",
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                ProductionConfiguration("test-contract", ((name, "synthetic-value"),))
        for value in (
            "Bearer synthetic-value", "password=synthetic-value", "api_key=synthetic-value",
            "access_token=synthetic-value", "-----BEGIN PRIVATE KEY-----",
            "https://user:synthetic-value@example.invalid/model",
            "https://example.invalid/model?token=synthetic-value",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.native(style=value, lyrics="Bearer of the light")
                with self.assertRaises(ValueError):
                    ProductionConfiguration("test-contract", (("lyrics_revision", value),))
        with self.assertRaises(ValueError):
            ProductionConfiguration("Bearer synthetic-value")
        with self.assertRaises(ValueError):
            self.native(model=self.root / "Bearer synthetic-value")
        operation = self.catalog.bind_inputs(self.native())
        for resource in ("executable", "cwd"):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                catalog_module.production_provenance(replace(
                    operation, command=replace(operation.command, **{
                        resource: self.root / "Bearer synthetic-value",
                    }),
                ), "yue2-audio")

    def test_melisma_file_lyrics_still_use_captured_input_revisions(self):
        lyrics = "Bearer of the light\nCaf\u00e9"
        self.lyrics.write_text(lyrics, encoding="utf-8")
        operation = self.catalog.bind_inputs(self.melisma_operation("file-lyrics"))
        first = catalog_module.production_provenance(operation, "abc")
        source = next(item for item in first.inputs if item.role == "lyrics")
        self.assertEqual(source.revision, hashlib.sha256(lyrics.encode("utf-8")).hexdigest())
        self.assertEqual(len(first.inputs), 2)
        self.assertNotIn("lyrics_revision", first.canonical["settings"])
        self.assertNotIn(lyrics, json.dumps(asdict(first), ensure_ascii=False))
        self.lyrics.write_text(lyrics + "!", encoding="utf-8")
        self.assertEqual(first, catalog_module.production_provenance(operation, "abc"))
        self.assertNotEqual(
            first.signature,
            self.candidate(self.melisma_operation("changed-file-lyrics"), "abc").signature,
        )

    def test_in_progress_verbatim_native_lyrics_are_explicitly_rejected(self):
        entry = self.record_native(self.native(lyrics="previous inline words"), "old-native")
        document = json.loads(self.history.read_text())
        evidence = json.loads(json.dumps(asdict(entry.provenance)))
        evidence["settings"] = [
            pair for pair in evidence["settings"] if pair[0] not in ("lyrics", "lyrics_revision")
        ] + [["lyrics", "previous inline words"]]
        canonical = {
            key: value for key, value in evidence.items()
            if key not in ("signature", "output_revision")
        }
        canonical["settings"] = dict(evidence["settings"])
        evidence["signature"] = hashlib.sha256(json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        for location in ("entry", "run"):
            broken = json.loads(json.dumps(document))
            if location == "entry":
                next(item for item in broken["artifacts"] if item["id"] == entry.id)["provenance"] = evidence
            else:
                broken["runs"][-1]["productions"][0]["provenance"] = evidence
            self.history.write_text(json.dumps(broken))
            before = self.history.read_bytes()
            with self.subTest(location=location), self.assertRaisesRegex(CatalogError, "inline lyrics"):
                ArtifactCatalog(self.history)
            self.assertEqual(self.history.read_bytes(), before)
        self.history.write_text(json.dumps(document))
        self.assertEqual(ArtifactCatalog(self.history).get(entry.id).provenance, entry.provenance)

    def test_equivalent_context_is_canonical_across_ordering_and_reconstruction(self):
        operation = self.mix()
        first = self.candidate(operation, "mixed-audio")
        reordered = replace(
            operation,
            inputs=tuple(reversed(operation.inputs)),
            metadata=tuple(reversed(operation.metadata)),
            production=replace(operation.production, settings=tuple(reversed(operation.production.settings))),
        )
        second = self.candidate(reordered, "mixed-audio")
        self.assertEqual(first.canonical, second.canonical)
        self.assertEqual(first.signature, second.signature)
        self.assertEqual(first.signature, self.candidate(self.mix(), "mixed-audio").signature)
        encoded = json.dumps(first.canonical, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(first.signature, hashlib.sha256(encoded.encode("utf-8")).hexdigest())
        self.assertEqual(len(first.signature), 64)

    def test_input_content_revision_changes_but_unchanged_reconstruction_does_not(self):
        operation = self.melisma_operation("candidate")
        first = self.candidate(operation, "abc")
        self.assertEqual(first.signature, self.candidate(self.melisma_operation("rebuilt"), "abc").signature)
        self.midi.write_text("changed MIDI fixture")
        changed = self.candidate(operation, "abc")
        self.assertNotEqual(first.signature, changed.signature)
        self.assertNotEqual(first.canonical["inputs"], changed.canonical["inputs"])

    def test_input_identity_roles_and_locator_paths_are_distinct_concepts(self):
        operation = self.catalog.bind_inputs(self.mix())
        first = catalog_module.production_provenance(operation, "mixed-audio")
        self.assertIsNotNone(first)
        moved = replace(operation, inputs=tuple(
            replace(item, path=self.root / f"relocated-{item.role}") for item in operation.inputs
        ))
        self.assertEqual(first.signature, catalog_module.production_provenance(moved, "mixed-audio").signature)
        changed_role = replace(operation, inputs=tuple(
            replace(item, role="vocal" if item.role == "instrumental" else "instrumental")
            for item in operation.inputs
        ))
        self.assertNotEqual(first.signature, catalog_module.production_provenance(changed_role, "mixed-audio").signature)
        selected = self.catalog.bind_inputs(self.melisma_operation("selected"))
        changed_identity = replace(selected, inputs=(
            replace(selected.inputs[0], artifact_id="f" * 32), *selected.inputs[1:],
        ))
        self.assertNotEqual(
            catalog_module.production_provenance(selected, "abc").signature,
            catalog_module.production_provenance(changed_identity, "abc").signature,
        )
        self.assertNotIn("path", first.canonical["inputs"][0])

    def test_singer_phonemizer_renderer_only_affect_ustx_bundle_output(self):
        baseline = self.melisma_operation("bundle", "render-inputs")
        for setting, value in (
            ("singer", "another-singer"), ("phonemizer", "Another.Phonemizer"),
            ("renderer", "DIFFSINGER"),
        ):
            with self.subTest(setting=setting):
                changed = self.melisma_operation("other-bundle", "render-inputs", **{setting: value})
                self.assertNotEqual(
                    self.candidate(baseline, "ustx").signature,
                    self.candidate(changed, "ustx").signature,
                )
                for output_type in ("abc", "ir"):
                    self.assertEqual(
                        self.candidate(baseline, output_type).signature,
                        self.candidate(changed, output_type).signature,
                    )
        standalone = self.melisma_operation("singer.ustx", "ustx", singer="selected")
        self.assertEqual(self.candidate(standalone, "ustx").canonical["settings"]["singer"], "selected")

    def test_current_configuration_does_not_leak_into_abc_or_upstream_audio(self):
        source = DiscoverySource("melisma", self.root, self.executable)
        draft = {
            "workflow": "generate-abc",
            "inputs": {"midi": str(self.midi), "lyrics": str(self.lyrics),
                       "abc_output": str(self.root / "configured.abc")},
        }
        original = None
        for singer in ("first-singer", "second-singer"):
            api = ExecutionAPI(Configuration(sources=(
                source, DiscoverySource("openutau", self.root, configured=(("singer", singer),)),
            )), self.catalog)
            _, operation = api.construct(draft)
            evidence = self.candidate(operation, "abc")
            if original is None:
                original = evidence.signature
            self.assertEqual(evidence.signature, original)
        vocal = OpenUtauAdapter(self.executable, self.root).build(self.ustx, self.root / "render.wav")
        native = self.native()
        before = (self.candidate(vocal, "vocal-audio"), self.candidate(native, "yue2-audio"))
        self.assertNotEqual(
            self.candidate(self.mix(), "mixed-audio").signature,
            self.candidate(self.mix(vocal_gain=0.9), "mixed-audio").signature,
        )
        for operation, kind, previous in (
            (vocal, "vocal-audio", before[0]), (native, "yue2-audio", before[1]),
        ):
            noisy = replace(operation, metadata=(*operation.metadata, ("mix_gain", "0.9"), ("singer", "unrelated")))
            self.assertEqual(previous.signature, self.candidate(noisy, kind).signature)

    def test_each_mix_setting_and_selected_input_affects_only_the_mix_context(self):
        first = self.candidate(self.mix(), "mixed-audio")
        for setting, value in (
            ("instrumental_gain", 0.8), ("vocal_gain", 0.8), ("master_gain", 0.8),
            ("duration", "longest"), ("normalize", True),
        ):
            with self.subTest(setting=setting):
                self.assertNotEqual(first.signature, self.candidate(self.mix(**{setting: value}), "mixed-audio").signature)
        self.vocal.write_text("changed selected audio")
        self.assertNotEqual(first.signature, self.candidate(self.mix(), "mixed-audio").signature)

    def test_native_settings_resources_and_contract_are_captured(self):
        first = self.candidate(self.native(), "yue2-audio")
        for setting, value in (
            ("style", "strings"), ("lyrics", "new words"), ("seed", 43),
            ("cot", "melody"), ("device", "cuda"), ("model", self.root / "other-model"),
            ("vae", self.root / "other-vae"), ("abc", None),
        ):
            with self.subTest(setting=setting):
                self.assertNotEqual(first.signature, self.candidate(self.native(**{setting: value}), "yue2-audio").signature)
        self.native_adapter.contract_version = "changed-contract-version"
        self.assertNotEqual(first.signature, self.candidate(self.native(), "yue2-audio").signature)

    def test_helper_prepare_and_generate_capture_their_own_inputs_and_settings(self):
        def prepare(**updates):
            settings = dict(style="piano", run_id="instrumental", seed=42)
            settings.update(updates)
            return self.helper.prepare(self.abc, self.root / "preparation", **settings)

        first = self.candidate(prepare(), "configuration")
        for setting, value in (("style", "strings"), ("seed", 43)):
            self.assertNotEqual(first.signature, self.candidate(prepare(**{setting: value}), "configuration").signature)
        self.assertEqual(first.signature, self.candidate(prepare(run_id="another-id"), "configuration").signature)

        def generate(**updates):
            settings = dict(models_root=self.root / "models", device="cpu")
            settings.update(updates)
            return self.helper.generate(self.manifest.parent, self.root / "generation", **settings)

        generation = self.candidate(generate(), "instrumental-audio")
        for setting, value in (("models_root", self.root / "other-models"), ("device", "cuda")):
            self.assertNotEqual(generation.signature, self.candidate(generate(**{setting: value}), "instrumental-audio").signature)
        self.manifest.write_text('{"files":{"score.abc":{"sha256":"changed"}}}')
        self.assertNotEqual(generation.signature, self.candidate(generate(), "instrumental-audio").signature)

    def test_native_helper_and_output_semantic_types_do_not_collapse(self):
        native = self.candidate(self.native(), "yue2-audio")
        helper = self.candidate(self.helper.generate(
            self.manifest.parent, self.root / "generation", models_root=self.root / "models", device="cpu",
        ), "instrumental-audio")
        self.assertNotEqual(native.signature, helper.signature)
        self.assertEqual(native.canonical["stage"], "yue2-native.generate")
        self.assertEqual(helper.canonical["stage"], "yue2-instrumental.generate")
        self.assertEqual(native.canonical["tool"], "yue2-native")
        self.assertEqual(native.canonical["operation"], "generate")
        self.assertEqual(native.canonical["contract"], "0.1.6")
        self.assertEqual(helper.canonical["contract"], "0.1.5")
        self.assertEqual(native.canonical["output_type"], "yue2-audio")
        self.assertEqual(helper.canonical["output_type"], "instrumental-audio")
        bundle = self.melisma_operation("bundle", "render-inputs")
        self.assertNotEqual(self.candidate(bundle, "abc").signature, self.candidate(bundle, "ir").signature)
        self.assertNotEqual(self.candidate(bundle, "abc").signature, self.candidate(self.melisma_operation("single"), "abc").signature)

    def test_output_destination_run_id_and_publication_mode_are_incidental(self):
        self.assertEqual(
            self.candidate(self.native("one", run_id="one"), "yue2-audio").signature,
            self.candidate(self.native("two", run_id="two"), "yue2-audio").signature,
        )
        stdout = self.melisma_operation("stdout.abc")
        file_output = self.melisma.build("abc", self.midi, self.lyrics, self.root / "file.abc")
        self.assertNotEqual(stdout.command.arguments, file_output.command.arguments)
        self.assertEqual(self.candidate(stdout, "abc").signature, self.candidate(file_output, "abc").signature)
        second = self.produce(self.melisma_operation("second.abc"), "another-run")
        entry = next(item for item in self.catalog.entries.values() if item.producer_run == "another-run")
        self.assertNotEqual(self.first.producer_run, entry.producer_run)
        self.assertNotEqual(self.first.path, entry.path)
        self.assertEqual(self.first.provenance.signature, entry.provenance.signature)
        self.assertNotEqual(self.first_result.process, second.process)

    def test_tool_executable_and_source_selection_are_semantic_resource_paths(self):
        operation = self.catalog.bind_inputs(self.native())
        first = catalog_module.production_provenance(operation, "yue2-audio")
        self.assertIsNotNone(first)
        for change in (
            {"executable": self.root / "other-python"}, {"cwd": self.root / "other-source"},
        ):
            changed = replace(operation, command=replace(operation.command, **change))
            self.assertNotEqual(first.signature, catalog_module.production_provenance(changed, "yue2-audio").signature)

    def test_successful_history_survives_failed_stopped_retry_and_current_config(self):
        historical = self.first.provenance
        self.assertIsNotNone(historical)
        for name, code, stopped in (("failed", 7, False), ("stopped", 0, True)):
            operation = self.catalog.begin(self.catalog.bind_inputs(self.melisma_operation(name)))
            try:
                process = ProcessResult(operation.command, ProcessState.EXITED, code, pid=123, stop_requested=stopped)
                result = OperationResult(operation, process, tuple(inspect_artifact(item) for item in operation.expected))
                self.assertFalse(result.success)
                self.catalog.register_result(result, name)
            finally:
                self.catalog.end(operation)
            self.assertEqual(self.catalog.get(self.first.id).provenance, historical)
        self.produce(self.melisma_operation("retry"), "retry")
        self.midi.write_text("new currently selected MIDI")
        self.assertNotEqual(historical.signature, self.candidate(self.melisma_operation("future"), "abc").signature)
        self.assertEqual(self.catalog.get(self.first.id).provenance, historical)
        self.assertEqual(ArtifactCatalog(self.history).get(self.first.id).provenance, historical)

    def test_same_path_replacement_preserves_earlier_successful_run_provenance(self):
        original = self.first.provenance
        self.assertIsNotNone(original)
        Path(self.first.path).unlink()
        operation = self.catalog.begin(self.catalog.bind_inputs(self.first_operation))
        try:
            result = OperationResult(
                operation, ProcessResult(operation.command, ProcessState.EXITED, 7, pid=123),
                tuple(inspect_artifact(item) for item in operation.expected),
            )
            self.catalog.register_result(result, "failed-same-path")
        finally:
            self.catalog.end(operation)
        self.assertEqual(self.catalog.get(self.first.id).production_status, "missing")
        production, = self.catalog.runs["first-run"].productions
        self.assertEqual(production.artifact_id, self.first.id)
        self.assertEqual(production.provenance, original)
        self.midi.write_text("new input for the same path")
        self.produce(self.first_operation, "successful-same-path")
        self.assertNotEqual(self.catalog.get(self.first.id).provenance.signature, original.signature)
        loaded = ArtifactCatalog(self.history)
        self.assertEqual(loaded.runs["first-run"].productions, (production,))
        self.assertTrue(loaded.runs["first-run"].success)
        self.assertFalse(loaded.runs["failed-same-path"].success)

    def test_openutau_depends_on_selected_ustx_not_unrelated_abc(self):
        operation = OpenUtauAdapter(self.executable, self.root).build(self.ustx, self.root / "vocal-output.wav")
        first = self.candidate(operation, "vocal-audio")
        self.abc.write_text("unrelated score edit")
        self.assertEqual(first.signature, self.candidate(operation, "vocal-audio").signature)
        self.ustx.write_text("new singer metadata in USTX")
        changed = self.candidate(operation, "vocal-audio")
        self.assertNotEqual(first.signature, changed.signature)
        self.assertEqual(changed.canonical["settings"], {})

    def test_round_trip_and_backend_serialization_keep_inspectable_provenance(self):
        evidence = self.first.provenance
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.canonical["stage"], "melisma.abc")
        self.assertEqual(evidence.canonical["output_type"], "abc")
        sources = {item["role"]: item for item in evidence.canonical["inputs"]}
        for source in self.first.sources:
            self.assertEqual(sources[source.role]["artifact_id"], source.artifact_id)
            self.assertEqual(sources[source.role]["revision"], source.revision)
        loaded = ArtifactCatalog(self.history)
        self.assertEqual(loaded.get(self.first.id).provenance, evidence)
        document = json.loads(json.dumps(stage_document("melisma.abc", StageState.READY, catalog=loaded)))
        entry = next(item for item in document["artifacts"] if item["id"] == self.first.id)
        self.assertEqual(entry["provenance"]["signature"], evidence.signature)
        self.assertEqual(entry["provenance"]["contract"], "melisma-phase1")
        self.assertIn("production", operation_document(self.first_result.operation))

    def test_audio_adapter_productions_persist_their_actual_semantic_types(self):
        operations = (
            (OpenUtauAdapter(self.executable, self.root).build(
                self.ustx, self.root / "rendered.wav",
            ), "vocal-audio"),
            (self.native(), "yue2-audio"),
            (self.helper.generate(
                self.manifest.parent, self.root / "generation",
                models_root=self.root / "models", device="cpu",
            ), "instrumental-audio"),
            (self.mix(), "mixed-audio"),
        )
        for index, (operation, output_type) in enumerate(operations):
            bound = self.catalog.begin(self.catalog.bind_inputs(operation))
            try:
                # Model/ffprobe execution is outside this model-free test.
                # Supply the structured successful validation result at the
                # existing adapter-to-catalog boundary using real built outputs.
                for output in bound.expected:
                    output.path.parent.mkdir(parents=True, exist_ok=True)
                    output.path.write_text("{}" if output.kind == "json" else "audio fixture")
                result = OperationResult(
                    bound, ProcessResult(bound.command, ProcessState.EXITED, 0, pid=123),
                    tuple(inspect_artifact(item) for item in bound.expected),
                )
                self.assertTrue(result.success)
                self.catalog.register_result(result, f"audio-{index}")
            finally:
                self.catalog.end(bound)
            entry = next(item for item in self.catalog.entries.values()
                         if item.producer_run == f"audio-{index}" and item.type == output_type)
            self.assertIsNotNone(entry.provenance)
            self.assertEqual(entry.provenance.canonical["output_type"], output_type)
            self.assertEqual(entry.provenance.stage, operation.stage)
            loaded = ArtifactCatalog(self.history)
            self.assertEqual(loaded.get(entry.id).provenance, entry.provenance)
            self.assertTrue(any(
                item.artifact_id == entry.id and item.provenance == entry.provenance
                for item in loaded.runs[f"audio-{index}"].productions
            ))

    def test_inspecting_canonical_fields_cannot_mutate_historical_provenance(self):
        original = self.first.provenance
        self.assertIsNotNone(original)
        canonical = original.canonical
        canonical["settings"]["singer"] = "not recorded"
        canonical["inputs"].clear()
        self.assertNotIn("singer", original.canonical["settings"])
        self.assertTrue(original.canonical["inputs"])
        self.assertEqual(self.catalog.get(self.first.id).provenance, original)
        self.assertEqual(ArtifactCatalog(self.history).get(self.first.id).provenance, original)

    def test_legacy_records_remain_unknown_and_never_match_by_default(self):
        raw = json.loads(self.history.read_text())
        for entry in raw["artifacts"]:
            entry.pop("provenance", None)
        for run in raw["runs"]:
            run.pop("productions", None)
        self.history.write_text(json.dumps(raw))
        loaded = ArtifactCatalog(self.history)
        entry = loaded.get(self.first.id)
        self.assertIsNone(entry.provenance)
        self.assertEqual(loaded.runs["first-run"].productions, ())
        candidate = self.candidate(self.melisma_operation("legacy-candidate"), "abc")
        self.assertIsNone(catalog_module.same_production_context(entry.provenance, candidate))
        loaded.refresh()
        self.assertIsNone(loaded.get(self.first.id).provenance)
        self.assertEqual(loaded.get(self.first.id).producer_run, "first-run")

    def test_imported_artifacts_keep_content_evidence_without_generated_provenance(self):
        entry = self.catalog.register_input(self.ustx, "ustx")
        self.assertIsNone(entry.provenance)
        self.assertIsNone(entry.stage)
        self.assertIsNone(entry.producer_run)
        self.assertEqual(entry.revision, fingerprint(self.ustx))
        operation = OpenUtauAdapter(self.executable, self.root).build(self.ustx, self.root / "render.wav")
        evidence = self.candidate(operation, "vocal-audio")
        source, = evidence.canonical["inputs"]
        self.assertEqual(source["artifact_id"], entry.id)
        self.assertEqual(source["revision"], entry.revision)
        self.assertEqual(source["role"], "ustx")
        self.assertEqual(ArtifactCatalog(self.history).get(entry.id), entry)

    def test_refresh_and_stage_state_do_not_rewrite_original_production_evidence(self):
        original = self.first.provenance
        self.assertIsNotNone(original)
        self.assertEqual(original.output_revision, self.first.revision)
        for state in StageState:
            document = stage_document("melisma.abc", state, catalog=self.catalog)
            self.assertEqual(document["state"], state.value)
            self.assertEqual(self.catalog.get(self.first.id).provenance, original)
        Path(self.first.path).write_text("edited externally")
        self.catalog.refresh()
        edited = self.catalog.get(self.first.id)
        self.assertNotEqual(edited.revision, original.output_revision)
        self.assertEqual(edited.provenance, original)
        Path(self.first.path).unlink()
        self.catalog.refresh()
        self.assertFalse(self.catalog.get(self.first.id).exists)
        self.assertEqual(self.catalog.get(self.first.id).provenance, original)

    def test_unknown_unbound_and_non_generation_operations_have_unknown_evidence(self):
        unbound = self.melisma_operation("unbound")
        self.assertIsNone(catalog_module.production_provenance(unbound, "abc"))
        play = FFplayAdapter(self.executable, self.root).build(self.vocal)
        self.assertEqual(play.expected, ())
        self.assertIsNone(catalog_module.production_provenance(self.catalog.bind_inputs(play), "audio"))
        self.file("prepared/delivery-manifest.json", "{}")
        verify = self.helper.verify(self.manifest.parent)
        self.assertIsNone(catalog_module.production_provenance(self.catalog.bind_inputs(verify), "diagnostic"))
        legacy_operation = replace(self.catalog.bind_inputs(unbound), production=None)
        self.assertIsNone(catalog_module.production_provenance(legacy_operation, "abc"))

    def test_metadata_environment_and_credentials_are_not_provenance(self):
        operation = self.melisma_operation("credentials.abc")
        first = self.candidate(operation, "abc")
        secret = "synthetic-private-test-value"
        changed = replace(
            operation,
            metadata=(*operation.metadata, ("api_token", secret), ("timestamp", "tomorrow")),
            command=replace(operation.command, environment=(("PRIVATE_TOKEN", secret),)),
        )
        evidence = self.candidate(changed, "abc")
        self.assertEqual(first.signature, evidence.signature)
        serialized = json.dumps(asdict(evidence))
        self.assertNotIn(secret, serialized)
        self.assertNotIn(hashlib.sha256(secret.encode()).hexdigest(), serialized)
        self.assertNotIn("PRIVATE_TOKEN", serialized)

    def test_recognizable_credentials_in_generation_text_are_rejected_before_hashing(self):
        with self.assertRaises(ValueError):
            self.native(style="Bearer synthetic-private-test-value")

    def test_register_result_preserves_inputs_captured_before_completion(self):
        operation = self.catalog.begin(self.catalog.bind_inputs(self.melisma_operation("captured")))
        try:
            result = self.melisma.execute(operation, ProcessRunner())
            self.assertTrue(result.success)
            revision = next(item.revision for item in operation.inputs if item.role == "midi")
            self.midi.write_text("changed after execution, before registration")
            self.catalog.register_result(result, "captured")
        finally:
            self.catalog.end(operation)
        entry = next(item for item in self.catalog.entries.values() if item.producer_run == "captured")
        self.assertIsNotNone(entry.provenance)
        recorded = next(item for item in entry.provenance.canonical["inputs"] if item["role"] == "midi")
        self.assertEqual(recorded["revision"], revision)
        self.assertNotEqual(recorded["revision"], fingerprint(self.midi))

    def test_partial_and_missing_outputs_do_not_claim_successful_provenance(self):
        for status in ("partial", "missing"):
            operation = self.catalog.begin(self.catalog.bind_inputs(self.melisma_operation(status)))
            try:
                if status == "partial":
                    Path(operation.expected[0].path).write_text("partial output")
                process = ProcessResult(operation.command, ProcessState.EXITED, 7, pid=123)
                result = OperationResult(operation, process, tuple(inspect_artifact(item) for item in operation.expected))
                self.catalog.register_result(result, status)
            finally:
                self.catalog.end(operation)
            entry = next(item for item in self.catalog.entries.values() if item.producer_run == status)
            self.assertEqual(entry.production_status, status)
            self.assertEqual(entry.exists, status == "partial")
            self.assertIsNone(entry.provenance)
        self.assertIsNotNone(self.catalog.get(self.first.id).provenance)

    def test_saved_provenance_rejects_corrupt_digest_and_unsupported_version(self):
        document = json.loads(self.history.read_text())
        saved = next(item for item in document["artifacts"] if item["id"] == self.first.id)
        self.assertIsNotNone(saved["provenance"])
        for key, value in (("signature", "0" * 64), ("version", 999), ("contract", "tampered")):
            broken = json.loads(json.dumps(document))
            entry = next(item for item in broken["artifacts"] if item["id"] == self.first.id)
            entry["provenance"][key] = value
            self.history.write_text(json.dumps(broken))
            with self.subTest(key=key), self.assertRaises(CatalogError):
                ArtifactCatalog(self.history)
        self.history.write_text(json.dumps(document))

    def test_comparison_only_reports_context_equality_without_lifecycle_changes(self):
        original = self.first.provenance
        self.assertIsNotNone(original)
        same = self.candidate(self.melisma_operation("same"), "abc")
        self.assertIs(catalog_module.same_production_context(original, same), True)
        self.midi.write_text("changed input")
        different = self.candidate(self.melisma_operation("different"), "abc")
        before = self.history.read_bytes()
        self.assertIs(catalog_module.same_production_context(original, different), False)
        self.assertIsNone(catalog_module.same_production_context(original, None))
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(self.catalog.get(self.first.id).provenance, original)
        self.assertEqual(stage_document("melisma.abc", StageState.SUCCESS, catalog=self.catalog)["state"], "success")


if __name__ == "__main__":
    unittest.main()
