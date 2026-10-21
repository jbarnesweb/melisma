"""Issue #7 local catalog, provenance, inspection and editing contracts."""

import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import MelismaAdapter, ToolAdapter
from tools.orchestration.artifact_catalog import (
    ArtifactCatalog, CatalogError, TEXT_LIMIT, fingerprint,
)
import tools.orchestration.artifact_catalog as catalog_module
from tools.orchestration.artifacts import inspect_artifact
from tools.orchestration.execution import RunRegistry
from tools.orchestration.process import (
    Command, ExpectedArtifact, Operation, OperationInput, OperationResult,
    ProcessResult, ProcessState,
)
from tools.web_ui.workflows import accepted_artifact_types
from test_execution import eventually


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "state" / "artifacts.json"
        self.catalog = ArtifactCatalog(self.history)
        self.command = Command(Path(sys.executable).resolve(), (), self.root)

    def file(self, name, text="contents"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def result(self, path, *, kind="file", tool="fixture", name="generate", code=0, inputs=()):
        operation = Operation(
            tool, name, self.command, (ExpectedArtifact(path, kind),), inputs=inputs,
        )
        process = ProcessResult(self.command, ProcessState.EXITED, code, pid=123)
        return OperationResult(
            operation, process, tuple(inspect_artifact(item) for item in operation.expected),
        )

    def entries(self):
        return self.catalog.document()["artifacts"]

    def test_import_identity_and_revision_survive_restart_without_producer(self):
        entry = self.catalog.register_input(self.file("existing.mid"), "midi")
        again = self.catalog.register_input(Path(entry.path), "midi")
        self.assertEqual(entry.id, again.id)
        restarted = ArtifactCatalog(self.history)
        restored = restarted.get(entry.id)
        self.assertEqual(restored.revision, entry.revision)
        self.assertIsNone(restored.producer_run)
        self.assertEqual(restored.production_status, "imported")
        self.assertNotEqual(restarted.session_id, self.catalog.session_id)
        self.assertEqual(restored.session_id, entry.session_id)
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.catalog.register_input(Path(entry.path), "lyrics")

    def test_explicit_inputs_preview_is_read_only_and_start_checks_revision(self):
        midi = self.file("input.mid")
        lyrics = self.file("lyrics.txt")
        operation = MelismaAdapter(self.command.executable, self.root).build(
            "abc", midi, lyrics, self.root / "output.abc", publish_stdout=True,
        )
        self.assertEqual([item.role for item in operation.inputs], ["midi", "lyrics"])
        bound = self.catalog.bind_inputs(operation)
        self.assertFalse(self.history.exists())
        self.assertFalse(self.entries())
        self.assertTrue(all(item.revision for item in bound.inputs))
        lyrics.write_text("edited", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since preview"):
            self.catalog.begin(bound)
        self.assertFalse(self.history.exists())
        self.assertFalse(self.catalog.active_paths)

    def test_validity_is_separate_from_production_and_missing_is_not_success(self):
        for name, code, content in (("good", 0, "data"), ("partial", 3, "data"), ("missing", 0, None)):
            path = self.root / name
            if content:
                path.write_text(content)
            result = self.result(path, code=code)
            self.catalog.register_result(result, name)
            entry = next(item for item in self.entries() if item["path"] == str(path))
            self.assertEqual(entry["valid"], content is not None)
            self.assertEqual(
                entry["production_status"],
                "successful" if name == "good" else "partial" if name == "partial" else "missing",
            )
            self.assertEqual(result.success, name == "good")
        compatible = self.catalog.compatible(("file",))
        self.assertEqual([Path(item["path"]).name for item in compatible], ["good"])

    def test_rejected_start_failure_and_publication_conflict_do_not_claim_outputs(self):
        path = self.file("occupied")
        original = fingerprint(path)
        base = self.result(path)
        for index, process in enumerate((
            replace(base.process, state=ProcessState.REJECTED, exit_status=None),
            replace(base.process, state=ProcessState.START_FAILED, pid=None),
            replace(base.process, publication_error="destination already exists"),
        )):
            self.catalog.register_result(replace(base, process=process), f"run-{index}")
        self.assertFalse(self.entries())
        self.assertEqual(fingerprint(path), original)
        self.assertEqual(len(self.catalog.runs), 3)

    def test_native_and_instrumental_semantics_and_duplicate_manifest_validation(self):
        for tool, name, expected in (("yue2-native", "generate", "yue2-audio"),
                                     ("yue2-instrumental", "generate", "instrumental-audio"),
                                     ("openutau", "render", "vocal-audio"),
                                     ("ffmpeg", "mix", "mixed-audio")):
            path = self.file(f"{tool}.flac")
            result = self.result(path, kind="audio", tool=tool, name=name)
            bad = replace(inspect_artifact(ExpectedArtifact(path)), valid=False, error="hash mismatch")
            self.catalog.register_result(replace(
                result, artifacts=(*result.artifacts, bad), errors=("hash mismatch",),
            ), tool)
            entry = next(item for item in self.entries() if item["path"] == str(path))
            self.assertEqual(entry["type"], expected)
            self.assertFalse(entry["valid"])
            self.assertEqual(entry["kind"], "audio")
        self.assertEqual(len(self.entries()), 4)
        native = self.result(self.file("native-ok.flac"), kind="audio", tool="yue2-native")
        self.catalog.register_result(native, "native-ok")
        self.assertFalse(self.catalog.compatible(
            accepted_artifact_types("mix-audio", "instrumental"),
        ))
        self.assertEqual(len(self.catalog.compatible(
            accepted_artifact_types("play-audio", "audio"),
        )), 1)

    def test_audio_semantics_require_exact_producing_operation_without_io(self):
        for tool, name, expected_type in (
            ("yue2-instrumental", "prepare", "audio"),
            ("yue2-instrumental", "verify", "audio"),
            ("yue2-instrumental", "generate", "instrumental-audio"),
            ("yue2-native", "generate", "yue2-audio"),
            ("yue2-native", "prepare", "audio"),
            ("openutau", "render", "vocal-audio"),
            ("openutau", "generate", "audio"),
            ("ffmpeg", "mix", "mixed-audio"),
            ("ffmpeg", "generate", "audio"),
        ):
            for suffix, kind in (
                (".wav", "file"), (".FLAC", "file"), (".mp3", "file"),
                (".ogg", "file"), (".bin", "audio"),
            ):
                declaration = ExpectedArtifact(self.root / f"unopened{suffix}", kind)
                operation = Operation(tool, name, self.command, (declaration,))
                record = catalog_module.ArtifactRecord(declaration, True, 10, True)
                with self.subTest(stage=operation.stage, suffix=suffix, kind=kind):
                    with mock.patch.object(Path, "open", side_effect=AssertionError("classification I/O")):
                        self.assertEqual(catalog_module.semantic_type(operation, record), expected_type)
        self.assertFalse(self.history.exists())

    def test_failed_output_can_be_regenerated_after_removing_partial_file(self):
        path = self.file("partial.txt")
        self.catalog.register_result(self.result(path, code=3), "failed")
        previous = self.entries()[0]
        path.unlink()
        path.write_text("new complete output")
        self.catalog.register_result(self.result(path), "successful")
        updated = self.entries()[0]
        self.assertEqual(updated["id"], previous["id"])
        self.assertNotEqual(updated["revision"], previous["revision"])
        self.assertEqual(updated["producer_run"], "successful")
        self.assertEqual(updated["production_status"], "successful")

    def test_history_corruption_version_and_schema_fail_explicitly(self):
        self.history.parent.mkdir()
        for data in ("{", '{"schema_version":2,"artifacts":[],"runs":[]}',
                     '{"schema_version":1,"artifacts":[{}],"runs":[]}',
                     '{"schema_version":1,"schema_version":1,"artifacts":[],"runs":[]}'):
            self.history.write_text(data)
            with self.assertRaises(CatalogError):
                ArtifactCatalog(self.history)
            self.assertEqual(self.history.read_text(), data)

    def test_atomic_write_failure_preserves_history_and_memory(self):
        self.catalog.register_input(self.file("one"), "text")
        before = self.history.read_bytes()
        with mock.patch("tools.orchestration.artifact_catalog.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(CatalogError, "disk failure"):
                self.catalog.register_input(self.file("two"), "text")
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(list(self.history.parent.iterdir()), [self.history])

    def test_reading_text_never_writes_and_renders_literal_markup(self):
        path = self.file("lyrics.txt", '<script>literal</script>\nGlo --')
        entry = self.catalog.register_input(path, "lyrics")
        before = self.history.read_bytes()
        with mock.patch("tools.orchestration.artifact_catalog.os.replace", side_effect=AssertionError("write")):
            view = self.catalog.read_text(entry.id)
        self.assertEqual(view["text"], path.read_text())
        self.assertTrue(view["editable"])
        self.assertFalse(view["truncated"])
        self.assertEqual(self.history.read_bytes(), before)

    def test_text_formats_and_read_only_configuration_diagnostics(self):
        for type_ in ("abc", "ustx", "lyrics", "configuration", "diagnostic", "text"):
            entry = self.catalog.register_input(self.file(type_), type_)
            view = self.catalog.read_text(entry.id)
            self.assertEqual(view["editable"], type_ not in ("configuration", "diagnostic"))
        entry = self.catalog.register_input(self.file("audio.wav"), "audio")
        self.assertIsNone(entry.valid)
        with self.assertRaisesRegex(ValueError, "text format"):
            self.catalog.read_text(entry.id)

    def test_ir_is_inspectable_but_not_editable(self):
        path = self.file("bound.jsonl", '{"type":"header","version":1,"ticks_per_quarter":480}\n')
        self.catalog.register_result(self.result(path, kind="ir", tool="melisma"), "ir")
        view = self.catalog.read_text(self.entries()[0]["id"])
        self.assertIn("ticks_per_quarter", view["text"])
        self.assertFalse(view["editable"])

    def test_truncated_binary_and_invalid_utf8_are_never_editable(self):
        path = self.file("large.txt", "x" * (TEXT_LIMIT - 1) + "\u00e9" * 2)
        entry = self.catalog.register_input(path, "lyrics")
        view = self.catalog.read_text(entry.id)
        self.assertTrue(view["truncated"])
        self.assertFalse(view["editable"])
        self.assertEqual(view["text"], "x" * (TEXT_LIMIT - 1))
        with self.assertRaises(ValueError):
            self.catalog.save_text(entry.id, entry.revision, view["text"], self.root / "truncated")
        self.assertFalse((self.root / "truncated").exists())
        for name, data in (("binary", b"abc\0def"), ("non-utf8", b"\xff")):
            path = self.root / name
            path.write_bytes(data)
            entry = self.catalog.register_input(path, "text")
            with self.assertRaises((ValueError, UnicodeError)):
                self.catalog.read_text(entry.id)

    def consumer(self, source):
        operation = Operation(
            "fixture", "consume", self.command, (),
            inputs=(OperationInput("lyrics", Path(source.path), "lyrics"),),
        )
        operation = self.catalog.begin(self.catalog.bind_inputs(operation))
        process = ProcessResult(self.command, ProcessState.EXITED, 0, pid=123)
        self.catalog.register_result(OperationResult(operation, process, ()), "consumer")
        self.catalog.end(operation)
        return operation

    def test_save_as_preserves_source_and_does_not_stale_known_consumer(self):
        source = self.catalog.register_input(self.file("lyrics", "old"), "lyrics")
        self.consumer(source)
        target = self.root / "new-lyrics"
        edited = self.catalog.save_text(source.id, source.revision, "new", target)
        self.assertEqual(Path(source.path).read_text(), "old")
        self.assertEqual(target.read_text(), "new")
        self.assertNotEqual(source.id, edited.id)
        self.assertEqual(edited.derived_from, source.id)
        self.assertEqual(edited.sources[0].revision, source.revision)
        self.assertFalse(self.catalog.changed_inputs("consumer"))

    def test_replace_preserves_backup_and_only_marks_direct_consumers_stale(self):
        source = self.catalog.register_input(self.file("lyrics", "old"), "lyrics")
        Path(source.path).chmod(0o640)
        self.consumer(source)
        view = self.catalog.read_text(source.id)
        updated = self.catalog.save_text(
            source.id, view["artifact"]["revision"], "new", Path(source.path), replace_existing=True,
        )
        self.assertEqual(Path(source.path).read_text(), "new")
        self.assertEqual(stat.S_IMODE(Path(source.path).stat().st_mode), 0o640)
        self.assertEqual(updated.revision, fingerprint(Path(source.path)))
        self.assertNotEqual(updated.revision, source.revision)
        backup, = self.root.glob("lyrics.backup-*")
        self.assertEqual(backup.read_text(), "old")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.catalog.changed_inputs("consumer"), [source.id])
        restarted = ArtifactCatalog(self.history)
        run, = restarted.document()["consumers"]
        self.assertTrue(run["stale"])
        self.assertTrue(run["success"])
        self.assertEqual(run["sources"][0]["revision"], source.revision)
        self.assertEqual(restarted.get(source.id).backup_path, str(backup))

    def test_save_as_refuses_missing_registered_path_without_merging_history(self):
        destination = self.file("registered.txt", "previous output")
        self.catalog.register_result(self.result(destination), "previous-producer")
        registered = self.catalog._at_path(destination)
        source = self.catalog.register_input(self.file("source.txt", "source"), "text")
        before = self.history.read_bytes()
        destination.unlink()
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.catalog.save_text(source.id, source.revision, "new contents", destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(self.catalog.get(registered.id), registered)
        self.assertEqual(self.catalog.get(registered.id).producer_run, "previous-producer")
        paths = [Path(entry["path"]).resolve() for entry in self.entries()]
        self.assertEqual(len(paths), len(set(paths)))
        copied = self.catalog.save_text(
            source.id, source.revision, "new contents", self.root / "unregistered.txt",
        )
        self.catalog.refresh()
        chosen = next(item for item in self.catalog.compatible(("text",)) if item["id"] == copied.id)
        operation = self.consumer(self.catalog.get(chosen["id"]))
        self.assertEqual(operation.inputs[0].artifact_id, chosen["id"])
        self.catalog.save_text(
            chosen["id"], chosen["revision"], "edited", Path(chosen["path"]), replace_existing=True,
        )
        self.assertEqual(self.catalog.changed_inputs("consumer"), [chosen["id"]])
        consumer, = self.catalog.document()["consumers"][-1:]
        self.assertTrue(consumer["stale"])
        self.assertTrue(consumer["success"])
        self.assertEqual(self.catalog.get(registered.id).producer_run, "previous-producer")

    def test_duplicate_canonical_paths_fail_load_commit_and_lookup(self):
        source = self.catalog.register_input(self.file("source.txt"), "text")
        original = self.history.read_bytes()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        for path in (source.path, str(alias / "source.txt")):
            duplicate = replace(source, id="f" * 32, path=path)
            self.catalog.entries[duplicate.id] = duplicate
            with self.assertRaisesRegex(CatalogError, "Duplicate canonical artifact path"):
                self.catalog._at_path(Path(source.path))
            with self.assertRaisesRegex(CatalogError, "Duplicate canonical artifact path"):
                self.catalog._commit(self.catalog.entries, self.catalog.runs)
            self.assertEqual(self.history.read_bytes(), original)
            document = json.loads(original)
            document["artifacts"].append({
                **document["artifacts"][0], "id": duplicate.id, "path": path,
            })
            contents = json.dumps(document)
            self.history.write_text(contents)
            with self.assertRaisesRegex(CatalogError, "Duplicate canonical artifact path"):
                ArtifactCatalog(self.history)
            self.assertEqual(self.history.read_text(), contents)
            del self.catalog.entries[duplicate.id]
            self.history.write_bytes(original)

    def test_replacement_does_not_copy_special_permission_bits(self):
        source = self.catalog.register_input(self.file("permissions.txt"), "text")
        path = Path(source.path)
        path.chmod(0o7640)
        self.catalog.save_text(
            source.id, source.revision, "updated", path, replace_existing=True,
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        backup, = self.root.glob("permissions.txt.backup-*")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_save_as_preparation_failure_leaves_no_partial_destination_or_registration(self):
        source = self.catalog.register_input(self.file("source.txt"), "text")
        destination = self.root / "new.txt"
        before = self.history.read_bytes()
        original_paths = set(self.root.iterdir())
        make_temporary = tempfile.NamedTemporaryFile

        def partial_write(**kwargs):
            stream = make_temporary(**kwargs)
            write = stream.write

            def fail(contents):
                write(contents[:2])
                raise OSError("injected partial write")

            stream.write = fail
            return stream

        for patch in (
            mock.patch.object(catalog_module.tempfile, "NamedTemporaryFile", side_effect=partial_write),
            mock.patch.object(catalog_module.os, "fsync", side_effect=OSError("injected fsync failure")),
        ):
            with patch, self.assertRaises(OSError):
                self.catalog.save_text(source.id, source.revision, "new contents", destination)
            self.assertFalse(destination.exists())
            self.assertEqual(set(self.root.iterdir()), original_paths)
            self.assertEqual(self.history.read_bytes(), before)
            self.assertEqual(len(self.entries()), 1)

    def test_save_as_publication_races_never_clobber_files_or_symlinks(self):
        source = self.catalog.register_input(self.file("source.txt"), "text")
        link = os.link
        before = self.history.read_bytes()
        for symlink in (False, True):
            destination = self.root / f"racing-{symlink}.txt"

            def occupied(temporary, path):
                self.assertEqual(Path(temporary).read_text(), "new contents")
                if symlink:
                    Path(path).symlink_to(self.root / "absent")
                else:
                    Path(path).write_text("other writer")
                return link(temporary, path)

            with (
                mock.patch.object(catalog_module.os, "link", side_effect=occupied),
                self.assertRaisesRegex(ValueError, "occupied destination"),
            ):
                self.catalog.save_text(source.id, source.revision, "new contents", destination)
            self.assertTrue(destination.is_symlink() if symlink else destination.read_text() == "other writer")
            self.assertEqual(self.history.read_bytes(), before)
            self.assertIsNone(self.catalog._at_path(destination))
        self.assertFalse((self.root / "absent").exists())

    def test_file_and_directory_fsync_order_for_replacement_and_history(self):
        source = self.catalog.register_input(self.file("source.txt", "old"), "text")
        path = Path(source.path)
        path.chmod(0o640)
        events = []
        fsync = os.fsync
        link = os.link
        replace_file = os.replace
        sync_directory = catalog_module.fsync_directory

        def syncing(descriptor):
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                events.append("file-fsync")
            return fsync(descriptor)

        def linking(temporary, destination):
            events.append("backup-link")
            self.assertEqual(path.read_text(), "old")
            return link(temporary, destination)

        def replacing(temporary, destination):
            events.append("history-replace" if Path(destination) == self.history else "text-replace")
            return replace_file(temporary, destination)

        def directory(path):
            events.append("history-directory" if path == self.history.parent else "text-directory")
            return sync_directory(path)

        with (
            mock.patch.object(catalog_module.os, "fsync", side_effect=syncing),
            mock.patch.object(catalog_module.os, "link", side_effect=linking),
            mock.patch.object(catalog_module.os, "replace", side_effect=replacing),
            mock.patch.object(catalog_module, "fsync_directory", side_effect=directory),
        ):
            self.catalog.save_text(source.id, source.revision, "new", path, replace_existing=True)
        self.assertEqual(events, [
            "file-fsync", "backup-link", "text-directory",
            "file-fsync", "text-replace", "text-directory",
            "file-fsync", "history-replace", "history-directory",
        ])

    def test_save_as_fsyncs_complete_private_contents_then_links_and_syncs_directory(self):
        source = self.catalog.register_input(self.file("source.txt"), "text")
        target = self.root / "new.txt"
        events = []
        sync_directory = catalog_module.fsync_directory
        link = os.link
        replace_file = os.replace

        def linking(temporary, destination):
            self.assertEqual(Path(temporary).read_text(), "complete text")
            self.assertEqual(stat.S_IMODE(Path(temporary).stat().st_mode), 0o600)
            events.append("link")
            return link(temporary, destination)

        def replacing(temporary, destination):
            self.assertEqual(Path(destination), self.history, "Save As must never use os.replace")
            events.append("history-replace")
            return replace_file(temporary, destination)

        def directory(path):
            events.append("history-directory" if path == self.history.parent else "text-directory")
            return sync_directory(path)

        with (
            mock.patch.object(catalog_module.os, "link", side_effect=linking),
            mock.patch.object(catalog_module.os, "replace", side_effect=replacing),
            mock.patch.object(catalog_module, "fsync_directory", side_effect=directory),
        ):
            self.catalog.save_text(source.id, source.revision, "complete text", target)
        self.assertEqual(events, ["link", "text-directory", "history-replace", "history-directory"])

    def test_history_directory_sync_failure_reports_published_history_without_rollback(self):
        self.catalog.register_input(self.file("source.txt"), "text")
        with (
            mock.patch.object(catalog_module, "fsync_directory", side_effect=OSError("directory failure")),
            self.assertRaisesRegex(CatalogError, "history published.*not rolled back"),
        ):
            self.catalog.register_input(self.file("second.txt"), "text")
        self.assertEqual(len(json.loads(self.history.read_text())["artifacts"]), 2)
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(len(ArtifactCatalog(self.history).entries), 2)

    def test_backup_directory_sync_failure_keeps_original_and_reports_backup_outcome(self):
        source = self.catalog.register_input(self.file("source.txt", "old"), "text")
        before = self.history.read_bytes()
        with (
            mock.patch.object(catalog_module, "fsync_directory", side_effect=OSError("directory failure")),
            self.assertRaisesRegex(CatalogError, "Original.*not replaced.*File published"),
        ):
            self.catalog.save_text(
                source.id, source.revision, "new", Path(source.path), replace_existing=True,
            )
        self.assertEqual(Path(source.path).read_text(), "old")
        backup, = self.root.glob("source.txt.backup-*")
        self.assertEqual(backup.read_text(), "old")
        self.assertEqual(self.history.read_bytes(), before)

    def test_text_directory_sync_failure_reports_publication_without_rollback(self):
        source = self.catalog.register_input(self.file("source.txt", "old"), "text")
        before = self.history.read_bytes()
        sync_directory = catalog_module.fsync_directory
        for replacing in (False, True):
            calls = 0
            target = Path(source.path) if replacing else self.root / "new.txt"

            def directory(path):
                nonlocal calls
                calls += 1
                if calls == (2 if replacing else 1):
                    raise OSError("directory failure")
                return sync_directory(path)

            with (
                mock.patch.object(catalog_module, "fsync_directory", side_effect=directory),
                self.assertRaisesRegex(CatalogError, "not rolled back"),
            ):
                self.catalog.save_text(
                    source.id, source.revision, "new", target, replace_existing=replacing,
                )
            self.assertEqual(target.read_text(), "new")
            self.assertEqual(self.history.read_bytes(), before)
        backup, = self.root.glob("source.txt.backup-*")
        self.assertEqual(backup.read_text(), "old")

    def test_text_publication_is_preserved_and_reported_when_history_write_fails(self):
        source = self.catalog.register_input(self.file("source.txt", "old"), "text")
        before = self.history.read_bytes()
        actual_replace = os.replace
        for replacing in (False, True):
            target = Path(source.path) if replacing else self.root / "new.txt"

            def fail_history(temporary, destination):
                if Path(destination) == self.history:
                    raise OSError("history unavailable")
                return actual_replace(temporary, destination)

            with (
                mock.patch.object(catalog_module.os, "replace", side_effect=fail_history),
                self.assertRaisesRegex(CatalogError, "Text saved.*history update failed"),
            ):
                self.catalog.save_text(
                    source.id, source.revision, "new", target, replace_existing=replacing,
                )
            self.assertEqual(target.read_text(), "new")
            self.assertEqual(self.history.read_bytes(), before)

    def test_stale_warning_does_not_propagate_through_generated_outputs(self):
        source = self.catalog.register_input(self.file("lyrics", "old"), "lyrics")
        output = self.file("score.abc", "X:1\nM:4/4\nL:1/32\nK:C\nV: Vocal\nV: Ins\n")
        first = self.result(
            output, kind="abc", tool="melisma",
            inputs=(OperationInput("lyrics", Path(source.path), "lyrics"),),
        )
        bound = self.catalog.begin(first.operation)
        self.catalog.register_result(replace(first, operation=bound), "first")
        self.catalog.end(bound)
        score = next(entry for entry in self.entries() if entry["path"] == str(output))
        second = Operation(
            "fixture", "consume-score", self.command, (),
            inputs=(OperationInput("abc", output, "abc"),),
        )
        second = self.catalog.begin(second)
        self.catalog.register_result(
            OperationResult(second, first.process, ()), "second",
        )
        self.catalog.end(second)
        self.catalog.save_text(
            source.id, source.revision, "edited", Path(source.path), replace_existing=True,
        )
        self.assertEqual(self.catalog.changed_inputs("first"), [source.id])
        self.assertFalse(self.catalog.changed_inputs("second"))
        self.assertEqual(self.catalog.get(score["id"]).revision, score["revision"])

    def test_audio_probe_evidence_is_not_reused_after_content_changes(self):
        path = self.file("vocal.wav")
        self.catalog.register_result(
            self.result(path, kind="audio", tool="openutau", name="render"), "vocal",
        )
        entry = self.entries()[0]
        self.catalog.refresh()
        self.assertTrue(self.catalog.get(entry["id"]).valid)
        path.write_text("different bytes")
        self.catalog.refresh()
        self.assertIsNone(self.catalog.get(entry["id"]).valid)

    def test_external_edit_refresh_and_missing_input_are_observed(self):
        source = self.catalog.register_input(self.file("lyrics"), "lyrics")
        self.consumer(source)
        Path(source.path).write_text("external edit")
        self.catalog.refresh()
        self.assertEqual(self.catalog.changed_inputs("consumer"), [source.id])
        Path(source.path).unlink()
        self.catalog.refresh()
        entry = self.catalog.get(source.id)
        self.assertFalse(entry.exists)
        self.assertFalse(entry.valid)
        self.assertFalse(self.catalog.compatible(("lyrics",)))

    def test_stale_revision_save_refuses_without_touching_files(self):
        source = self.catalog.register_input(self.file("lyrics", "old"), "lyrics")
        Path(source.path).write_text("external edit")
        for replacing in (False, True):
            with self.assertRaisesRegex(ValueError, "changed"):
                self.catalog.save_text(
                    source.id, source.revision, "new",
                    Path(source.path) if replacing else self.root / "copy",
                    replace_existing=replacing,
                )
        self.assertEqual(Path(source.path).read_text(), "external edit")
        self.assertFalse(list(self.root.glob("*.backup-*")))
        self.assertFalse((self.root / "copy").exists())

    def test_save_as_refuses_occupied_symlinks_and_original(self):
        source = self.catalog.register_input(self.file("lyrics"), "lyrics")
        occupied = self.file("occupied", "preserve")
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "absent")
        for target in (occupied, dangling, Path(source.path)):
            with self.assertRaises(ValueError):
                self.catalog.save_text(source.id, source.revision, "new", target)
        self.assertEqual(occupied.read_text(), "preserve")
        self.assertTrue(dangling.is_symlink())
        self.assertFalse((self.root / "absent").exists())

    def test_active_shared_inputs_remain_protected_until_all_consumers_finish(self):
        source = self.catalog.register_input(self.file("lyrics"), "lyrics")
        operation = Operation(
            "fixture", "consume", self.command, (),
            inputs=(OperationInput("lyrics", Path(source.path), "lyrics"),),
        )
        first = self.catalog.begin(operation)
        second = self.catalog.begin(operation)
        self.catalog.end(first)
        self.assertFalse(self.catalog.read_text(source.id)["editable"])
        with self.assertRaises(ValueError):
            self.catalog.save_text(
                source.id, source.revision, "edit", Path(source.path), replace_existing=True,
            )
        self.catalog.end(second)
        self.assertTrue(self.catalog.read_text(source.id)["editable"])

    def test_two_input_roles_for_one_path_share_one_artifact_identity(self):
        path = self.file("input")
        operation = Operation(
            "fixture", "consume", self.command, (),
            inputs=(OperationInput("first", path, "text"), OperationInput("second", path, "text")),
        )
        bound = self.catalog.begin(operation)
        self.assertEqual(bound.inputs[0].artifact_id, bound.inputs[1].artifact_id)
        self.assertEqual(len(self.entries()), 1)
        self.catalog.end(bound)
        self.assertFalse(self.catalog.active_paths)

    def test_run_eviction_restart_and_diagnostics_survive(self):
        registry = RunRegistry(max_runs=1, max_active=1, catalog=self.catalog)
        self.addCleanup(registry.close)
        adapter = ToolAdapter(self.command.executable, self.root)
        adapter.tool, adapter.operations = "fixture", ("write",)
        for index in range(2):
            path = self.root / f"out-{index}.txt"
            operation = adapter._operation(
                "write", ("-B", "-c", f"from pathlib import Path;Path({str(path)!r}).write_text('complete')"),
                (ExpectedArtifact(path),),
            )
            record = registry.start(adapter, operation)
            run = registry.get(record["id"])
            result = eventually(lambda: (current := run.snapshot())["complete"] and current)
            self.assertTrue(result["success"])
            self.assertIsNone(result["catalog_error"])
        self.assertEqual(len(registry.list()), 1)
        restored = ArtifactCatalog(self.history)
        self.assertEqual(len(restored.runs), 2)
        entries = restored.document()["artifacts"]
        self.assertEqual(len(entries), 4)
        diagnostics = [entry for entry in entries if entry["type"] == "diagnostic"]
        for entry in diagnostics:
            data = json.loads(Path(entry["path"]).read_text())
            self.assertIn("stdout_truncated", data["process"])
            self.assertNotIn("environment", data["command"])
            self.assertEqual(Path(entry["path"]).stat().st_mode & 0o777, 0o600)

    def test_catalog_finalization_failure_retains_process_result_with_explicit_error(self):
        registry = RunRegistry(catalog=self.catalog)
        self.addCleanup(registry.close)
        adapter = ToolAdapter(self.command.executable, self.root)
        adapter.tool, adapter.operations = "fixture", ("write",)
        operation = adapter._operation("write", ("-B", "-c", "print('tool diagnostic')"), ())
        with mock.patch.object(self.catalog, "register_result", side_effect=CatalogError("disk full")):
            record = registry.start(adapter, operation)
            run = registry.get(record["id"])
            result = eventually(lambda: (current := run.snapshot())["complete"] and current)
        self.assertTrue(result["success"])
        self.assertIn("tool diagnostic", result["process"]["stdout"])
        self.assertIn("disk full", result["catalog_error"])
        self.assertFalse(self.catalog.active_paths)


if __name__ == "__main__":
    unittest.main()
