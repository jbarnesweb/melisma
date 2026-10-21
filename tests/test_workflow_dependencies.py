"""Issue #8 dependency definitions, without stale state or execution planning."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration.adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from tools.orchestration.artifact_catalog import ARTIFACT_TYPES, semantic_type
from tools.orchestration.artifacts import inspect_artifact
from tools.orchestration.playback import FFplayAdapter
from tools.orchestration.yue2 import InstrumentalAdapter, NativeYuE2Adapter
from tools.web_ui.workflows import (
    StageDependency, accepted_artifact_types, direct_downstream, direct_upstream,
    stage_dependencies, transitive_downstream, workflow_definitions, workflow_document,
)


class WorkflowDependencyTests(unittest.TestCase):
    def test_direct_relationships_and_reverse_queries(self):
        expected = {
            "generate-midi": (),
            "generate-abc": (),
            "generate-ustx": (),
            "render-vocals": ("generate-ustx",),
            "generate-yue2-audio": ("generate-abc",),
            "mix-audio": ("generate-yue2-audio", "render-vocals"),
            "play-audio": ("generate-yue2-audio", "mix-audio", "render-vocals"),
            "full-pipeline": (),
        }
        self.assertEqual({item.key for item in workflow_definitions()}, set(expected))
        for stage, upstream in expected.items():
            with self.subTest(stage=stage):
                self.assertEqual(direct_upstream(stage), upstream)
                self.assertEqual(direct_downstream(stage), tuple(sorted(
                    consumer for consumer, producers in expected.items() if stage in producers
                )))

    def test_transitive_relationships_and_independent_branches(self):
        for source, intermediate in (
            ("generate-abc", "generate-yue2-audio"),
            ("generate-ustx", "render-vocals"),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    set(transitive_downstream(source)),
                    {intermediate, "mix-audio", "play-audio"},
                )
        self.assertEqual(transitive_downstream("mix-audio"), ("play-audio",))
        self.assertEqual(transitive_downstream("play-audio"), ())
        for left in ("generate-ustx", "render-vocals"):
            for right in ("generate-abc", "generate-yue2-audio"):
                self.assertNotIn(right, transitive_downstream(left))
                self.assertNotIn(left, transitive_downstream(right))

    def test_edges_use_existing_workflows_and_accepted_semantic_types(self):
        workflows = {item.key: item for item in workflow_definitions()}
        for stage, workflow in workflows.items():
            self.assertNotIn(stage, transitive_downstream(stage), "graph must be acyclic")
            self.assertEqual(len(workflow.stage_dependencies), len(set(workflow.stage_dependencies)))
            for edge in workflow.stage_dependencies:
                with self.subTest(stage=stage, edge=edge):
                    self.assertIn(edge.upstream, workflows)
                    self.assertIn(edge.artifact_type, ARTIFACT_TYPES)
                    self.assertIn(edge.artifact_type, accepted_artifact_types(stage, edge.input))

    def test_ustx_is_a_separate_render_input_not_abc_or_bundle_prerequisite(self):
        self.assertEqual(stage_dependencies("render-vocals"), (
            StageDependency("generate-ustx", "ustx", "ustx"),
        ))
        self.assertEqual(stage_dependencies("generate-ustx"), ())
        self.assertEqual(stage_dependencies("generate-abc"), ())
        self.assertNotIn("generate-ustx", transitive_downstream("render-vocals"))

    def test_mix_uses_vocal_and_instrumental_not_native_song_audio(self):
        self.assertEqual(stage_dependencies("mix-audio"), (
            StageDependency("render-vocals", "vocal", "vocal-audio"),
            StageDependency("generate-yue2-audio", "instrumental", "instrumental-audio"),
        ))
        self.assertNotIn("yue2-audio", {
            edge.artifact_type for edge in stage_dependencies("mix-audio")
        })
        self.assertIn(
            StageDependency("generate-yue2-audio", "audio", "yue2-audio"),
            stage_dependencies("play-audio"),
        )

    def test_definitions_project_unchanged_to_backend_document(self):
        document = workflow_document()
        for workflow in document["workflows"]:
            self.assertEqual(workflow["stage_dependencies"], tuple(
                {"upstream": edge.upstream, "input": edge.input, "artifact_type": edge.artifact_type}
                for edge in stage_dependencies(workflow["key"])
            ))
        self.assertFalse(document["execution_enabled"])

    def test_unknown_stages_fail_explicitly(self):
        for query in (stage_dependencies, direct_upstream, direct_downstream, transitive_downstream):
            with self.subTest(query=query.__name__), self.assertRaisesRegex(ValueError, "Unknown"):
                query("not-a-workflow")

    def test_edges_match_actual_adapter_inputs_and_catalog_output_types(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {}
            for name in ("input.mid", "lyrics.txt", "input.ustx", "input.abc",
                         "vocal.wav", "instrumental.flac"):
                paths[name] = root / name
                paths[name].write_text("fixture", encoding="utf-8")
            executable = Path(sys.executable)
            melisma = MelismaAdapter(executable, root)
            abc = melisma.build("abc", paths["input.mid"], paths["lyrics.txt"], root / "out.abc")
            ustx = melisma.build(
                "ustx", paths["input.mid"], paths["lyrics.txt"], root / "out.ustx",
                singer="selected-singer",
            )
            render = OpenUtauAdapter(executable, root).build(paths["input.ustx"], root / "out.wav")
            native_adapter = NativeYuE2Adapter(executable, root)
            native_settings = dict(
                style="piano", lyrics="", run_id="test", seed=1, model=root / "model",
                vae=root / "vae", device="cpu", cot="full",
            )
            native = native_adapter.build(root / "native", abc=paths["input.abc"], **native_settings)
            native_without_score = native_adapter.build(root / "unscored", **native_settings)
            helper = InstrumentalAdapter(executable, root)
            prepared = helper.prepare(
                paths["input.abc"], root / "prepare", style="piano", run_id="test", seed=1,
            )
            manifest = root / "prepared" / "prepared-manifest.json"
            manifest.parent.mkdir()
            manifest.write_text("{}")
            instrumental = helper.generate(
                manifest.parent, root / "instrumental", models_root=root / "models", device="cpu",
            )
            mix = FFmpegAdapter(executable, root).build(
                paths["instrumental.flac"], paths["vocal.wav"], root / "mix.wav",
                instrumental_gain=1, vocal_gain=0.5, master_gain=0.7,
                duration="first", normalize=False,
            )
            play = FFplayAdapter(executable, root).build(paths["vocal.wav"])
            self.assertEqual([item.role for item in abc.inputs], ["midi", "lyrics"])
            self.assertEqual(abc.inputs, ustx.inputs)
            self.assertEqual(native_without_score.inputs, ())
            self.assertNotIn("--singer", render.command.arguments)
            consumers = {
                "render-vocals": (render,),
                "generate-yue2-audio": (native, prepared),
                "mix-audio": (mix,),
                "play-audio": (play,),
            }
            producers = {
                ("generate-abc", "abc"): abc,
                ("generate-ustx", "ustx"): ustx,
                ("render-vocals", "vocal-audio"): render,
                ("generate-yue2-audio", "yue2-audio"): native,
                ("generate-yue2-audio", "instrumental-audio"): instrumental,
                ("mix-audio", "mixed-audio"): mix,
            }
            for stage, operations in consumers.items():
                for edge in stage_dependencies(stage):
                    with self.subTest(stage=stage, edge=edge):
                        for operation in operations:
                            source, = (item for item in operation.inputs if item.role == edge.input)
                            # FFplay accepts generic audio; workflow compatibility
                            # preserves the catalog's more specific audio types.
                            self.assertEqual(source.type, "audio" if stage == "play-audio" else edge.artifact_type)
                        producer = producers[edge.upstream, edge.artifact_type]
                        self.assertIn(edge.artifact_type, {
                            semantic_type(producer, inspect_artifact(output))
                            for output in producer.expected
                        })


if __name__ == "__main__":
    unittest.main()
