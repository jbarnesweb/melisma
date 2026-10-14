"""Issue #2 contracts: harmless local helpers, never models or audible playback."""

import hashlib
import json
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import (
    FFmpegAdapter, MelismaAdapter, OpenUtauAdapter, ToolAdapter,
)
from tools.orchestration.artifacts import AudioValidator, inspect_artifact
from tools.orchestration.playback import (
    FFplayAdapter, PlaybackState, PlaybackValidationError, playback_state,
)
from tools.orchestration.process import (
    Command, ExpectedArtifact, OwnedChild, ProcessResult, ProcessRunner, ProcessState,
)
from tools.orchestration.yue2 import (
    HELPER_FILES, NATIVE_FILES, PREPARED_FILES, InstrumentalAdapter, NativeYuE2Adapter,
)


PROBE = json.dumps({
    "streams": [{"codec_name": "pcm_s16le", "sample_rate": "44100", "channels": 2}],
    "format": {"duration": "1.0"},
})


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.python = Path(sys.executable).resolve()
        self.runner = ProcessRunner()
        self.audio = AudioValidator(self.python, self.root)

    def file(self, name, content=b"input"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def helper(self, name, code):
        script = self.file(name, (f"#!{self.python}\n" + code + "\n").encode())
        script.chmod(0o755)
        return script

    def command(self, code, *arguments):
        return Command(self.python, ("-B", "-c", code, *arguments), self.root)

    def fake_result(self, command, status=0, stdout="", stderr=""):
        return ProcessResult(command, ProcessState.EXITED, status, stdout, stderr, pid=123)

    def producing_runner(self, producer, status=0, stderr=""):
        def execute(command):
            if command.arguments[:2] == ("-v", "error"):
                return self.fake_result(command, stdout=PROBE)
            producer()
            return self.fake_result(command, status=status, stdout="generated", stderr=stderr)
        runner = mock.Mock(spec=ProcessRunner)
        runner.run.side_effect = execute
        return runner


class ProcessTests(TemporaryTest):
    def test_argv_quoting_cwd_and_separate_streams(self):
        values = ("space here", "'quotes'", 'double"quote', "$(touch injected)", "; echo bad",
                  "*", "--switch-like", "")
        command = self.command(
            "import json, os, sys; print(json.dumps([os.getcwd(), sys.argv[1:]])); "
            "print('diagnostic', file=sys.stderr)", *values
        )
        result = self.runner.run(command)
        self.assertTrue(result.success)
        self.assertEqual(result.state, ProcessState.EXITED)
        self.assertEqual(result.exit_status, 0)
        self.assertEqual(json.loads(result.stdout), [str(self.root), list(values)])
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertFalse((self.root / "injected").exists())
        self.assertEqual(command.argv[0], str(self.python))
        self.assertEqual(command.arguments[-len(values):], values)
        self.assertIn("'space here'", command.display)
        self.assertGreater(result.pid, 0)

    def test_nonzero_retains_all_execution_data(self):
        command = self.command(
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"
        )
        result = self.runner.run(command)
        self.assertFalse(result.success)
        self.assertEqual((result.exit_status, result.stdout, result.stderr), (7, "out\n", "err\n"))
        self.assertIs(result.command, command)

    def test_signal_is_not_normal_exit(self):
        result = self.runner.run(self.command("import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
        self.assertEqual(result.state, ProcessState.SIGNALLED)
        self.assertEqual(result.signal, signal.SIGTERM)
        self.assertFalse(result.success)

    def test_start_failures_are_records(self):
        command = Command(self.root / "missing", (), self.root)
        result = self.runner.run(command)
        self.assertEqual(result.state, ProcessState.START_FAILED)
        self.assertIsNone(result.exit_status)
        self.assertTrue(result.error)
        result = self.runner.run(Command(self.python, (), self.root / "missing-cwd"))
        self.assertEqual(result.state, ProcessState.START_FAILED)

    def test_environment_overrides_are_child_only(self):
        with mock.patch.dict("os.environ", {"MELISMA_TEST": "parent", "DELETE_TEST": "parent"}):
            command = Command(
                self.python,
                ("-B", "-c", "import os; print(os.environ['MELISMA_TEST']); "
                 "print('DELETE_TEST' in os.environ)"),
                self.root, (("MELISMA_TEST", "child"), ("DELETE_TEST", None)),
            )
            result = self.runner.run(command)
            self.assertEqual(result.stdout, "child\nFalse\n")
            import os
            self.assertEqual(os.environ["MELISMA_TEST"], "parent")

    def test_invalid_command_structure(self):
        for executable, arguments, cwd in (
            (Path("python"), (), self.root),
            (self.python, (), Path(".")),
            (self.python, "shell string", self.root),
            (self.python, ("bad\0value",), self.root),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                Command(executable, arguments, cwd)

    def test_missing_empty_present_and_partial_artifacts(self):
        adapter = ToolAdapter(self.python, self.root)
        adapter.tool = "test"
        adapter.operations = ("write",)
        for name, code, exit_code, valid in (
            ("missing", "pass", 0, False),
            ("empty", "open('empty', 'w').close()", 0, False),
            ("present", "open('present', 'w').write('data')", 0, True),
            ("partial", "import sys; open('partial', 'w').write('partial'); sys.exit(3)", 3, True),
        ):
            with self.subTest(name=name):
                operation = adapter._operation(
                    "write", ("-B", "-c", code),
                    (ExpectedArtifact(self.root / name),),
                )
                result = adapter.execute(operation, self.runner)
                self.assertEqual(result.process.exit_status, exit_code)
                self.assertEqual(result.artifacts[0].valid, valid)
                self.assertEqual(result.success, valid and exit_code == 0)
                self.assertIs(result.operation, operation)

    def test_stale_artifact_never_proves_success(self):
        adapter = ToolAdapter(self.python, self.root)
        adapter.tool, adapter.operations = "test", ("write",)
        operation = adapter._operation(
            "write", ("-B", "-c", "raise RuntimeError('must not run')"),
            (ExpectedArtifact(self.root / "output"),),
        )
        path = self.file("output", b"preserved")
        result = adapter.execute(operation, self.runner)
        self.assertEqual(result.process.state, ProcessState.REJECTED)
        self.assertFalse(result.success)
        self.assertEqual(path.read_bytes(), b"preserved")

    def test_invalid_structured_artifacts(self):
        for kind, content in (
            ("json", b"[]"), ("json", b"{broken"), ("ir", b"{}\n"),
            ("abc", b"not ABC"), ("ustx", b"not USTX"),
        ):
            with self.subTest(kind=kind):
                record = inspect_artifact(ExpectedArtifact(self.file("bad", content), kind))
                self.assertFalse(record.valid)
                self.assertTrue(record.error)


class AdapterTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.midi = self.file("-input with spaces.mid")
        self.lyrics = self.file("lyrics.txt")
        self.ustx = self.file("input.USTX")

    def test_melisma_all_single_file_modes(self):
        adapter = MelismaAdapter(self.root / "melisma", self.root)
        for mode in ("ir", "abc", "ustx"):
            with self.subTest(mode=mode):
                operation = adapter.build(mode, self.midi, self.lyrics, Path(f"{mode}.out"))
                self.assertEqual(
                    operation.command.arguments,
                    ("--format", mode, str(self.midi), str(self.lyrics), str(self.root / f"{mode}.out")),
                )
                self.assertEqual(operation.expected[0].kind, mode)

    def test_melisma_metadata_and_bundle_order(self):
        adapter = MelismaAdapter(self.root / "melisma", self.root)
        for mode in ("ustx", "render-inputs"):
            with self.subTest(mode=mode):
                operation = adapter.build(
                    mode, self.midi, self.lyrics, Path(mode),
                    singer="Singer name", phonemizer="Example.Class", renderer="DIFFSINGER",
                )
                prefix = ("render-inputs",) if mode == "render-inputs" else ("--format", "ustx")
                self.assertEqual(
                    operation.command.arguments,
                    (*prefix, "--singer", "Singer name", "--phonemizer", "Example.Class",
                     "--renderer", "DIFFSINGER", str(self.midi), str(self.lyrics), str(self.root / mode)),
                )
                if mode == "render-inputs":
                    self.assertEqual(
                        [item.path.name for item in operation.expected],
                        ["bound.jsonl", "vocal.ustx", "accompaniment.abc"],
                    )
                    self.assertNotIn("--format", operation.command.arguments)

    def test_melisma_rejects_unsupported_and_unsafe_outputs(self):
        adapter = MelismaAdapter(self.python, self.root)
        for mode, kwargs in (("midi", {}), ("abc", {"singer": "Singer"})):
            with self.assertRaises(ValueError):
                adapter.build(mode, self.midi, self.lyrics, Path("out"), **kwargs)
        for output in (self.lyrics, self.root / "absent-parent" / "out"):
            with self.assertRaises(ValueError):
                adapter.build("ir", self.midi, self.lyrics, output)
        directory = self.root / "bundle"
        directory.mkdir()
        adapter.build("render-inputs", self.midi, self.lyrics, directory)
        self.file("bundle/keep")
        with self.assertRaises(ValueError):
            adapter.build("render-inputs", self.midi, self.lyrics, directory)

    def test_dangling_output_symlink_is_not_overwritten(self):
        output = self.root / "link"
        output.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            MelismaAdapter(self.python, self.root).build("ir", self.midi, self.lyrics, output)

    def test_strict_openutau_render(self):
        adapter = OpenUtauAdapter(self.python, self.root, audio=self.audio)
        operation = adapter.build(self.ustx, Path("output.WAV"))
        self.assertEqual(
            operation.command.arguments,
            ("render", str(self.ustx), "--out", str(self.root / "output.WAV")),
        )
        for flag in ("--singer", "--phonemizer", "--renderer", "--device"):
            self.assertNotIn(flag, operation.command.arguments)
        with self.assertRaises(TypeError):
            adapter.build(self.ustx, Path("out.wav"), singer="invented")
        with self.assertRaises(ValueError):
            adapter.build(self.lyrics, Path("out.wav"))
        with self.assertRaises(ValueError):
            adapter.build(self.ustx, Path("out.flac"))

    def test_ffmpeg_explicit_mix_contract(self):
        adapter = FFmpegAdapter(self.python, self.root, audio=self.audio)
        instrumental = self.file("instrumental.flac")
        vocal = self.file("vocal.wav")
        for duration in ("first", "shortest", "longest"):
            for normalize in (False, True):
                operation = adapter.build(
                    instrumental, vocal, Path("mix.wav"),
                    instrumental_gain=1, vocal_gain=0.5, master_gain=0.707945784,
                    duration=duration, normalize=normalize,
                )
                self.assertEqual(
                    operation.command.arguments,
                    ("-hide_banner", "-nostdin", "-n", "-i", str(instrumental),
                     "-i", str(vocal), "-filter_complex",
                     "[0:a]volume=1.0[accompaniment];[1:a]volume=0.5[vocal];"
                     f"[accompaniment][vocal]amix=inputs=2:duration={duration}:"
                     f"normalize={int(normalize)}[mix];[mix]volume=0.707945784[out]",
                     "-map", "[out]", "-c:a", "pcm_s16le", str(self.root / "mix.wav")),
                )

    def test_ffmpeg_rejects_filter_injection_and_invalid_gains(self):
        adapter = FFmpegAdapter(self.python, self.root)
        for gain in ("1;anull", "1", True, 0, -1, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                adapter.build(self.midi, self.lyrics, Path("mix.wav"),
                              instrumental_gain=gain, vocal_gain=1, master_gain=1,
                              duration="first", normalize=False)
        for duration, normalize in (("first;evil", False), ("first", "false")):
            with self.assertRaises(ValueError):
                adapter.build(self.midi, self.lyrics, Path("mix.wav"),
                              instrumental_gain=1, vocal_gain=1, master_gain=1,
                              duration=duration, normalize=normalize)

    def test_audio_validation_records_failure_and_success(self):
        adapter = OpenUtauAdapter(self.python, self.root, audio=self.audio)
        operation = adapter.build(self.ustx, Path("audio.wav"))
        runner = self.producing_runner(lambda: self.file("audio.wav"))
        result = adapter.execute(operation, runner)
        self.assertTrue(result.success)
        self.assertEqual(len(result.validation_processes), 1)
        self.assertEqual(result.validation_processes[0].stdout, PROBE)
        (self.root / "audio.wav").unlink()
        runner.run.side_effect = (
            lambda command: self.fake_result(command, stdout="not JSON")
            if command.arguments[:2] == ("-v", "error")
            else (self.file("audio.wav") and self.fake_result(command))
        )
        result = adapter.execute(operation, runner)
        self.assertFalse(result.success)
        self.assertFalse(result.artifacts[0].valid)
        self.assertTrue(result.errors)

    def test_missing_audio_validator_is_not_success(self):
        adapter = OpenUtauAdapter(self.python, self.root)
        operation = adapter.build(self.ustx, Path("audio.wav"))
        result = adapter.execute(operation, self.producing_runner(lambda: self.file("audio.wav")))
        self.assertTrue(result.process.success)
        self.assertFalse(result.success)

    def test_ffmpeg_peak_analysis_preserves_checks_and_rejects_clipping(self):
        for index, (peak, success) in enumerate((("-2.0", True), ("0.0", False),
                                               ("1.0", False), ("-inf", True), ("bad", False))):
            with self.subTest(peak=peak):
                adapter = FFmpegAdapter(self.python, self.root, audio=self.audio)
                name = f"mix-{index}.wav"
                operation = adapter.build(
                    self.midi, self.lyrics, Path(name), instrumental_gain=1,
                    vocal_gain=0.5, master_gain=0.7, duration="first", normalize=False,
                )
                def execute(command):
                    if command.arguments[:2] == ("-v", "error"):
                        return self.fake_result(command, stdout=PROBE)
                    if "-af" in command.arguments:
                        return self.fake_result(
                            command, stderr=f"mean_volume: -10.0 dB\nmax_volume: {peak} dB\n"
                        )
                    self.file(name)
                    return self.fake_result(command)
                runner = mock.Mock(spec=ProcessRunner)
                runner.run.side_effect = execute
                result = adapter.execute(operation, runner)
                self.assertEqual(result.success, success)
                self.assertEqual(len(result.validation_processes), 2)
                self.assertEqual(result.validation_processes[-1].command.arguments[-4:],
                                 ("volumedetect", "-f", "null", "-"))

    def test_surface_queries_do_not_execute_or_probe(self):
        with mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected probe")):
            for adapter_class in (
                MelismaAdapter, OpenUtauAdapter, FFmpegAdapter, NativeYuE2Adapter,
                InstrumentalAdapter, FFplayAdapter,
            ):
                adapter = adapter_class(self.python, self.root)
                self.assertTrue(adapter.operations)
                self.assertTrue(adapter.tool)


class YuE2Tests(TemporaryTest):
    def native(self, **overrides):
        adapter = NativeYuE2Adapter(self.python, self.root, audio=self.audio)
        values = dict(style="Piano", lyrics="Song words", run_id="song-1", seed=42,
                      model=Path("native-model"), vae=Path("native-vae"), device="cuda",
                      cot="melody", abc=self.file("score-input.abc"))
        values.update(overrides)
        return adapter, adapter.build(Path("native-output"), **values)

    def manifest(self, root, names):
        return {
            name: {"bytes": (root / name).stat().st_size,
                   "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
            for name in names
        }

    def native_files(self, root, flags=None, status="complete"):
        root.mkdir(parents=True, exist_ok=True)
        names = (*NATIVE_FILES, "score.abc")
        for name in names:
            (root / name).write_bytes(b"fixture content")
        receipt = dict(
            status=status, truncated=flags if flags is not None else {"abc": False, "semantic": False},
            artifacts=self.manifest(root, names),
        )
        (root / "result.json").write_text(json.dumps(receipt))
        return receipt

    def test_native_command_environment_and_artifact_root(self):
        _, operation = self.native()
        self.assertEqual(operation.command.arguments, (
            "-B", "-m", "yue2.cli", "generate", "--style=Piano", "--lyrics=Song words",
            "--cot", "melody", "--seed", "42", "--id", "song-1",
            "--output", str(self.root / "native-output"),
            "--model", str(self.root / "native-model"), "--vae", str(self.root / "native-vae"),
            "--device", "cuda", "--offline", "--abc-file", str(self.root / "score-input.abc"),
        ))
        self.assertEqual(dict(operation.command.environment)["PYTHONPATH"], str(self.root / "src"))
        self.assertEqual(dict(operation.metadata)["contract_version"], "0.1.6")
        self.assertEqual(operation.expected[0].path, self.root / "native-output/song-1/audio.flac")

    def test_native_and_helper_are_not_interchangeable(self):
        native, operation = self.native()
        helper = InstrumentalAdapter(self.root / "pinned-python", self.root, audio=self.audio)
        prepared = helper.prepare(self.file("input.abc"), Path("preparation"), style="Piano",
                                  run_id="instrumental", seed=12)
        self.assertEqual(prepared.command.arguments, (
            "-B", str(helper.script), "cover", "--abc", str(self.root / "input.abc"),
            "--composer", "agent", "--style", "Piano", "--empty-lyrics",
            "--id", "instrumental", "--seed", "12", "--output", str(self.root / "preparation"),
            "--prepare-only", "--offline",
        ))
        self.assertEqual(dict(prepared.command.environment)["PYTHONPATH"], None)
        self.assertEqual(dict(prepared.metadata)["contract_version"], "0.1.5")
        self.assertNotEqual(prepared.command.executable, operation.command.executable)
        self.file("prepared/prepared-manifest.json", b'{"files": {}}')
        generation = helper.generate(Path("prepared"), Path("generation"),
                                     models_root=Path("pinned-models"), device="cuda")
        self.assertEqual(generation.command.arguments, (
            "-B", str(helper.script), "generate", "--prepared", str(self.root / "prepared"),
            "--output", str(self.root / "generation"), "--models-root", str(self.root / "pinned-models"),
            "--device", "cuda", "--offline",
        ))
        self.assertEqual(generation.expected[0].path, self.root / "generation/native/audio.flac")
        verification = helper.verify(self.root)
        self.assertEqual(verification.command.arguments, ("-B", str(helper.script), "verify", str(self.root)))
        with self.assertRaises(ValueError):
            helper.execute(operation, self.runner)
        with self.assertRaises(ValueError):
            native.execute(generation, self.runner)

    def test_native_invalid_request_boundaries(self):
        for overrides in (
            {"run_id": "../escape"}, {"run_id": "-id"}, {"seed": True},
            {"cot": "off"}, {"cot": "unsupported"}, {"style": ""},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.native(**overrides)
        _, operation = self.native(cot="off", abc=None, lyrics="")
        self.assertIn("--lyrics=", operation.command.arguments)

    def test_native_complete_with_hash_receipts(self):
        adapter, operation = self.native()
        root = self.root / "native-output/song-1"
        result = adapter.execute(operation, self.producing_runner(lambda: self.native_files(root)))
        self.assertTrue(result.success)
        self.assertEqual(result.process.stdout, "generated")
        self.assertEqual(result.completion["native"]["truncated"], {"abc": False, "semantic": False})
        self.assertGreater(len(result.artifacts), 2)

    def test_native_exit_zero_truncation_and_incomplete_states(self):
        cases = (
            ({"abc": True, "semantic": False}, "complete"),
            ({"abc": False, "semantic": True}, "complete"),
            ({"abc": False}, "complete"),
            ({"abc": 0, "semantic": False}, "complete"),
            ({"abc": False, "semantic": False}, "needs_review"),
        )
        for index, (flags, status) in enumerate(cases):
            with self.subTest(flags=flags, status=status):
                adapter, operation = self.native()
                root = self.root / "native-output/song-1"
                # Each case has an independent output to avoid stale-output acceptance.
                operation = adapter.build(
                    Path(f"case-{index}"), style="Piano", lyrics="words", run_id="song",
                    seed=0, model=Path("model"), vae=Path("vae"), device="cuda", cot="melody",
                )
                root = self.root / f"case-{index}/song"
                result = adapter.execute(
                    operation, self.producing_runner(lambda: self.native_files(root, flags, status))
                )
                self.assertTrue(result.process.success)
                self.assertFalse(result.success)
                self.assertTrue(result.errors)
                self.assertEqual(result.completion["native"]["truncated"], flags)

    def test_native_missing_malformed_corrupt_and_escaping_receipts(self):
        for index, failure in enumerate(("missing", "malformed", "hash", "escape", "manifest")):
            with self.subTest(failure=failure):
                adapter, operation = self.native()
                operation = adapter.build(
                    Path(f"bad-{index}"), style="Piano", lyrics="words", run_id="song",
                    seed=0, model=Path("model"), vae=Path("vae"), device="cuda", cot="melody",
                )
                root = self.root / f"bad-{index}/song"
                def produce():
                    receipt = self.native_files(root)
                    if failure == "missing":
                        (root / "audio.flac").unlink()
                    elif failure == "malformed":
                        (root / "result.json").write_text("not JSON")
                        return
                    elif failure == "hash":
                        (root / "prefix.npy").write_bytes(b"tampered")
                    elif failure == "escape":
                        receipt["artifacts"]["../../outside"] = {"sha256": "0" * 64, "bytes": 1}
                    else:
                        del receipt["artifacts"]["semantic.npy"]
                    (root / "result.json").write_text(json.dumps(receipt))
                result = adapter.execute(operation, self.producing_runner(produce))
                self.assertTrue(result.process.success)
                self.assertFalse(result.success)
                self.assertTrue(result.errors)

    def test_nonzero_retains_native_completion(self):
        adapter, operation = self.native()
        result = adapter.execute(
            operation,
            self.producing_runner(lambda: self.native_files(self.root / "native-output/song-1"),
                                  status=2, stderr="generation failed"),
        )
        self.assertFalse(result.success)
        self.assertEqual(result.process.exit_status, 2)
        self.assertEqual(result.process.stderr, "generation failed")
        self.assertEqual(result.completion["native"]["status"], "complete")

    def test_helper_preparation_allows_pinned_empty_lyrics(self):
        adapter = InstrumentalAdapter(self.python, self.root)
        operation = adapter.prepare(self.file("input.abc"), Path("preparation"), style="Piano",
                                    run_id="instrumental", seed=1)
        def produce():
            root = self.root / "preparation/prepared"
            root.mkdir(parents=True)
            for name in PREPARED_FILES:
                (root / name).write_bytes(b"" if name == "lyrics.txt" else b"fixture")
            (root / "prepared-manifest.json").write_text(json.dumps(
                {"files": self.manifest(root, PREPARED_FILES)}
            ))
        result = adapter.execute(operation, self.producing_runner(produce))
        self.assertTrue(result.success)
        self.assertTrue(any(record.size == 0 and record.valid for record in result.artifacts))

    def test_helper_summary_version_and_truncation(self):
        for index, (version, truncated, success) in enumerate(
            (("0.1.5", False, True), ("0.1.6", False, False), ("0.1.5", True, False))
        ):
            with self.subTest(version=version, truncated=truncated):
                adapter = InstrumentalAdapter(self.python, self.root, audio=self.audio)
                prepared = self.file("prepared/prepared-manifest.json", b'{"files": {}}').parent
                operation = adapter.generate(prepared, Path(f"helper-{index}"),
                                             models_root=Path("models"), device="cuda")
                def produce():
                    root = self.root / f"helper-{index}"
                    self.native_files(root / "native")
                    for name in ("score.abc", "prompt.txt", "repair-report.json"):
                        (root / name).write_bytes(b"fixture")
                    (root / "summary.json").write_text(json.dumps(
                        {"status": "complete", "package_version": version,
                         "truncated": {"abc": False, "semantic": truncated},
                         "exact_prefix_verified": True, "exact_abc_verified": True}
                    ))
                    (root / "delivery-manifest.json").write_text(json.dumps(
                        {"files": self.manifest(root, HELPER_FILES)}
                    ))
                result = adapter.execute(operation, self.producing_runner(produce))
                self.assertEqual(result.success, success)
                self.assertEqual(result.completion["summary"]["package_version"], version)

    def test_verify_requires_metadata_not_just_zero_exit(self):
        adapter = InstrumentalAdapter(self.python, self.root)
        operation = adapter.verify(self.root)
        for stdout, success in (("{}", False), ("garbage", False), ('{"verified": true}', True)):
            runner = mock.Mock(spec=ProcessRunner)
            runner.run.return_value = self.fake_result(operation.command, stdout=stdout)
            self.assertEqual(adapter.execute(operation, runner).success, success)


class PlaybackTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        probe = self.helper("fake-probe", f"print({PROBE!r})")
        self.audio = AudioValidator(probe, self.root)
        self.input = self.file("input.wav")

    def adapter(self, code):
        executable = self.helper("fake-player", code)
        return FFplayAdapter(executable, self.root, audio=self.audio)

    def test_missing_input_rejected_before_spawn(self):
        adapter = self.adapter("raise RuntimeError('should not run')")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("spawn")):
            with self.assertRaises(ValueError):
                adapter.build(Path("missing.wav"))

    def test_exact_ffplay_command_and_no_nostdin(self):
        operation = self.adapter("pass").build(self.input)
        self.assertEqual(operation.command.arguments, (
            "-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit",
            "-vn", "-i", str(self.input),
        ))
        self.assertNotIn("-nostdin", operation.command.arguments)

    def test_child_state_interpretation(self):
        command = self.command("pass")
        cases = (
            (0, "", False, True, PlaybackState.EOF),
            (0, "No such file or directory", False, True, PlaybackState.ERROR),
            (1, "", False, True, PlaybackState.ERROR),
            (0, "", False, False, PlaybackState.ERROR),
            (123, "", True, True, PlaybackState.STOPPED),
            (-signal.SIGTERM, "", True, True, PlaybackState.STOPPED),
        )
        for status, stderr, stopped, live, state in cases:
            result = ProcessResult(
                command, ProcessState.SIGNALLED if status < 0 else ProcessState.EXITED,
                status, stderr=stderr, stop_requested=stopped,
            )
            self.assertEqual(playback_state(result, live), state)

    def test_fake_child_eof_and_exit_zero_error(self):
        for code, state in (
            ("import time; time.sleep(0.03)", PlaybackState.EOF),
            ("import sys; print('decoder failed', file=sys.stderr)", PlaybackState.ERROR),
        ):
            adapter = self.adapter(code)
            operation = adapter.build(self.input)
            result = adapter.execute(operation, self.runner)
            self.assertEqual(result.completion["playback_state"], state.value)
            self.assertEqual(result.success, state == PlaybackState.EOF)
            self.assertEqual(result.process.exit_status, 0)
            self.assertEqual(len(result.validation_processes), 1)

    def test_owned_stop_exact_pid_and_idempotent_reap(self):
        adapter = self.adapter("import time; time.sleep(30)")
        with adapter.start(adapter.build(self.input), self.runner) as playback:
            with OwnedChild(self.command("import time; time.sleep(30)")) as other:
                self.assertEqual(playback.state, PlaybackState.RUNNING)
                self.assertNotEqual(playback.pid, other.pid)
                result = playback.stop(timeout=0.2)
                self.assertEqual(result.state, PlaybackState.STOPPED)
                self.assertTrue(result.process.stop_requested)
                self.assertEqual(result.process.pid, playback.pid)
                self.assertFalse(result.success)
                self.assertIsNone(other.poll())
                self.assertEqual(playback.stop(), result)

    def test_bounded_escalation_only_owned_child(self):
        ready = self.root / "ready"
        adapter = self.adapter(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"open({str(ready)!r}, 'w').write('ready')\n"
            "time.sleep(30)"
        )
        with adapter.start(adapter.build(self.input), self.runner) as playback:
            deadline = time.monotonic() + 2
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            started = time.monotonic()
            result = playback.stop(timeout=0.1)
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(result.state, PlaybackState.STOPPED)
            self.assertTrue(result.process.escalated)
            self.assertEqual(result.process.signal, signal.SIGKILL)

    def test_large_separate_logs_do_not_block_child(self):
        with OwnedChild(self.command(
            "import sys; print('o' * 200000); print('e' * 200000, file=sys.stderr)"
        )) as child:
            result = child.wait(timeout=3)
        self.assertEqual(result.stdout, "o" * 200000 + "\n")
        self.assertEqual(result.stderr, "e" * 200000 + "\n")
        self.assertFalse(result.stop_requested)

    def test_stop_after_eof_preserves_eof(self):
        adapter = self.adapter("import time; time.sleep(0.03)")
        with adapter.start(adapter.build(self.input), self.runner) as playback:
            eof = playback.wait(timeout=3)
            self.assertEqual(eof.state, PlaybackState.EOF)
            self.assertEqual(playback.stop(), eof)
            self.assertFalse(eof.process.stop_requested)

    def test_invalid_audio_does_not_start_playback(self):
        adapter = self.adapter("raise RuntimeError('must not run')")
        operation = adapter.build(self.input)
        probe = self.helper("bad-probe", "import sys; print('bad input', file=sys.stderr)")
        adapter.audio = AudioValidator(probe, self.root)
        with self.assertRaises(PlaybackValidationError) as caught:
            adapter.start(operation, self.runner)
        self.assertEqual(caught.exception.validation.stderr, "bad input\n")
        result = adapter.execute(operation, self.runner)
        self.assertEqual(result.process.state, ProcessState.REJECTED)
        self.assertFalse(result.success)

    def test_input_revalidated_before_start(self):
        adapter = self.adapter("pass")
        operation = adapter.build(self.input)
        self.input.unlink()
        with self.assertRaises(ValueError):
            adapter.start(operation, self.runner)

    def test_start_failure_retains_command_and_probe(self):
        adapter = FFplayAdapter(self.root / "missing-player", self.root, audio=self.audio)
        operation = adapter.build(self.input)
        result = adapter.execute(operation, self.runner)
        self.assertEqual(result.process.state, ProcessState.START_FAILED)
        self.assertIs(result.process.command, operation.command)
        self.assertTrue(result.validation_processes[0].success)

    def test_stop_timeout_must_be_bounded(self):
        with OwnedChild(self.command("import time; time.sleep(30)")) as child:
            for timeout in (0, -1, 6, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    child.stop(timeout)


if __name__ == "__main__":
    unittest.main()
