"""Live loopback execution trust boundary using a tiny temporary Melisma stand-in."""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration import Configuration, DiscoverySource
from tools.orchestration.process import OUTPUT_LIMIT
from tools.web_ui.server import WorkflowHandler, create_server
from test_execution import eventually


class WebExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / "melisma-fixture"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "print('<diagnostic>&',file=sys.stderr,flush=True)\n"
            "if pathlib.Path(sys.argv[-1]).read_text() == 'wait':\n"
            " print('incremental',flush=True)\n"
            " while not pathlib.Path('release').exists(): time.sleep(.005)\n"
            "if sys.argv[2] == 'abc':\n"
            " print('X:1\\nM:4/4\\nL:1/32\\nK:C\\nV: Vocal\\nV: Ins')\n"
            "else:\n"
            " print('ustx_version: 0.7\\nresolution: 480\\ntracks:\\nvoice_parts:')\n"
            "if pathlib.Path(sys.argv[-1]).read_text() == 'large':\n"
            " sys.stdout.flush()\n"
            f" sys.stdout.buffer.write(b'\\n%' + b'x'*{OUTPUT_LIMIT + 17} + b'\\n% complete tail\\n')\n"
        )
        executable.chmod(0o755)
        self.midi = self.root / "input with spaces.mid"
        self.lyrics = self.root / "lyrics.txt"
        self.midi.write_text("fixture")
        self.lyrics.write_text("lyrics")
        config = Configuration(sources=(DiscoverySource("melisma", self.root, executable),))
        self.server = create_server(
            0, configuration=config, artifact_history=self.root / "history.json",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.logging = mock.patch.object(WorkflowHandler, "log_message")
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()

    def request(self, method, path, value=None, *, authorize=True):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        headers = {"Content-Type": "application/json"}
        if authorize:
            headers["X-UI-CSRF"] = self.server.csrf
        try:
            connection.request(method, path, json.dumps(value) if value is not None else None, headers)
            response = connection.getresponse()
            body = response.read()
            return response.status, json.loads(body) if response.getheader(
                "Content-Type", "",
            ).startswith("application/json") else body.decode()
        finally:
            connection.close()

    def preview(self, mode="abc", **updates):
        inputs = {"midi": str(self.midi), "lyrics": str(self.lyrics), f"{mode}_output": str(self.root / f"new.{mode}")}
        inputs.update(updates)
        return self.request("POST", "/api/operations/preview", {"workflow": f"generate-{mode}", "inputs": inputs})

    def test_preview_and_execution_exact_operation_artifact_and_safe_json(self):
        status, preview = self.preview()
        self.assertEqual(status, 200)
        self.assertFalse((self.root / "new.abc").exists())
        self.assertNotIn(str(self.root / "new.abc"), preview["command"]["arguments"])
        status, record = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        self.assertEqual(status, 202)
        path = f"/api/runs/{record['id']}"
        result = eventually(lambda: (current := self.request("GET", path)[1])["complete"] and current)
        self.assertEqual(result["command"], preview["command"])
        self.assertTrue(result["success"])
        self.assertEqual(result["process"]["stderr"], "<diagnostic>&\n")
        self.assertTrue(result["artifacts"][0]["valid"])
        self.assertEqual((self.root / "new.abc").read_text(), result["process"]["stdout"])
        self.assertEqual(self.request("GET", "/api/runs")[1]["runs"][0]["id"], result["id"])
        self.assertEqual(self.request("POST", path + "/stop", {})[1], result)
        self.assertEqual(self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})[0], 404)

    def test_ustx_metadata_is_literal_and_invalid_inputs_never_spawn(self):
        status, preview = self.preview(
            "ustx", singer="<b>$(touch injected)</b>", renderer="manual", phonemizer="",
        )
        self.assertEqual(status, 200)
        self.assertIn("<b>$(touch injected)</b>", preview["command"]["arguments"])
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            for updates in (
                {"midi": "relative.mid"}, {"lyrics": str(self.root / "missing")},
                {"ustx_output": str(self.lyrics)}, {"arguments": ["sh"]},
                {"cwd": "/"}, {"singer": "-invalid"}, {"midi": None},
            ):
                with self.subTest(updates=updates):
                    self.assertEqual(self.preview("ustx", **updates)[0], 422)
            for value in (
                {"command": "sh -c bad"},
                {"executable": "/bin/sh", "arguments": ["-c", "bad"]},
                {"workflow": "full-pipeline", "inputs": {}},
                {"workflow": "render-vocals", "inputs": {}},
                {"workflow": ["generate-abc"], "inputs": {}},
            ):
                self.assertEqual(self.request("POST", "/api/operations/preview", value)[0], 422)
            self.assertEqual(self.request("POST", "/api/runs", {"argv": ["/bin/sh"]})[0], 422)

    def test_large_artifact_survives_http_console_truncation(self):
        self.lyrics.write_text("large")
        status, preview = self.preview()
        self.assertEqual(status, 200)
        status, run = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        self.assertEqual(status, 202)
        path = f"/api/runs/{run['id']}"
        record = eventually(lambda: (current := self.request("GET", path)[1])["complete"] and current)
        data = (self.root / "new.abc").read_bytes()
        self.assertTrue(record["success"], record["errors"])
        self.assertTrue(record["artifacts"][0]["valid"])
        self.assertGreater(len(data), OUTPUT_LIMIT)
        self.assertEqual(record["artifacts"][0]["size"], len(data))
        self.assertTrue(data.endswith(b"\n% complete tail\n"))
        self.assertTrue(record["process"]["stdout_truncated"])
        self.assertTrue(record["process"]["stdout_complete"])
        self.assertEqual(record["process"]["stdout_bytes"], len(data))
        self.assertEqual(record["process"]["stdout"].encode(), data[:OUTPUT_LIMIT])
        self.assertEqual(record["process"]["stderr"], "<diagnostic>&\n")

    def test_csrf_unknown_ids_and_stop_payload(self):
        self.assertEqual(self.request("POST", "/api/operations/preview", {}, authorize=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/runs", {}, authorize=False)[0], 403)
        path = "/api/runs/" + "0" * 32
        self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("POST", path + "/stop", {})[0], 404)
        self.assertEqual(self.request("POST", path + "/stop", {"pid": 1})[0], 422)

    def test_live_polling_stop_and_retained_output(self):
        self.lyrics.write_text("wait")
        _, preview = self.preview()
        _, record = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        path = f"/api/runs/{record['id']}"
        live = eventually(lambda: (current := self.request("GET", path)[1])["process"]["stdout"]
                          and current["process"]["stderr"] and current)
        self.assertFalse(live["complete"])
        self.assertEqual(live["process"]["stderr"], "<diagnostic>&\n")
        self.assertEqual(self.request("POST", path + "/stop", {})[0], 200)
        result = eventually(lambda: (current := self.request("GET", path)[1])["complete"] and current)
        self.assertTrue(result["stop_requested"])
        self.assertIn("incremental", result["process"]["stdout"])
        self.assertFalse(result["success"])
        self.assertFalse((self.root / "new.abc").exists())
        self.assertEqual(self.request("POST", path + "/stop", {})[1], result)

    def test_configuration_preview_is_not_execution_authorization(self):
        from tools.web_ui.execution import ExecutionAPI
        api = ExecutionAPI(Configuration())
        with self.assertRaisesRegex(ValueError, "configured local"):
            api.preview({
                "workflow": "generate-abc",
                "inputs": {"midi": str(self.midi), "lyrics": str(self.lyrics), "abc_output": str(self.root / "out")},
            })

    def test_preview_retention_and_input_recheck(self):
        previews = [self.preview()[1]["preview_id"] for _ in range(33)]
        self.assertEqual(len(self.server.execution.previews), 32)
        self.assertEqual(self.request("POST", "/api/runs", {"preview_id": previews[0]})[0], 404)
        self.lyrics.unlink()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            self.assertEqual(self.request("POST", "/api/runs", {"preview_id": previews[-1]})[0], 422)

    def test_preview_run_and_validation_documents_omit_environment_values(self):
        from dataclasses import replace
        from tools.orchestration.process import Command
        secret = "synthetic-private-override-value"
        construct = self.server.execution.construct
        def with_environment(draft):
            adapter, operation = construct(draft)
            operation = replace(operation, command=replace(
                operation.command, environment=(("PRIVATE_SETTING", secret),),
            ))
            def complete(result, runner):
                validation = runner.run(Command(
                    Path(sys.executable).resolve(), ("-B", "-c", "print('check')"),
                    self.root, (("PRIVATE_CHECK", secret),),
                ))
                return replace(result, validation_processes=(validation,))
            adapter.complete = complete
            return adapter, operation
        with mock.patch.object(self.server.execution, "construct", side_effect=with_environment):
            _, preview = self.preview()
        self.assertNotIn(secret, json.dumps(preview))
        self.assertNotIn("environment", preview["command"])
        _, run = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        self.assertNotIn(secret, json.dumps(run))
        path = f"/api/runs/{run['id']}"
        result = eventually(lambda: (current := self.request("GET", path)[1])["complete"] and current)
        self.assertTrue(result["success"])
        self.assertTrue(result["validation_processes"])
        for record in (result, self.request("GET", "/api/runs")[1]):
            self.assertNotIn(secret, json.dumps(record))
        self.assertNotIn("environment", result["validation_processes"][0]["command"])

    def register(self, path, type_):
        status, entry = self.request(
            "POST", "/api/artifacts/register", {"path": str(path), "type": type_},
        )
        self.assertEqual(status, 200, entry)
        return entry

    def test_artifact_registration_compatibility_and_read_only_inspection(self):
        lyrics = self.register(self.lyrics, "lyrics")
        self.register(self.midi, "midi")
        before = (self.root / "history.json").read_bytes()
        status, view = self.request("GET", f"/api/artifacts/{lyrics['id']}/text")
        self.assertEqual(status, 200)
        self.assertEqual(view["text"], "lyrics")
        self.assertEqual((self.root / "history.json").read_bytes(), before)
        status, choices = self.request(
            "POST", "/api/artifacts/compatible", {"workflow": "generate-ustx", "field": "lyrics"},
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in choices["artifacts"]], [lyrics["id"]])
        self.assertEqual(choices["readiness"], "unknown")
        self.assertEqual(self.request("GET", "/api/artifacts")[0], 200)
        self.assertEqual(self.request("GET", f"/api/artifacts/{'a' * 32}/text")[0], 404)
        self.assertEqual(self.request("GET", "/api/artifacts/../../etc/passwd/text")[0], 404)

    def test_artifact_writes_require_existing_ui_authorization_and_strict_fields(self):
        for path, draft in (
            ("/api/artifacts/register", {"path": str(self.lyrics), "type": "lyrics"}),
            ("/api/artifacts/refresh", {}),
            ("/api/artifacts/compatible", {"workflow": "generate-abc", "field": "midi"}),
            (f"/api/artifacts/{'a' * 32}/save", {}),
        ):
            self.assertEqual(self.request("POST", path, draft, authorize=False)[0], 403)
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            for draft in (
                {"path": "relative", "type": "lyrics"},
                {"path": str(self.lyrics), "type": "instrumental-audio"},
                {"path": str(self.lyrics), "type": "lyrics", "command": "anything"},
                {"path": [], "type": "lyrics"},
            ):
                self.assertEqual(self.request("POST", "/api/artifacts/register", draft)[0], 422)
            self.assertEqual(self.request(
                "POST", "/api/artifacts/compatible", {"workflow": "generate-midi", "field": "midi"},
            )[0], 422)

    def test_preview_revision_changed_before_start_never_executes(self):
        _, preview = self.preview()
        self.lyrics.write_text("different lyrics")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            status, error = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        self.assertEqual(status, 422)
        self.assertIn("changed since preview", error["error"])
        self.assertFalse(self.server.execution.registry.list())

    def test_registered_missing_save_as_rejection_preserves_selection_binding_and_stale_identity(self):
        source = self.register(self.lyrics, "lyrics")
        missing = self.root / "previous-lyrics.txt"
        missing.write_text("previous lyrics")
        previous = self.register(missing, "lyrics")
        missing.unlink()
        status, error = self.request("POST", f"/api/artifacts/{source['id']}/save", {
            "revision": source["revision"], "text": "different lyrics",
            "destination": str(missing), "replace": False,
        })
        self.assertEqual(status, 422)
        self.assertIn("already registered", error["error"])
        self.assertFalse(missing.exists())
        _, choices = self.request("POST", "/api/artifacts/compatible", {
            "workflow": "generate-abc", "field": "lyrics",
        })
        selected, = choices["artifacts"]
        self.assertEqual(selected["id"], source["id"])
        _, preview = self.preview(lyrics=selected["path"])
        preview_input = next(item for item in preview["inputs"] if item["role"] == "lyrics")
        self.assertEqual(preview_input["artifact_id"], selected["id"])
        _, record = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        run_path = f"/api/runs/{record['id']}"
        historical = eventually(lambda: (value := self.request("GET", run_path)[1])["complete"] and value)
        self.assertTrue(historical["success"])
        consumed = next(item for item in historical["inputs"] if item["role"] == "lyrics")
        self.assertEqual(consumed["artifact_id"], selected["id"])
        status, edited = self.request("POST", f"/api/artifacts/{selected['id']}/save", {
            "revision": selected["revision"], "text": "edited lyrics",
            "destination": selected["path"], "replace": True,
        })
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["id"], selected["id"])
        final = self.request("GET", run_path)[1]
        self.assertEqual(final["stale_inputs"], [selected["id"]])
        self.assertTrue(final["success"])
        self.assertEqual(final["process"], historical["process"])
        history = self.request("GET", "/api/artifacts")[1]
        paths = [str(Path(item["path"]).resolve()) for item in history["artifacts"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(next(item for item in history["artifacts"] if item["id"] == previous["id"])["path"], str(missing))

    def test_edit_save_as_and_replacement_preserve_historical_result(self):
        lyrics = self.register(self.lyrics, "lyrics")
        _, preview = self.preview()
        _, record = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        path = f"/api/runs/{record['id']}"
        historical = eventually(lambda: (current := self.request("GET", path)[1])["complete"] and current)
        self.assertTrue(historical["success"])
        self.assertIsNone(historical["catalog_error"])
        self.assertEqual(historical["inputs"][1]["artifact_id"], lyrics["id"])
        draft = {
            "revision": lyrics["revision"], "text": "derived lyrics",
            "destination": str(self.root / "derived.txt"), "replace": False,
        }
        status, copy = self.request("POST", f"/api/artifacts/{lyrics['id']}/save", draft)
        self.assertEqual(status, 200, copy)
        self.assertNotEqual(copy["id"], lyrics["id"])
        self.assertEqual(self.lyrics.read_text(), "lyrics")
        self.assertFalse(self.request("GET", path)[1]["stale_inputs"])
        draft.update(destination=str(self.lyrics), replace=True)
        status, updated = self.request("POST", f"/api/artifacts/{lyrics['id']}/save", draft)
        self.assertEqual(status, 200, updated)
        self.assertEqual(updated["id"], lyrics["id"])
        final = self.request("GET", path)[1]
        self.assertTrue(final["success"])
        self.assertEqual(final["process"], historical["process"])
        self.assertEqual(final["stale_inputs"], [lyrics["id"]])
        backup, = self.root.glob("lyrics.txt.backup-*")
        self.assertEqual(backup.read_text(), "lyrics")
        self.assertEqual(self.request(
            "POST", f"/api/artifacts/{lyrics['id']}/save", draft,
        )[0], 422)

    def test_artifact_history_survives_server_restart_without_run_registry(self):
        _, preview = self.preview()
        _, record = self.request("POST", "/api/runs", {"preview_id": preview["preview_id"]})
        eventually(lambda: self.request("GET", f"/api/runs/{record['id']}")[1]["complete"])
        _, before = self.request("GET", "/api/artifacts")
        configuration = self.server.configuration
        self.close()
        self.server = create_server(
            0, configuration=configuration, artifact_history=self.root / "history.json",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        _, after = self.request("GET", "/api/artifacts")
        self.assertEqual(before["artifacts"], after["artifacts"])
        self.assertEqual(before["consumers"], after["consumers"])
        self.assertNotEqual(before["session_id"], after["session_id"])
        self.assertFalse(self.request("GET", "/api/runs")[1]["runs"])
        self.assertEqual(len([entry for entry in after["artifacts"] if entry["type"] == "diagnostic"]), 1)
        self.assertTrue((self.root / "new.abc").is_file())

    def test_truncated_inspection_and_read_only_diagnostics_cannot_be_saved(self):
        from tools.orchestration.artifact_catalog import TEXT_LIMIT
        self.lyrics.write_text("x" * (TEXT_LIMIT + 1))
        entry = self.register(self.lyrics, "lyrics")
        _, view = self.request("GET", f"/api/artifacts/{entry['id']}/text")
        self.assertTrue(view["truncated"])
        self.assertFalse(view["editable"])
        status, _ = self.request("POST", f"/api/artifacts/{entry['id']}/save", {
            "revision": entry["revision"], "text": view["text"],
            "destination": str(self.root / "never-write.txt"), "replace": False,
        })
        self.assertEqual(status, 422)
        self.assertFalse((self.root / "never-write.txt").exists())

    def test_catalog_failures_are_visible_not_empty_success(self):
        from tools.orchestration.artifact_catalog import CatalogError
        with mock.patch.object(
            self.server.catalog, "register_input", side_effect=CatalogError("write failure"),
        ):
            status, error = self.request("POST", "/api/artifacts/register", {
                "path": str(self.lyrics), "type": "lyrics",
            })
        self.assertEqual(status, 500)
        self.assertIn("write failure", error["error"])
        self.assertEqual(error["category"], "artifact-history")


if __name__ == "__main__":
    unittest.main()
