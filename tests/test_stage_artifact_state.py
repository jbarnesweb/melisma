"""Issue #8: current execution-stage state is not artifact existence/history."""

import json
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration import execution
from tools.orchestration.adapters import MelismaAdapter
from tools.orchestration.artifact_catalog import ArtifactCatalog
from tools.orchestration.process import ProcessState
from test_execution import eventually


class StageArtifactStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.history = self.root / "state" / "artifacts.json"
        self.catalog = ArtifactCatalog(self.history)
        self.registry = execution.RunRegistry(catalog=self.catalog)
        self.addCleanup(self.registry.close)
        self.executable = self.root / "melisma-fixture"
        self.executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "behavior = pathlib.Path(sys.argv[-1]).read_text()\n"
            "if behavior == 'failed': sys.exit(7)\n"
            "if behavior == 'wait':\n"
            " print('fixture-ready',file=sys.stderr,flush=True)\n"
            " while True: time.sleep(.005)\n"
            "if behavior == 'invalid': print('not a Melisma projection')\n"
            "elif sys.argv[2] == 'abc':\n"
            " print('X:1\\nM:4/4\\nL:1/32\\nK:C\\nV: Vocal\\nV: Ins')\n"
            "else:\n"
            " print('ustx_version: 0.7\\nresolution: 480\\ntracks:\\nvoice_parts:')\n",
            encoding="utf-8",
        )
        self.executable.chmod(0o755)
        self.adapter = MelismaAdapter(self.executable, self.root)
        self.midi = self.root / "input.mid"
        self.midi.write_text("fixture", encoding="utf-8")
        self.first_operation = self.operation("earlier")
        self.first = self.start(self.first_operation)
        self.assertTrue(self.completed(self.first)["success"])
        self.earlier, = (
            entry for entry in self.catalog.entries.values()
            if entry.producer_run == self.first.id and entry.type == "abc"
        )
        self.stage = self.earlier.stage
        self.assertEqual(self.stage, "melisma.abc")

    def operation(self, name, behavior="success", mode="abc"):
        lyrics = self.root / f"{name}-lyrics.txt"
        lyrics.write_text(behavior, encoding="utf-8")
        return self.adapter.build(
            mode, self.midi, lyrics, self.root / f"{name}.{mode}", publish_stdout=True,
        )

    def start(self, operation, adapter=None):
        record = self.registry.start(adapter or self.adapter, operation)
        return self.registry.get(record["id"])

    def completed(self, run):
        return eventually(lambda: (record := run.snapshot())["complete"] and record)

    def current(self, registry=None):
        return (registry or self.registry).stage_snapshot(
            self.stage, idle_state=execution.StageState.NOT_CONFIGURED,
        )

    def assert_retained(self, document):
        entry, = (item for item in document["artifacts"] if item["id"] == self.earlier.id)
        self.assertEqual(
            json.loads(json.dumps(entry)), json.loads(json.dumps(asdict(self.earlier))),
        )
        self.assertTrue(entry["exists"])
        self.assertTrue(entry["valid"])
        self.assertEqual(entry["production_status"], "successful")
        self.assertEqual(entry["producer_run"], self.first.id)
        self.assertTrue(Path(entry["path"]).is_file())
        self.assertEqual(self.catalog.get(self.earlier.id), self.earlier)

    def test_success_with_artifact_is_distinct_from_later_failed_attempt(self):
        successful = self.current()
        self.assertEqual(successful["state"], "success")
        self.assertEqual(successful["run_id"], self.first.id)
        self.assert_retained(successful)

        failed = self.start(self.operation("retry", "failed"))
        attempt = self.completed(failed)
        current = self.current()
        self.assertEqual(attempt["stage_state"], "failed")
        self.assertEqual(current["state"], "failed")
        self.assertEqual(current["run_id"], failed.id)
        self.assertEqual(self.first.snapshot()["stage_state"], "success")
        self.assert_retained(current)
        missing, = (item for item in current["artifacts"] if item["producer_run"] == failed.id)
        self.assertFalse(missing["exists"])
        self.assertEqual(missing["production_status"], "missing")
        self.assertEqual(self.catalog.read_text(self.earlier.id)["text"], Path(self.earlier.path).read_text())

    def test_running_attempt_does_not_clear_previously_produced_artifact(self):
        run = self.start(self.operation("running", "wait"))
        eventually(lambda: "fixture-ready" in run.snapshot()["process"]["stderr"])
        self.assertEqual(run.snapshot()["stage_state"], "running")
        current = self.current()
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["run_id"], run.id)
        self.assert_retained(current)
        self.assertEqual([item["id"] for item in current["artifacts"]], [self.earlier.id])
        self.assertEqual(run.snapshot()["artifacts"], [])

    def test_stopped_attempt_retains_earlier_artifact_and_historical_success(self):
        run = self.start(self.operation("stopped", "wait"))
        eventually(lambda: "fixture-ready" in run.snapshot()["process"]["stderr"])
        run.stop()
        record = self.completed(run)
        self.assertTrue(record["stop_requested"])
        self.assertFalse(record["success"])
        self.assertEqual(record["stage_state"], "stopped")
        self.assertEqual(self.current()["state"], "stopped")
        self.assert_retained(self.current())
        self.assertTrue(self.catalog.runs[self.first.id].success)

    def test_explicit_current_states_preserve_existing_history_without_writing(self):
        before = self.history.read_bytes()
        for state in (
            execution.StageState.DISABLED, execution.StageState.NOT_CONFIGURED,
            execution.StageState.READY, execution.StageState.RUNNING,
            execution.StageState.SUCCESS, execution.StageState.FAILED,
            execution.StageState.STOPPED, execution.StageState.STALE,
        ):
            with self.subTest(state=state):
                document = execution.stage_document(self.stage, state, catalog=self.catalog)
                self.assertEqual(document["state"], state.value)
                self.assertIsNone(document["run_id"])
                self.assert_retained(document)
                self.assertEqual(self.history.read_bytes(), before)

    def test_json_keeps_current_failure_separate_from_prior_artifact_production(self):
        run = self.start(self.operation("failed-json", "failed"))
        self.completed(run)
        document = json.loads(json.dumps(self.current()))
        self.assertEqual(document["stage"], "melisma.abc")
        self.assertEqual(document["state"], "failed")
        self.assert_retained(document)
        persisted = json.loads(self.history.read_text())
        self.assertEqual(persisted["schema_version"], 1)
        self.assertEqual(set(persisted), {"schema_version", "artifacts", "runs"})
        saved, = (item for item in persisted["artifacts"] if item["id"] == self.earlier.id)
        self.assertEqual(saved["production_status"], "successful")
        self.assertTrue(saved["exists"])

    def test_ready_after_session_restart_can_coexist_with_persisted_artifact(self):
        catalog = ArtifactCatalog(self.history)
        registry = execution.RunRegistry(catalog=catalog)
        self.addCleanup(registry.close)
        before = self.history.read_bytes()
        document = registry.stage_snapshot(self.stage, idle_state=execution.StageState.READY)
        self.assertEqual(document["state"], "ready")
        self.assertIsNone(document["run_id"])
        self.assert_retained(document)
        self.assertEqual(self.history.read_bytes(), before)

    def test_success_does_not_claim_that_a_missing_artifact_still_exists(self):
        Path(self.earlier.path).unlink()
        self.catalog.refresh()
        document = self.current()
        self.assertEqual(document["state"], "success")
        entry, = document["artifacts"]
        self.assertEqual(entry["id"], self.earlier.id)
        self.assertFalse(entry["exists"])
        self.assertEqual(entry["production_status"], "successful")
        self.assertEqual(entry["producer_run"], self.first.id)

    def test_validation_failure_is_failed_even_when_process_exits_zero(self):
        run = self.start(self.operation("invalid", "invalid"))
        record = self.completed(run)
        self.assertTrue(record["process"]["success"])
        self.assertFalse(record["success"])
        self.assertEqual(record["stage_state"], "failed")
        self.assertEqual(self.current()["state"], "failed")
        self.assert_retained(self.current())
        partial, = (item for item in self.current()["artifacts"] if item["producer_run"] == run.id)
        self.assertTrue(partial["exists"])
        self.assertFalse(partial["valid"])
        self.assertEqual(partial["production_status"], "partial")

    def test_validation_remains_running_with_earlier_artifact_retained(self):
        entered, release = threading.Event(), threading.Event()

        class ValidatingMelisma(MelismaAdapter):
            def complete(self, result, runner):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture validation deadline")
                return result

        adapter = ValidatingMelisma(self.executable, self.root)
        run = self.start(self.operation("validating"), adapter)
        try:
            self.assertTrue(entered.wait(5))
            record = run.snapshot()
            self.assertEqual(record["phase"], "validating")
            self.assertTrue(record["process"]["success"])
            self.assertFalse(record["complete"])
            self.assertEqual(record["stage_state"], "running")
            self.assertEqual(self.current()["state"], "running")
            self.assert_retained(self.current())
        finally:
            release.set()
        self.assertEqual(self.completed(run)["stage_state"], "success")

    def test_start_failure_does_not_replace_or_hide_previous_output(self):
        adapter = MelismaAdapter(self.root / "missing-executable", self.root)
        operation = self.operation("cannot-start")
        operation = replace(operation, command=replace(
            operation.command, executable=adapter.executable,
        ))
        run = self.start(operation, adapter)
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], "start-failed")
        self.assertEqual(record["stage_state"], "failed")
        self.assert_retained(self.current())

    def test_rejected_occupied_output_does_not_relabel_earlier_artifact_as_failed(self):
        run = self.start(self.first_operation)
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], "rejected")
        self.assertEqual(record["stage_state"], "failed")
        self.assertEqual(self.current()["state"], "failed")
        self.assert_retained(self.current())

    def test_cancelled_before_spawn_is_stopped_without_losing_prior_artifact(self):
        entered, release = threading.Event(), threading.Event()

        class GatedMelisma(MelismaAdapter):
            def execute(self, operation, runner):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture release deadline")
                return super().execute(operation, runner)

        adapter = GatedMelisma(self.executable, self.root)
        run = self.start(self.operation("cancelled"), adapter)
        try:
            self.assertTrue(entered.wait(5))
            run.stop()
        finally:
            release.set()
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], ProcessState.CANCELLED)
        self.assertEqual(record["stage_state"], "stopped")
        self.assert_retained(self.current())

    def test_stage_history_excludes_other_operations_and_imported_artifacts(self):
        run = self.start(self.operation("other", mode="ustx"))
        self.assertTrue(self.completed(run)["success"])
        imported = self.root / "imported.abc"
        imported.write_text(Path(self.earlier.path).read_text())
        entry = self.catalog.register_input(imported, "abc")
        self.assertIsNone(entry.stage)
        document = self.current()
        self.assertEqual(document["state"], "success")
        self.assertEqual(document["run_id"], self.first.id)
        self.assertEqual([item["id"] for item in document["artifacts"]], [self.earlier.id])
        self.assert_retained(document)

    def test_stage_without_catalog_does_not_infer_tracked_artifacts_from_success(self):
        registry = execution.RunRegistry()
        self.addCleanup(registry.close)
        operation = self.operation("uncatalogued")
        run = registry.get(registry.start(self.adapter, operation)["id"])
        self.assertTrue(self.completed(run)["success"])
        document = registry.stage_snapshot(self.stage, idle_state=execution.StageState.READY)
        self.assertEqual(document["state"], "success")
        self.assertEqual(document["artifacts"], [])
        self.assertTrue(operation.expected[0].path.exists())


if __name__ == "__main__":
    unittest.main()
