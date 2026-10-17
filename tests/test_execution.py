"""Deterministic local child fixtures for Issue #6; no tools/models/network."""

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import MelismaAdapter, ToolAdapter
from tools.orchestration.execution import (
    RegistryFull, RunRegistry, command_document, operation_document, process_document,
)
from tools.orchestration.process import (
    CapturedStream, Command, ExpectedArtifact, OwnedChild, ProcessRunner, ProcessState,
)


def eventually(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        threading.Event().wait(0.005)
    raise AssertionError("Fixture protocol deadline exceeded")


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python = Path(sys.executable).resolve()
        self.registry = RunRegistry()
        self.addCleanup(self.registry.close)
        self.adapter = ToolAdapter(self.python, self.root)
        self.adapter.tool, self.adapter.operations = "fixture", ("test",)

    def command(self, code, *args):
        return Command(self.python, ("-B", "-u", "-c", code, *args), self.root)

    def operation(self, code, *, expected=()):
        return self.adapter._operation("test", self.command(code).arguments, expected)

    def start(self, code, *, expected=()):
        record = self.registry.start(self.adapter, self.operation(code, expected=expected))
        return self.registry.get(record["id"])

    def completed(self, run):
        return eventually(lambda: (record := run.snapshot())["complete"] and record)

    def gate_code(self):
        return (
            "import pathlib, sys, time; "
            "print('<stdout>&', flush=True); print('<stderr>&', file=sys.stderr, flush=True)\n"
            "while not pathlib.Path('release').exists(): time.sleep(.005)\n"
            "print('complete', flush=True)"
        )

    def test_display_and_execution_use_exact_vector_not_shell(self):
        arguments = ("a b", "'single'", '"double"', "", "; touch injected", "$(touch injected)", "*")
        command = self.command("import json,sys;print(json.dumps(sys.argv[1:]))", *arguments)
        self.assertEqual(shlex.split(command.display), list(command.argv))
        original = subprocess.Popen
        with mock.patch("tools.orchestration.process.subprocess.Popen", wraps=original) as popen:
            result = ProcessRunner().run(command)
        self.assertEqual(json.loads(result.stdout), list(arguments))
        self.assertEqual(popen.call_args.args[0], command.argv)
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertFalse((self.root / "injected").exists())

    def test_absolute_paths_required(self):
        for executable, cwd in ((Path("python"), self.root), (self.python, Path("."))):
            with self.assertRaises(ValueError):
                Command(executable, (), cwd)

    def test_incremental_output_lifecycle_and_utc_times(self):
        run = self.start(self.gate_code())
        live = eventually(lambda: (record := run.snapshot())["process"]["stderr"]
                          and record["process"]["stdout"] and record)
        self.assertFalse(live["complete"])
        self.assertEqual(live["process"]["state"], "running")
        self.assertEqual(live["process"]["stdout"], "<stdout>&\n")
        self.assertEqual(live["process"]["stderr"], "<stderr>&\n")
        pid = live["process"]["pid"]
        (self.root / "release").touch()
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], "exited")
        self.assertTrue(record["success"])
        self.assertIn("complete", record["process"]["stdout"])
        self.assertEqual(record["id"], live["id"])
        self.assertFalse(record["can_stop"])
        for key in ("submitted_at", "started_at", "completed_at"):
            self.assertIsNotNone(datetime.fromisoformat(record["process"][key]).utcoffset())
        self.assertGreaterEqual(record["duration"], 0)
        self.assertGreaterEqual(record["process"]["duration"], 0)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_nonzero_signal_and_start_failure_keep_diagnostics(self):
        run = self.start("import sys;print('out');print('err',file=sys.stderr);sys.exit(7)")
        result = self.completed(run)["process"]
        self.assertEqual((result["exit_status"], result["stdout"], result["stderr"]), (7, "out\n", "err\n"))
        run = self.start("import os,signal;os.kill(os.getpid(),signal.SIGTERM)")
        result = self.completed(run)["process"]
        self.assertEqual((result["state"], result["signal"]), ("signalled", signal.SIGTERM))
        adapter = ToolAdapter(self.root / "missing-executable", self.root)
        adapter.tool, adapter.operations = "fixture", ("test",)
        operation = adapter._operation("test", (), ())
        run = self.registry.get(self.registry.start(adapter, operation)["id"])
        result = self.completed(run)["process"]
        self.assertEqual(result["state"], "start-failed")
        self.assertIn("No such file or directory", result["error"])
        self.assertIn("missing-executable", result["error"])
        self.assertIsNone(result["started_at"])

    def test_rejected_existing_artifact_retains_error_and_artifact(self):
        path = self.root / "preserved"
        path.write_text("keep")
        run = self.start("raise Exception('must not execute')", expected=(ExpectedArtifact(path),))
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], "rejected")
        self.assertIn("refusing existing outputs", record["process"]["error"])
        self.assertEqual(record["artifacts"][0]["size"], 4)
        self.assertFalse(record["success"])
        self.assertEqual(path.read_text(), "keep")

    def test_missing_artifact_errors_do_not_hide_process_streams(self):
        run = self.start(
            "import sys; print('primary'); print('diagnostic',file=sys.stderr); sys.exit(3)",
            expected=(ExpectedArtifact(self.root / "missing", "abc"),),
        )
        record = self.completed(run)
        self.assertEqual(record["process"]["stdout"], "primary\n")
        self.assertEqual(record["process"]["stderr"], "diagnostic\n")
        self.assertTrue(record["errors"])
        self.assertFalse(record["artifacts"][0]["valid"])
        self.assertEqual(record["artifacts"][0]["kind"], "abc")
        self.assertIsNotNone(record["artifacts"][0]["error"])
        json.dumps(record)

    def test_stop_exact_child_graceful_repeated_and_reaped(self):
        first = self.start(self.gate_code())
        second = self.start(self.gate_code())
        eventually(lambda: first.snapshot()["process"]["stdout"])
        eventually(lambda: second.snapshot()["process"]["stdout"])
        first.stop()
        result = self.completed(first)
        self.assertTrue(result["stop_requested"])
        self.assertTrue(result["process"]["stop_requested"])
        self.assertFalse(result["process"]["escalated"])
        self.assertFalse(result["success"])
        self.assertEqual(first.stop(), result)
        self.assertFalse(second.snapshot()["complete"])
        with self.assertRaises(ChildProcessError):
            os.waitpid(result["process"]["pid"], os.WNOHANG)
        second.stop()
        self.completed(second)

    def test_forced_escalation_with_ready_handshake(self):
        code = (
            "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready')\n"
            "while True: time.sleep(1)"
        )
        with OwnedChild(self.command(code)) as child:
            eventually(lambda: child.snapshot().stdout == "ready\n")
            result = child.stop(timeout=0.05)
            self.assertTrue(result.stop_requested)
            self.assertTrue(result.escalated)
            self.assertEqual(result.signal, signal.SIGKILL)
            self.assertIs(child.stop(), result)

    def test_zero_exit_after_stop_keeps_intent(self):
        code = (
            "import signal,sys,time;signal.signal(signal.SIGTERM,lambda *_:sys.exit(0));print('ready')\n"
            "while True: time.sleep(1)"
        )
        run = self.start(code)
        eventually(lambda: run.snapshot()["process"]["stdout"])
        run.stop()
        record = self.completed(run)
        self.assertEqual(record["process"]["state"], "exited")
        self.assertEqual(record["process"]["exit_status"], 0)
        self.assertTrue(record["stop_requested"])
        self.assertFalse(record["success"])

    def test_bounds_output_and_session_eviction(self):
        registry = RunRegistry(max_runs=2, max_active=1, output_limit=64)
        self.addCleanup(registry.close)
        ids = []
        for _ in range(3):
            run = registry.get(registry.start(
                self.adapter, self.operation("import sys;print('o'*200);print('e'*300,file=sys.stderr)")
            )["id"])
            ids.append(run.id)
            record = self.completed(run)
            for stream, length in (("stdout", 201), ("stderr", 301)):
                self.assertEqual(len(record["process"][stream]), 64)
                self.assertTrue(record["process"][f"{stream}_truncated"])
                self.assertEqual(record["process"][f"{stream}_bytes"], length)
        self.assertEqual([run["id"] for run in registry.list()], ids[1:])
        with self.assertRaises(KeyError):
            registry.get(ids[0])
        active = registry.start(self.adapter, self.operation(self.gate_code()))
        with self.assertRaises(RegistryFull):
            registry.start(self.adapter, self.operation("pass"))
        registry.close()
        self.assertTrue(registry.get(active["id"]).snapshot()["complete"])
        with self.assertRaises(RegistryFull):
            registry.start(self.adapter, self.operation("pass"))

    def test_concurrent_admission_reserves_output(self):
        path = self.root / "reserved"
        operation = self.operation(self.gate_code(), expected=(ExpectedArtifact(path),))
        first = self.registry.start(self.adapter, operation)
        with self.assertRaises(RegistryFull):
            self.registry.start(self.adapter, operation)
        self.registry.get(first["id"]).stop()

    def test_validation_records_and_metadata_preserve_primary(self):
        parent = self
        class Validating(ToolAdapter):
            tool, operations = "fixture", ("test",)
            def complete(self, result, runner):
                from dataclasses import replace
                validation = runner.run(parent.command("import sys;print('check');sys.exit(9)"))
                return replace(
                    result, errors=("validation failed",),
                    completion={"status": "needs-review"},
                    validation_processes=(validation,),
                )
        adapter = Validating(self.python, self.root)
        operation = adapter._operation("test", self.command("print('primary')").arguments, ())
        run = self.registry.get(self.registry.start(adapter, operation)["id"])
        record = self.completed(run)
        self.assertEqual(record["process"]["stdout"], "primary\n")
        self.assertEqual(record["validation_processes"][0]["exit_status"], 9)
        self.assertEqual(record["completion"], {"status": "needs-review"})
        self.assertFalse(record["success"])

    def test_raw_spool_publication_race_symlinks_and_byte_fidelity(self):
        output = self.root / "output"
        command = self.command("import sys;sys.stdout.buffer.write(b'raw\\xff\\r\\n')")
        with OwnedChild(command, output_limit=2, capture_complete=True) as child:
            child.wait()
            output.write_bytes(b"race winner")
            process = child.complete_result(stdout_artifact=output)
            self.assertIn("publication failed", process.publication_error)
            self.assertEqual(output.read_bytes(), b"race winner")
            output.unlink()
            output.symlink_to(self.root / "missing-target")
            process = child.complete_result(stdout_artifact=output)
            self.assertIn("publication failed", process.publication_error)
            self.assertTrue(output.is_symlink())
            self.assertFalse((self.root / "missing-target").exists())
            output.unlink()
            process = child.complete_result(stdout_artifact=output)
            self.assertIsNone(process.publication_error)
            self.assertEqual(output.read_bytes(), b"raw\xff\r\n")
            self.assertNotEqual(process.stdout.encode("utf-8"), output.read_bytes())
            spool = child._stdout.spool
        self.assertTrue(spool.closed)

    def test_reaped_child_closes_capture_threads(self):
        with OwnedChild(self.command("print('done')")) as child:
            result = child.wait(timeout=5)
            self.assertEqual(result.stdout, "done\n")
            self.assertTrue(process_document(result)["success"])
            self.assertFalse(child._stdout.thread.is_alive())
            self.assertFalse(child._stderr.thread.is_alive())

    def test_spool_read_failure_retains_process_diagnostics(self):
        with mock.patch.object(
            CapturedStream, "read_complete", side_effect=OSError("fixture spool read failure"),
        ):
            run = self.start("import sys;print('primary');print('diagnostic',file=sys.stderr)")
            record = self.completed(run)
        self.assertEqual(record["process"]["state"], "exited")
        self.assertEqual(record["process"]["exit_status"], 0)
        self.assertEqual(record["process"]["stdout"], "primary\n")
        self.assertEqual(record["process"]["stderr"], "diagnostic\n")
        self.assertIn("complete output read failed", record["process"]["error"])
        self.assertFalse(record["success"])

    def test_pipe_held_open_does_not_block_capture_cleanup(self):
        read_fd, write_fd = os.pipe()
        try:
            capture = CapturedStream(os.fdopen(read_fd, "rb"), 4)
            os.write(write_fd, b"captured")
            eventually(lambda: capture.snapshot()[2] == 8)
            began = time.monotonic()
            capture.finish()
            capture.thread.join(2)
            self.assertFalse(capture.thread.is_alive())
            from tools.orchestration.process import CAPTURE_GRACE
            self.assertGreaterEqual(time.monotonic() - began, CAPTURE_GRACE)
            text, truncated, count, error = capture.snapshot()
            self.assertEqual((text, truncated, count), ("capt", True, 8))
            self.assertIn("EOF not received", error)
            self.assertIn("capture incomplete", error)
            self.assertFalse(capture.eof_received)
            self.assertTrue(capture.stream.closed)
        finally:
            os.close(write_fd)

    def test_stop_before_spawn_is_cancelled_without_starting(self):
        entered, release = threading.Event(), threading.Event()
        class Gated(ToolAdapter):
            tool, operations = "fixture", ("test",)
            def execute(self, operation, runner):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("fixture release deadline")
                return super().execute(operation, runner)
        adapter = Gated(self.python, self.root)
        operation = adapter._operation("test", self.command("print('must not start')").arguments, ())
        run = self.registry.get(self.registry.start(adapter, operation)["id"])
        self.assertTrue(entered.wait(5))
        try:
            with mock.patch("subprocess.Popen", side_effect=AssertionError("cancelled spawn")):
                run.stop()
                release.set()
                result = self.completed(run)
            self.assertEqual(result["process"]["state"], "cancelled")
            self.assertTrue(result["stop_requested"])
            self.assertIsNone(result["process"]["pid"])
        finally:
            release.set()

    def test_default_one_mib_limits_and_split_utf8(self):
        from tools.orchestration.process import OUTPUT_LIMIT
        result = ProcessRunner().run(self.command(
            f"import sys;sys.stdout.write('o'*{OUTPUT_LIMIT + 1});"
            f"sys.stderr.write('e'*{OUTPUT_LIMIT + 2})"
        ))
        self.assertEqual(len(result.stdout), OUTPUT_LIMIT + 1)
        self.assertEqual(len(result.stderr), OUTPUT_LIMIT + 2)
        self.assertFalse(result.stdout_truncated or result.stderr_truncated)
        console = process_document(result)
        self.assertEqual(len(console["stdout"]), OUTPUT_LIMIT)
        self.assertEqual(len(console["stderr"]), OUTPUT_LIMIT)
        self.assertTrue(console["stdout_truncated"] and console["stderr_truncated"])
        read_fd, write_fd = os.pipe()
        capture = CapturedStream(os.fdopen(read_fd, "rb"), 64)
        try:
            os.write(write_fd, b"\xe2\x82")
            eventually(lambda: capture.snapshot()[2] == 2)
            self.assertEqual(capture.snapshot()[0], "")
            os.write(write_fd, b"\xac")
            eventually(lambda: capture.snapshot()[0] == "\u20ac")
        finally:
            os.close(write_fd)
            capture.thread.join(2)
        self.assertEqual(capture.snapshot()[0], "\u20ac")

    @unittest.skipUnless((ROOT / "build/melisma").is_file(), "existing Melisma build unavailable; no build performed")
    def test_existing_melisma_stdout_publication_for_abc_and_ustx(self):
        adapter = MelismaAdapter(ROOT / "build/melisma", ROOT)
        for mode in ("abc", "ustx"):
            operation = adapter.build(
                mode, ROOT / "examples/five-notes.mid", ROOT / "examples/gloria.txt",
                self.root / f"actual.{mode}", publish_stdout=True,
            )
            run = self.registry.get(self.registry.start(adapter, operation)["id"])
            record = self.completed(run)
            self.assertTrue(record["success"], record)
            self.assertEqual(
                (self.root / f"actual.{mode}").read_text(), record["process"]["stdout"],
            )

    def test_artifact_larger_than_console_limit_succeeds_for_both_modes(self):
        from tools.orchestration.process import OUTPUT_LIMIT
        executable = self.root / "large-melisma"
        executable.write_text(
            f"#!{self.python}\n"
            "import pathlib,sys\n"
            "sys.stdout.buffer.write(pathlib.Path('producer.data').read_bytes())\n"
            "print('underlying diagnostic',file=sys.stderr)\n"
        )
        executable.chmod(0o755)
        midi, lyrics = self.root / "input.mid", self.root / "lyrics.txt"
        midi.write_text("fixture")
        lyrics.write_text("fixture")
        adapter = MelismaAdapter(executable, self.root)
        for mode, header in (
            ("abc", b"X:1\nM:4/4\nL:1/32\nK:C\nV: Vocal\nV: Ins\n%"),
            ("ustx", b"ustx_version: 0.7\nresolution: 480\ntracks:\nvoice_parts:\n#"),
        ):
            data = header + b"x" * (OUTPUT_LIMIT + 257) + b"\r\n# tail \xe2\x82\xac\r\n"
            (self.root / "producer.data").write_bytes(data)
            for limit in (64, OUTPUT_LIMIT):
                with self.subTest(mode=mode, limit=limit):
                    registry = RunRegistry(output_limit=limit)
                    self.addCleanup(registry.close)
                    output = self.root / f"large-{limit}.{mode}"
                    operation = adapter.build(mode, midi, lyrics, output, publish_stdout=True)
                    run = registry.get(registry.start(adapter, operation)["id"])
                    record = self.completed(run)
                    self.assertTrue(record["success"], record["errors"])
                    self.assertTrue(record["artifacts"][0]["valid"])
                    self.assertEqual(record["artifacts"][0]["size"], len(data))
                    self.assertEqual(output.read_bytes(), data)
                    self.assertTrue(record["process"]["stdout_truncated"])
                    self.assertLessEqual(len(record["process"]["stdout"].encode()), limit)
                    self.assertEqual(record["process"]["stdout_bytes"], len(data))
                    self.assertEqual(record["process"]["stderr"], "underlying diagnostic\n")

    def test_large_machine_output_for_probe_and_session_validation(self):
        from dataclasses import replace
        from tools.orchestration.artifacts import AudioValidator
        from tools.orchestration.process import OUTPUT_LIMIT
        data = {
            "streams": [{"codec_name": "pcm_s16le", "sample_rate": "44100", "channels": 2}],
            "format": {"duration": "1.0"}, "padding": "x" * (OUTPUT_LIMIT + 17),
            "tail": "complete",
        }
        (self.root / "probe.json").write_text(json.dumps(data))
        probe = self.root / "probe"
        probe.write_text(
            f"#!{self.python}\n"
            "import pathlib;print(pathlib.Path('probe.json').read_text())\n"
        )
        probe.chmod(0o755)
        validator = AudioValidator(probe, self.root)
        errors, check = validator.validate(self.root / "dummy.wav", ProcessRunner())
        self.assertEqual(errors, ())
        self.assertEqual(json.loads(check.stdout), data)
        self.assertFalse(check.stdout_truncated)
        class Validating(ToolAdapter):
            tool, operations = "fixture", ("test",)
            def complete(self, result, runner):
                errors, check = validator.validate(probe, runner)
                parsed = json.loads(check.stdout)
                return replace(
                    result, errors=errors, completion={"tail": parsed["tail"]},
                    validation_processes=(check,),
                )
        adapter = Validating(self.python, self.root)
        operation = adapter._operation("test", self.command("print('primary')").arguments, ())
        run = self.registry.get(self.registry.start(adapter, operation)["id"])
        record = self.completed(run)
        self.assertTrue(record["success"])
        self.assertEqual(record["completion"], {"tail": "complete"})
        self.assertTrue(record["validation_processes"][0]["stdout_truncated"])
        self.assertEqual(len(run.result.validation_processes[0].stdout), OUTPUT_LIMIT)
        self.assertEqual(len(record["validation_processes"][0]["stdout"]), OUTPUT_LIMIT)

    def test_valid_json_prefix_cannot_hide_invalid_authoritative_suffix(self):
        from tools.orchestration.yue2 import InstrumentalAdapter
        from tools.orchestration.process import OUTPUT_LIMIT
        adapter = InstrumentalAdapter(self.python, self.root)
        adapter.script.parent.mkdir(parents=True)
        adapter.script.write_text(
            f"print('{{\"verified\":true}}' + ' ' * {OUTPUT_LIMIT + 17} + 'INVALID')\n"
        )
        result = adapter.execute(adapter.verify(self.root), ProcessRunner())
        self.assertTrue(result.process.success)
        self.assertGreater(len(result.process.stdout), OUTPUT_LIMIT)
        self.assertFalse(result.success)
        self.assertIsNone(result.completion)
        self.assertTrue(result.errors)

    def test_ffmpeg_analysis_receives_complete_large_stderr(self):
        from tools.orchestration.adapters import FFmpegAdapter
        from tools.orchestration.process import OUTPUT_LIMIT, OperationResult, ProcessResult
        executable = self.root / "analysis"
        executable.write_text(
            f"#!{self.python}\n"
            f"import sys;sys.stderr.write('x'*{OUTPUT_LIMIT + 17}"
            " + '\\nmean_volume: -10.0 dB\\nmax_volume: -2.0 dB\\n')\n"
        )
        executable.chmod(0o755)
        output = self.root / "mix.wav"
        output.write_bytes(b"fixture")
        adapter = FFmpegAdapter(executable, self.root)
        operation = adapter._operation("mix", (), (ExpectedArtifact(output, "audio"),))
        result = adapter.complete(OperationResult(
            operation, ProcessResult(operation.command, ProcessState.EXITED, 0), (),
        ), ProcessRunner())
        self.assertTrue(result.success, result.errors)
        self.assertEqual(result.completion["max_volume_db"], -2.0)
        self.assertGreater(len(result.validation_processes[0].stderr), OUTPUT_LIMIT)
        self.assertTrue(process_document(result.validation_processes[0])["stderr_truncated"])

    def test_incomplete_capture_is_visible_and_never_published(self):
        read_fd, write_fd = os.pipe()
        try:
            with OwnedChild(self.command("pass"), output_limit=4, capture_complete=True) as child:
                child._stdout.thread.join(2)
                self.assertFalse(child._stdout.thread.is_alive())
                child._stdout = CapturedStream(
                    os.fdopen(read_fd, "rb"), 4, spool=child._stdout.spool,
                )
                os.write(write_fd, b"captured")
                eventually(lambda: child._stdout.snapshot()[2] == 8)
                process = child.wait(timeout=5)
                self.assertEqual(process.stdout, "capt")
                self.assertEqual(process.stdout_bytes, 8)
                self.assertTrue(process.stdout_truncated)
                self.assertFalse(process.stdout_complete)
                self.assertTrue(process.stderr_complete)
                self.assertIn("stdout: EOF not received", process.error)
                self.assertEqual(process.exit_status, 0)
                self.assertFalse(process.success)
                output = self.root / "must-not-publish"
                authoritative = child.complete_result(stdout_artifact=output)
                self.assertEqual(authoritative.stdout, "captured")
                self.assertFalse(output.exists())
                document = process_document(authoritative)
                self.assertFalse(document["stdout_complete"])
                self.assertIn("capture incomplete", document["error"])
                spool = child._stdout.spool
            self.assertTrue(spool.closed)
        finally:
            os.close(write_fd)

    def test_environment_values_are_not_in_any_command_document(self):
        from dataclasses import replace
        from tools.orchestration.process import ProcessResult
        command = replace(
            self.command("pass"),
            environment=(("PRIVATE_SETTING", "synthetic-private-override-value"),),
        )
        operation = replace(self.operation("pass"), command=command)
        process = ProcessResult(command, ProcessState.EXITED, 0)
        for document in (
            command_document(command), operation_document(operation), process_document(process),
        ):
            self.assertNotIn("synthetic-private-override-value", json.dumps(document))
            self.assertNotIn("environment", document.get("command", document))


if __name__ == "__main__":
    unittest.main()
