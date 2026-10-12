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
            "vocal_gain": 0.5,
            "accompaniment_gain": 1.0,
            "master_gain": 0.707945784,
            "accompaniment_conformance_anchors": None,
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
            self.assertEqual(
                saved["configuration"]["master_gain"],
                0.707945784,
            )

            runner.current_stage = "generate-yue2"
            runner.record_failure(RuntimeError("generation failed"))
            saved = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["failed_stage"], "generate-yue2")
            self.assertEqual(saved["error"], "generation failed")

    def test_final_mix_measurements_persist_on_success_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            runner.execute = mock.Mock(
                return_value=mock.Mock(
                    returncode=0,
                    stdout="",
                    stderr=(
                        "mean_volume: -12.7 dB\n"
                        "max_volume: -3.2 dB\n"
                    ),
                )
            )
            runner.analyze_final_mix(root / "final.wav")
            runner.write_manifest()
            saved = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["final_mix_analysis"],
                {"mean_volume_db": -12.7, "max_volume_db": -3.2},
            )

            runner.execute.return_value = mock.Mock(
                returncode=0,
                stdout="",
                stderr="mean_volume: -6.1 dB\nmax_volume: 0.0 dB\n",
            )
            runner.current_stage = "validate-final-mix"
            with self.assertRaises(pipeline.PipelineError) as raised:
                runner.analyze_final_mix(root / "final.wav")
            runner.record_failure(raised.exception)
            saved = json.loads(runner.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(
                saved["final_mix_analysis"],
                {"mean_volume_db": -6.1, "max_volume_db": 0.0},
            )
            self.assertEqual(
                saved["stages"]["validate-final-mix"]["status"],
                "failed",
            )

    def test_extraction_uses_inspection_duration(self):
        inspection = json.dumps({"nominal_duration_seconds": 12.375})
        duration = pipeline.nominal_duration(inspection)
        command = pipeline.extraction_command(
            Path("full.flac"), Path("accompaniment.wav"), duration
        )
        self.assertEqual(command[command.index("-t") + 1], "12.375")
        self.assertNotIn("accompaniment.abc", command)

    def light_in_the_ruins_anchors(self):
        return [
            (0.0, 0.0),
            (8.094854, 8.0),
            (12.162854, 12.0),
            (16.263666, 16.0),
            (20.190603, 20.0),
            (24.0, 24.0),
        ]

    def test_conformance_anchor_validation_and_ratios(self):
        segments = pipeline.conformance_segments(
            self.light_in_the_ruins_anchors(),
            24.0,
        )
        self.assertEqual(len(segments), 5)
        self.assertAlmostEqual(
            segments[0]["atempo_ratio"],
            8.094854 / 8.0,
        )
        self.assertAlmostEqual(
            segments[0]["stretch_ratio"],
            8.0 / 8.094854,
        )
        self.assertAlmostEqual(
            segments[-1]["atempo_ratio"],
            (24.0 - 20.190603) / 4.0,
        )

    def test_conformance_rejects_invalid_anchors_and_ratios(self):
        invalid_cases = (
            ([(1.0, 0.0), (24.0, 24.0)], "begin at 0"),
            ([(0.0, 0.0), (8.0, 8.0), (7.0, 12.0), (24.0, 24.0)], "source times"),
            ([(0.0, 0.0), (8.0, 8.0), (12.0, 7.0), (24.0, 24.0)], "destination times"),
            ([(0.0, 0.0), (0.0, 8.0), (24.0, 24.0)], "source times"),
            ([(0.0, 0.0), (8.0, 0.0), (24.0, 24.0)], "destination times"),
            ([(0.0, 0.0), (23.0, 24.0)], "source anchor"),
            ([(0.0, 0.0), (24.0, 23.0)], "destination anchor"),
            ([(0.0, 0.0), (24.0, 4.0), (24.0, 24.0)], "supported range"),
        )
        for anchors, message in invalid_cases:
            with self.subTest(anchors=anchors):
                with self.assertRaisesRegex(pipeline.PipelineError, message):
                    pipeline.conformance_segments(anchors, 24.0)

    def test_conformance_filter_graph_is_piecewise_and_score_bounded(self):
        segments = pipeline.conformance_segments(
            self.light_in_the_ruins_anchors(),
            24.0,
        )
        graph = pipeline.conformance_filter_graph(segments, 24.0)
        self.assertEqual(graph.count("atrim=start="), 6)
        self.assertEqual(graph.count("asetpts=PTS-STARTPTS"), 6)
        self.assertEqual(graph.count("atempo="), 5)
        self.assertIn("atrim=start=0:end=8.094854", graph)
        self.assertIn("[segment0][segment1][segment2][segment3][segment4]", graph)
        self.assertIn("concat=n=5:v=0:a=1[concatenated]", graph)
        self.assertIn("apad=whole_dur=24", graph)
        self.assertTrue(graph.endswith("asetpts=PTS-STARTPTS[out]"))

    def test_manual_anchor_file_is_explicit_json_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "anchors.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "source_seconds": source,
                            "destination_seconds": destination,
                        }
                        for source, destination in self.light_in_the_ruins_anchors()
                    ]
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                pipeline.manual_conformance_anchors(path),
                self.light_in_the_ruins_anchors(),
            )

    def test_disabled_conformance_preserves_source_for_mix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            source = root / "accompaniment.wav"
            source.write_bytes(b"source")
            runner = pipeline.Pipeline(args)
            runner.execute = mock.Mock()
            runner.conform_accompaniment(
                source,
                root / "accompaniment-conformed.wav",
                24.0,
            )
            runner.execute.assert_not_called()
            state = runner.manifest["accompaniment_conformance"]
            self.assertFalse(state["enabled"])
            self.assertFalse(state["succeeded"])
            self.assertEqual(state["source"]["path"], str(source))
            self.assertIsNone(state["conformed"])
            self.assertEqual(
                pipeline.accompaniment_for_mix(
                    source,
                    root / "accompaniment-conformed.wav",
                    enabled=state["enabled"],
                    succeeded=state["succeeded"],
                ),
                source,
            )

    def test_enabled_conformance_requires_success_for_mix(self):
        source = Path("accompaniment.wav")
        conformed = Path("accompaniment-conformed.wav")
        with self.assertRaisesRegex(pipeline.PipelineError, "did not succeed"):
            pipeline.accompaniment_for_mix(
                source,
                conformed,
                enabled=True,
                succeeded=False,
            )
        self.assertEqual(
            pipeline.accompaniment_for_mix(
                source,
                conformed,
                enabled=True,
                succeeded=True,
            ),
            conformed,
        )

    def test_successful_conformance_records_manifest_representation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchors_path = root / "anchors.json"
            anchors_path.write_text(
                json.dumps(
                    [
                        {
                            "source_seconds": source,
                            "destination_seconds": destination,
                        }
                        for source, destination in self.light_in_the_ruins_anchors()
                    ]
                ),
                encoding="utf-8",
            )
            args = self.make_args(
                root,
                accompaniment_conformance_anchors=anchors_path,
            )
            args.output.mkdir()
            source = root / "accompaniment.wav"
            conformed = root / "accompaniment-conformed.wav"
            source.write_bytes(b"source")
            runner = pipeline.Pipeline(args)

            def execute(command, **_kwargs):
                if command[0] == "ffmpeg":
                    conformed.write_bytes(b"conformed")
                if command[0] == "ffprobe" and "format=duration" in command:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps({"format": {"duration": "24.000000"}}),
                        stderr="",
                    )
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"streams": [{"codec_type": "audio"}]}),
                    stderr="",
                )

            runner.execute = mock.Mock(side_effect=execute)
            runner.conform_accompaniment(source, conformed, 24.0)
            state = runner.manifest["accompaniment_conformance"]
            self.assertTrue(state["enabled"])
            self.assertTrue(state["succeeded"])
            self.assertEqual(state["source"]["sha256"], pipeline.sha256_file(source))
            self.assertEqual(
                state["conformed"]["sha256"],
                pipeline.sha256_file(conformed),
            )
            self.assertEqual(state["anchors"][1]["source_seconds"], 8.094854)
            self.assertEqual(len(state["segments"]), 5)
            self.assertEqual(state["conformed_duration_seconds"], 24.0)
            self.assertEqual(state["nominal_score_duration_seconds"], 24.0)

    def test_conformance_failure_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchors_path = root / "anchors.json"
            anchors_path.write_text("[]", encoding="utf-8")
            args = self.make_args(
                root,
                accompaniment_conformance_anchors=anchors_path,
            )
            args.output.mkdir()
            source = root / "accompaniment.wav"
            source.write_bytes(b"source")
            runner = pipeline.Pipeline(args)
            runner.current_stage = "conform-accompaniment"
            with self.assertRaisesRegex(pipeline.PipelineError, "at least two"):
                runner.stage(
                    "conform-accompaniment",
                    lambda: runner.conform_accompaniment(
                        source,
                        root / "accompaniment-conformed.wav",
                        24.0,
                    ),
                )
            self.assertFalse(
                runner.manifest["accompaniment_conformance"]["succeeded"]
            )
            self.assertEqual(
                runner.manifest["stages"]["conform-accompaniment"]["status"],
                "failed",
            )

    def test_mix_command_contains_safe_default_gains(self):
        command = pipeline.mix_command(
            Path("vocal.wav"),
            Path("accompaniment.wav"),
            Path("final.wav"),
            0.5,
            1.0,
            0.707945784,
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertEqual(
            filters,
            "[0:a]volume=1.0[accompaniment];"
            "[1:a]volume=0.5[vocal];"
            "[accompaniment][vocal]"
            "amix=inputs=2:duration=first:normalize=0[mix];"
            "[mix]volume=0.707945784[out]",
        )
        self.assertEqual(
            command[command.index("-i") + 1],
            "accompaniment.wav",
        )
        self.assertEqual(command[command.index("-map") + 1], "[out]")

    def test_mix_command_uses_custom_gains(self):
        command = pipeline.mix_command(
            Path("vocal.wav"),
            Path("accompaniment.wav"),
            Path("final.wav"),
            0.75,
            1.25,
            0.8,
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:a]volume=1.25[accompaniment]", filters)
        self.assertIn("[1:a]volume=0.75[vocal]", filters)
        self.assertIn("[mix]volume=0.8[out]", filters)

    def test_invalid_gains_are_rejected(self):
        for value in ("0", "-1", "NaN", "inf", "-inf", "not-a-number"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    pipeline.finite_positive_float(value)

    def test_gain_cli_defaults_are_safe_prototype_values(self):
        parser = pipeline.build_parser()
        defaults = {
            action.dest: action.default
            for action in parser._actions
            if action.dest in {
                "accompaniment_gain",
                "vocal_gain",
                "master_gain",
            }
        }
        self.assertEqual(
            defaults,
            {
                "accompaniment_gain": 1.0,
                "vocal_gain": 0.5,
                "master_gain": 0.707945784,
            },
        )

    def test_volumedetect_parses_numeric_values(self):
        output = (
            "[Parsed_volumedetect_0] mean_volume: -12.7 dB\n"
            "[Parsed_volumedetect_0] max_volume: -3.2 dB\n"
        )
        self.assertEqual(
            pipeline.parse_volumedetect(output),
            {"mean_volume_db": -12.7, "max_volume_db": -3.2},
        )

    def test_volumedetect_parses_zero_and_negative_infinity(self):
        self.assertEqual(
            pipeline.parse_volumedetect(
                "mean_volume: -inf dB\nmax_volume: 0.0 dB\n"
            ),
            {"mean_volume_db": "-inf", "max_volume_db": 0.0},
        )

    def test_volumedetect_rejects_missing_or_unexpected_output(self):
        for output in (
            "",
            "mean_volume: -12.0 dB\n",
            "mean_volume: quiet dB\nmax_volume: -3.0 dB\n",
        ):
            with self.subTest(output=output):
                with self.assertRaises(pipeline.PipelineError):
                    pipeline.parse_volumedetect(output)

    def test_zero_db_peak_fails_validation(self):
        with self.assertRaisesRegex(
            pipeline.PipelineError,
            "final mix reached 0.0 dBFS and may be clipped",
        ):
            pipeline.validate_final_mix_analysis(
                {"mean_volume_db": -6.1, "max_volume_db": 0.0}
            )

    def test_negative_peak_passes_validation(self):
        pipeline.validate_final_mix_analysis(
            {"mean_volume_db": -12.7, "max_volume_db": -3.2}
        )
        pipeline.validate_final_mix_analysis(
            {"mean_volume_db": "-inf", "max_volume_db": "-inf"}
        )

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

    def make_successful_unconformed_run(self, root: Path):
        args = self.make_args(root)
        args.output.mkdir()
        runner = pipeline.Pipeline(args)
        runner.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(args.midi)
        runner.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(args.lyrics)
        runner.manifest["nominal_score_duration_seconds"] = 24.0
        for name in pipeline.STAGE_NAMES:
            runner.manifest["stages"][name] = {"status": "success"}
        runner.manifest["stages"].pop("conform-accompaniment")
        runner.manifest["configuration"].pop(
            "accompaniment_conformance_enabled", None
        )
        runner.manifest["configuration"].pop(
            "accompaniment_conformance_anchors", None
        )
        runner.manifest["configuration"].pop(
            "accompaniment_conformance_anchors_sha256", None
        )
        runner.manifest["inputs"].pop("conformance_anchors_sha256", None)
        runner.manifest.pop("accompaniment_conformance", None)
        runner.manifest["final_mix_analysis"] = {
            "mean_volume_db": -12.0,
            "max_volume_db": -3.0,
        }
        runner.manifest["artifacts"]["final.wav"] = {
            "path": str(args.output / "final.wav"),
            "sha256": "old-final",
        }
        runner.write_manifest()
        return args

    def write_conformance_anchors(self, path: Path, anchors=None):
        if anchors is None:
            anchors = self.light_in_the_ruins_anchors()
        path.write_text(
            json.dumps(
                [
                    {
                        "source_seconds": source,
                        "destination_seconds": destination,
                    }
                    for source, destination in anchors
                ]
            ),
            encoding="utf-8",
        )

    def test_resume_can_enable_conformance_without_rerunning_upstream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_args = self.make_successful_unconformed_run(root)
            anchors_path = root / "anchors.json"
            self.write_conformance_anchors(anchors_path)
            resumed = pipeline.Pipeline(
                self.make_args(
                    root,
                    resume=True,
                    accompaniment_conformance_anchors=anchors_path,
                )
            )

            upstream = pipeline.STAGE_NAMES[
                : pipeline.STAGE_NAMES.index("conform-accompaniment")
            ]
            for name in upstream:
                with self.subTest(stage=name):
                    action = mock.Mock()
                    validator = mock.Mock()
                    resumed.stage(name, action, validator)
                    validator.assert_called_once_with()
                    action.assert_not_called()

            self.assertTrue(resumed.conformance_downstream_resume)
            for name in upstream:
                self.assertEqual(
                    resumed.manifest["stages"][name]["status"],
                    "success",
                )
            for name in (
                "conform-accompaniment",
                "mix-final",
                "validate-final-mix",
            ):
                self.assertEqual(
                    resumed.manifest["stages"][name]["status"],
                    "pending",
                )

            source = base_args.output / "yue2" / "accompaniment.wav"
            conformed = base_args.output / "yue2" / "accompaniment-conformed.wav"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original accompaniment")

            def execute(command, **_kwargs):
                if command[0] == "ffmpeg":
                    conformed.write_bytes(b"conformed accompaniment")
                if command[0] == "ffprobe" and "format=duration" in command:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps({"format": {"duration": "24.0"}}),
                        stderr="",
                    )
                return mock.Mock(
                    returncode=0,
                    stdout=json.dumps({"streams": [{"codec_type": "audio"}]}),
                    stderr="",
                )

            resumed.execute = mock.Mock(side_effect=execute)
            resumed.stage(
                "conform-accompaniment",
                lambda: resumed.conform_accompaniment(
                    source,
                    conformed,
                    24.0,
                ),
            )
            mix_action = mock.Mock()
            validation_action = mock.Mock()
            resumed.stage("mix-final", mix_action)
            resumed.stage("validate-final-mix", validation_action)
            mix_action.assert_called_once_with()
            validation_action.assert_called_once_with()

            configuration = resumed.manifest["configuration"]
            self.assertTrue(configuration["accompaniment_conformance_enabled"])
            self.assertEqual(
                configuration["accompaniment_conformance_anchors"],
                str(anchors_path),
            )
            self.assertEqual(
                configuration["accompaniment_conformance_anchors_sha256"],
                pipeline.sha256_file(anchors_path),
            )
            state = resumed.manifest["accompaniment_conformance"]
            self.assertTrue(state["succeeded"])
            self.assertEqual(len(state["anchors"]), 6)
            self.assertEqual(len(state["segments"]), 5)
            self.assertEqual(
                pipeline.accompaniment_for_mix(
                    source,
                    conformed,
                    enabled=state["enabled"],
                    succeeded=state["succeeded"],
                ),
                conformed,
            )
            self.assertEqual(source.read_bytes(), b"original accompaniment")
            self.assertNotIn("final_mix_analysis", resumed.manifest)

    def test_resume_can_enable_explicitly_disabled_conformance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = self.make_args(root)
            args.output.mkdir()
            runner = pipeline.Pipeline(args)
            for name in pipeline.STAGE_NAMES:
                runner.manifest["stages"][name] = {"status": "success"}
            runner.write_manifest()
            anchors_path = root / "anchors.json"
            self.write_conformance_anchors(anchors_path)
            resumed = pipeline.Pipeline(
                self.make_args(
                    root,
                    resume=True,
                    accompaniment_conformance_anchors=anchors_path,
                )
            )
            self.assertTrue(resumed.conformance_downstream_resume)
            self.assertTrue(
                resumed.manifest["configuration"][
                    "accompaniment_conformance_enabled"
                ]
            )
            for name in (
                "conform-accompaniment",
                "mix-final",
                "validate-final-mix",
            ):
                self.assertEqual(
                    resumed.manifest["stages"][name]["status"],
                    "pending",
                )

    def make_enabled_conformance_manifest(
        self,
        root: Path,
        anchors_path: Path,
    ):
        args = self.make_args(
            root,
            accompaniment_conformance_anchors=anchors_path,
        )
        args.output.mkdir()
        runner = pipeline.Pipeline(args)
        anchors_sha256 = pipeline.sha256_file(anchors_path)
        runner.manifest["configuration"][
            "accompaniment_conformance_anchors_sha256"
        ] = anchors_sha256
        runner.manifest["inputs"]["conformance_anchors_sha256"] = anchors_sha256
        runner.write_manifest()
        return args

    def test_resume_rejects_enabled_to_disabled_conformance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchors_path = root / "anchors.json"
            self.write_conformance_anchors(anchors_path)
            self.make_enabled_conformance_manifest(root, anchors_path)
            with self.assertRaisesRegex(pipeline.PipelineError, "cannot disable"):
                pipeline.Pipeline(self.make_args(root, resume=True))

    def test_resume_rejects_changed_conformance_anchor_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "anchors.json"
            replacement = root / "replacement.json"
            self.write_conformance_anchors(original)
            self.write_conformance_anchors(replacement)
            self.make_enabled_conformance_manifest(root, original)
            with self.assertRaisesRegex(pipeline.PipelineError, "anchor path"):
                pipeline.Pipeline(
                    self.make_args(
                        root,
                        resume=True,
                        accompaniment_conformance_anchors=replacement,
                    )
                )

    def test_resume_rejects_changed_conformance_anchor_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            anchors_path = root / "anchors.json"
            self.write_conformance_anchors(anchors_path)
            self.make_enabled_conformance_manifest(root, anchors_path)
            self.write_conformance_anchors(
                anchors_path,
                [(0.0, 0.0), (12.0, 11.5), (24.0, 24.0)],
            )
            with self.assertRaisesRegex(pipeline.PipelineError, "anchor contents"):
                pipeline.Pipeline(
                    self.make_args(
                        root,
                        resume=True,
                        accompaniment_conformance_anchors=anchors_path,
                    )
                )

    def test_resume_conformance_transition_rejects_invalid_upstream_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_args = self.make_successful_unconformed_run(root)
            accompaniment = base_args.output / "yue2" / "accompaniment.wav"
            accompaniment.parent.mkdir(parents=True)
            accompaniment.write_bytes(b"original")
            manifest = json.loads(
                (base_args.output / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["artifacts"]["accompaniment.wav"] = {
                "path": str(accompaniment),
                "sha256": pipeline.sha256_file(accompaniment),
            }
            (base_args.output / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            accompaniment.write_bytes(b"modified")
            anchors_path = root / "anchors.json"
            self.write_conformance_anchors(anchors_path)
            resumed = pipeline.Pipeline(
                self.make_args(
                    root,
                    resume=True,
                    accompaniment_conformance_anchors=anchors_path,
                )
            )
            action = mock.Mock()
            with self.assertRaisesRegex(
                pipeline.PipelineError,
                "upstream stage verification failed: extract-accompaniment",
            ):
                resumed.stage(
                    "extract-accompaniment",
                    action,
                    lambda: resumed.verify_artifact(
                        "accompaniment.wav",
                        accompaniment,
                    ),
                )
            action.assert_not_called()
            self.assertEqual(
                resumed.manifest["stages"]["extract-accompaniment"]["status"],
                "success",
            )
            self.assertEqual(resumed.current_stage, "conform-accompaniment")

    def test_final_mix_validation_resume_states(self):
        for status, should_run in (
            ("missing", True),
            ("failed", True),
            ("success", False),
        ):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    base_args = self.make_args(root)
                    base_args.output.mkdir()
                    first = pipeline.Pipeline(base_args)
                    first.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(
                        base_args.midi
                    )
                    first.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(
                        base_args.lyrics
                    )
                    for name in pipeline.STAGE_NAMES:
                        first.manifest["stages"][name] = {"status": "success"}
                    if status == "missing":
                        del first.manifest["stages"]["validate-final-mix"]
                    else:
                        first.manifest["stages"]["validate-final-mix"] = {
                            "status": status
                        }
                    first.write_manifest()

                    resume_args = self.make_args(root, resume=True)
                    resumed = pipeline.Pipeline(resume_args)
                    action = mock.Mock()
                    validator = mock.Mock()
                    resumed.stage("validate-final-mix", action, validator)
                    validator.assert_called_once_with()
                    if should_run:
                        action.assert_called_once_with()
                    else:
                        action.assert_not_called()

    def test_successful_validation_reruns_if_final_artifact_is_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base_args = self.make_args(root)
            base_args.output.mkdir()
            first = pipeline.Pipeline(base_args)
            first.manifest["inputs"]["midi_sha256"] = pipeline.sha256_file(
                base_args.midi
            )
            first.manifest["inputs"]["lyrics_sha256"] = pipeline.sha256_file(
                base_args.lyrics
            )
            for name in pipeline.STAGE_NAMES:
                first.manifest["stages"][name] = {"status": "success"}
            first.write_manifest()

            resumed = pipeline.Pipeline(self.make_args(root, resume=True))
            action = mock.Mock()
            validator = mock.Mock(side_effect=pipeline.PipelineError("hash mismatch"))
            resumed.stage("validate-final-mix", action, validator)
            action.assert_called_once_with()

    def test_resume_rejects_changed_gain_configuration(self):
        for gain, changed in (
            ("vocal_gain", 0.75),
            ("accompaniment_gain", 1.25),
            ("master_gain", 0.9),
        ):
            with self.subTest(gain=gain):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    args = self.make_args(root)
                    args.output.mkdir()
                    first = pipeline.Pipeline(args)
                    first.write_manifest()
                    with self.assertRaisesRegex(
                        pipeline.PipelineError,
                        f"configuration {gain} does not match",
                    ):
                        pipeline.Pipeline(
                            self.make_args(
                                root,
                                resume=True,
                                **{gain: changed},
                            )
                        )

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
