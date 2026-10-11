import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "tools" / "melisma_pipeline.py"
SPEC = importlib.util.spec_from_file_location("melisma_pipeline", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)


class PipelineTest(unittest.TestCase):
    def make_args(self, root: Path, **overrides):
        midi = root / "input.mid"
        lyrics = root / "lyrics.txt"
        key = root / "identity"
        melisma = root / "melisma"
        project = root / "OpenUtau.Cli.csproj"
        for path, contents in (
            (midi, b"midi"),
            (lyrics, b"lyrics"),
            (key, b"PRIVATE KEY CONTENT"),
            (melisma, b"executable"),
            (project, b"project"),
        ):
            path.write_bytes(contents)
        melisma.chmod(0o755)
        values = {
            "midi": midi.resolve(),
            "lyrics": lyrics.resolve(),
            "output": (root / "output").resolve(),
            "singer": "TIGER_DS_v102",
            "phonemizer": "Example.Phonemizer",
            "renderer": "DIFFSINGER",
            "style": "Instrumental, simple piano, no vocals",
            "seed": 831001,
            "runpod_host": "runpod.example",
            "runpod_port": 22094,
            "runpod_user": "root",
            "ssh_key": key.resolve(),
            "ssh_connect_timeout": 10,
            "melisma": melisma.resolve(),
            "openutau_project": project.resolve(),
            "yue2_repository": "/workspace/YuE",
            "vocal_gain": 0.25,
            "accompaniment_gain": 4.0,
            "verbose": False,
            "resume": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_local_command_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            melisma_output = root / "output" / "melisma"
            command = pipeline.melisma_command(args, melisma_output)
            self.assertEqual(command.count("render-inputs"), 1)
            self.assertEqual(
                command,
                [
                    str(args.melisma),
                    "render-inputs",
                    "--singer",
                    args.singer,
                    "--phonemizer",
                    args.phonemizer,
                    "--renderer",
                    args.renderer,
                    str(args.midi),
                    str(args.lyrics),
                    str(melisma_output),
                ],
            )
            self.assertEqual(
                pipeline.openutau_command(
                    args, melisma_output / "vocal.ustx", root / "vocal.wav"
                )[:8],
                [
                    "dotnet",
                    "run",
                    "--project",
                    str(args.openutau_project),
                    "--no-build",
                    "--",
                    "render",
                    str(melisma_output / "vocal.ustx"),
                ],
            )

    def test_ssh_and_scp_use_configured_connection(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = self.make_args(
                Path(temporary),
                runpod_host="changing-host.example",
                runpod_port=30222,
            )
            ssh = pipeline.ssh_base_args(args)
            scp = pipeline.scp_base_args(args)
            for command in (ssh, scp):
                self.assertIn("IdentitiesOnly=yes", command)
                self.assertIn(str(args.ssh_key), command)
            self.assertEqual(ssh[ssh.index("-p") + 1], "30222")
            self.assertEqual(scp[scp.index("-P") + 1], "30222")
            self.assertIn("root@changing-host.example", ssh)
            self.assertIn("BatchMode=yes", ssh)
            self.assertIn("ConnectTimeout=10", ssh)

    def test_runpod_endpoint_has_no_default(self):
        parser = pipeline.build_parser()
        host_action = next(
            action for action in parser._actions if action.dest == "runpod_host"
        )
        port_action = next(
            action for action in parser._actions if action.dest == "runpod_port"
        )
        self.assertTrue(host_action.required)
        self.assertTrue(port_action.required)
        self.assertIsNone(host_action.default)
        self.assertIsNone(port_action.default)

    def test_nonempty_output_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (output / "sentinel").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(pipeline.PipelineError, "non-empty"):
                pipeline.ensure_output_directory(output)
            self.assertEqual((output / "sentinel").read_text(encoding="utf-8"), "keep")

    def test_run_id_is_sanitized_and_seeded(self):
        run_id = pipeline.sanitize_run_id(" Gloria run / take #1 ", 831001)
        self.assertRegex(run_id, r"^[A-Za-z0-9._-]+$")
        self.assertIn("Gloria-run-take-1", run_id)
        self.assertIn("831001", run_id)
        self.assertEqual(
            run_id, pipeline.sanitize_run_id(" Gloria run / take #1 ", 831001)
        )

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "value"
            path.write_bytes(b"abc")
            self.assertEqual(
                pipeline.sha256_file(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )

    def test_manifest_success_and_failure_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            runner.manifest["status"] = "success"
            runner.write_manifest()
            saved = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "success")

            runner.current_stage = "generate-yue2"
            runner.record_failure(RuntimeError("generation failed"))
            saved = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["failed_stage"], "generate-yue2")
            self.assertEqual(saved["error"], "generation failed")

    def test_extraction_uses_inspection_duration(self):
        inspection = json.dumps({"nominal_duration_seconds": 12.375})
        duration = pipeline.nominal_duration(inspection)
        command = pipeline.extraction_command(
            Path("full.flac"), Path("accompaniment.wav"), duration
        )
        self.assertEqual(command[command.index("-t") + 1], "12.375")
        self.assertNotIn("accompaniment.abc", command)

    def test_mix_command_contains_prototype_gains_and_no_normalization(self):
        command = pipeline.mix_command(
            Path("vocal.wav"),
            Path("accompaniment.wav"),
            Path("final.wav"),
            0.25,
            4.0,
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("volume=0.25", filters)
        self.assertIn("volume=4", filters)
        self.assertIn("amix=inputs=2:normalize=0", filters)

    def test_manifest_does_not_contain_private_key_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            runner.write_manifest()
            contents = runner.manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE KEY CONTENT", contents)
            self.assertNotIn(str(args.ssh_key), contents)

    def test_failed_ssh_preflight_prevents_melisma_invocation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            commands = []

            def fail_ssh(command, **_kwargs):
                commands.append(command)
                if command[0] == "ssh":
                    raise pipeline.PipelineError(
                        "ssh failed",
                        exit_status=255,
                        stderr="connection refused",
                    )
                return mock.Mock(returncode=0, stdout="", stderr="")

            runner.execute = mock.Mock(side_effect=fail_ssh)
            with mock.patch.object(pipeline.shutil, "which", return_value="/usr/bin/tool"):
                with self.assertRaisesRegex(
                    pipeline.PipelineError,
                    "RunPod SSH preflight failed; no pipeline stages were started",
                ):
                    runner.run()
            self.assertTrue(commands)
            self.assertFalse(any(command[0] == str(args.melisma) for command in commands))

    def test_preflight_ssh_arguments_are_bounded_and_noninteractive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(
                root,
                runpod_host="new-pod.example",
                runpod_port=31234,
                ssh_connect_timeout=7,
            )
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            runner.execute = mock.Mock(
                return_value=mock.Mock(returncode=0, stdout="", stderr="")
            )
            with mock.patch.object(pipeline.shutil, "which", return_value="/usr/bin/tool"):
                runner.preflight()
            command = runner.execute.call_args.args[0]
            self.assertEqual(command[0], "ssh")
            self.assertIn("IdentitiesOnly=yes", command)
            self.assertIn("BatchMode=yes", command)
            self.assertIn("ConnectTimeout=7", command)
            self.assertEqual(command[command.index("-p") + 1], "31234")
            self.assertEqual(command[command.index("-i") + 1], str(args.ssh_key))
            self.assertIn("root@new-pod.example", command)

    def test_existing_failed_output_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            first = pipeline.Pipeline(args)
            first.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(args.midi)
            first.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(args.lyrics)
            first.manifest["status"] = "failed"
            first.manifest["stages"]["preflight"] = {"status": "success"}
            first.manifest["stages"]["melisma-render-inputs"] = {
                "status": "failed",
                "error": "cancelled",
            }
            first.write_manifest()

            args.resume = True
            pipeline.ensure_output_directory(args.output, resume=True)
            resumed = pipeline.Pipeline(args)
            self.assertEqual(resumed.manifest["status"], "running")
            self.assertEqual(
                resumed.manifest["stages"]["melisma-render-inputs"]["status"],
                "failed",
            )

    def test_completed_stage_is_verified_and_not_rerun_on_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root, resume=True)
            args.output.mkdir()
            base_args = self.make_args(root)
            first = pipeline.Pipeline(base_args)
            first.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(args.midi)
            first.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(args.lyrics)
            first.manifest["stages"]["preflight"] = {"status": "success"}
            first.manifest["stages"]["melisma-render-inputs"] = {"status": "success"}
            first.write_manifest()
            resumed = pipeline.Pipeline(args)
            action = mock.Mock()
            validator = mock.Mock()
            resumed.stage("melisma-render-inputs", action, validator)
            validator.assert_called_once_with()
            action.assert_not_called()

    def test_keyboard_interrupt_writes_interrupted_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            argv = [
                "--midi", str(args.midi),
                "--lyrics", str(args.lyrics),
                "--output", str(args.output),
                "--singer", args.singer,
                "--phonemizer", args.phonemizer,
                "--renderer", args.renderer,
                "--style", args.style,
                "--seed", str(args.seed),
                "--runpod-host", args.runpod_host,
                "--runpod-port", str(args.runpod_port),
                "--runpod-user", args.runpod_user,
                "--ssh-key", str(args.ssh_key),
                "--melisma", str(args.melisma),
                "--openutau-project", str(args.openutau_project),
            ]
            with mock.patch.object(pipeline.Pipeline, "run", side_effect=KeyboardInterrupt):
                self.assertEqual(pipeline.main(argv), 130)
            manifest = json.loads(
                (args.output / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["status"], "interrupted")

    def test_remote_run_id_is_stable_across_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            first = pipeline.Pipeline(args)
            first.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(args.midi)
            first.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(args.lyrics)
            first.manifest["remote_run_id"] = "stable-run-831001"
            first.write_manifest()
            args.resume = True
            resumed = pipeline.Pipeline(args)
            self.assertEqual(resumed.manifest["remote_run_id"], "stable-run-831001")

    def test_existing_remote_run_directory_is_tolerated_on_resume(self):
        command = pipeline.create_remote_run_command(
            "/workspace/YuE/runs/stable-run", resume=True
        )
        self.assertEqual(
            command,
            ["mkdir", "-p", "--", "/workspace/YuE/runs/stable-run"],
        )

    def test_prepared_score_uses_yue_contract_with_multiple_abc_files(self):
        prepared = "/workspace/YuE/runs/run/prepared-run/prepared"
        output = json.dumps(
            {
                "prepared": prepared,
                "mode": "melody",
                "nominal_seconds": 10.0,
            }
        )
        score = pipeline.prepared_score_path(prepared, output)
        self.assertEqual(score, f"{prepared}/score.abc")
        other_files = [f"{prepared}/original.abc", f"{prepared}/score.abc"]
        self.assertIn(score, other_files)
        self.assertNotEqual(score, sorted(other_files)[0])

    def test_remote_failure_diagnostics_are_stored_without_key_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            runner.current_stage = "prepare-yue2"
            error = pipeline.PipelineError(
                "remote failed",
                exit_status=17,
                stdout="useful output",
                stderr="useful error",
            )
            runner.record_failure(error)
            manifest_text = runner.manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            stage = manifest["stages"]["prepare-yue2"]
            self.assertEqual(stage["exit_status"], 17)
            self.assertEqual(stage["stdout"], "useful output")
            self.assertEqual(stage["stderr"], "useful error")
            self.assertNotIn("PRIVATE KEY CONTENT", manifest_text)


if __name__ == "__main__":
    unittest.main()
