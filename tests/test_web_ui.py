"""Workflow forms and read-only routes remain non-executing without submission."""

import http.client
import ipaddress
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import asdict, fields as dataclass_fields, replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration import (
    Capability, Configuration, DiscoveryReport, DiscoverySource, Provenance,
    Readiness, SourceIdentity, SourceKind, State, Verification,
)
from tools.orchestration.adapters import FFmpegAdapter
from tools.orchestration.configuration import (
    AsyncAPI, AuthReference, ConfigurationError, ExecutionContract,
    GenerationDefaults, MixSettings, Preset, SSHMetadata, SamplingDefaults,
    YuE2Target, configuration_from_value, configuration_structure, configuration_value,
)
from tools.orchestration.discovery import DiscoveryService, SingerRoot
from tools.orchestration.process import ExpectedArtifact
from tools.web_ui.configuration import configuration_document
from tools.web_ui.server import STATIC, WorkflowHandler, create_server
from tools.web_ui.workflows import workflow_definitions, workflow_document


def dependency_reports():
    source = SourceIdentity("openutau", "/operator/openutau", "test-contract")
    def record(key, value, evidence, provenance=Provenance.FILESYSTEM):
        return Capability(
            source, key, value, provenance,
            Verification.SOURCE if provenance == Provenance.SOURCE else Verification.METADATA,
            State.CONFIGURED if provenance == Provenance.CONFIGURED else State.CANDIDATE,
            evidence=evidence,
        )
    return (DiscoveryReport(source, (
        record("singer", "alpha", (("singer_type", "classic"), ("path", "/authorized/alpha"))),
        record("singer", "beta", (("singer_type", "diffsinger"), ("path", "/authorized/beta"))),
        record("renderer", "first", (("singer_type", "classic"),), Provenance.SOURCE),
        record("renderer", "second", (("singer_type", "diffSinger"),), Provenance.SOURCE),
        record("phonemizer", "alpha-phonemizer", (
            ("singer", "alpha"), ("file", "/authorized/alpha/character.yaml"),
        ), Provenance.CONFIGURED),
        record("phonemizer", "beta-phonemizer", (
            ("singer", "beta"), ("file", "/authorized/beta/character.yaml"),
        ), Provenance.CONFIGURED),
    )),)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        for target in (
            "subprocess.run", "subprocess.Popen",
            "tools.orchestration.discovery.DiscoveryService.__init__",
            "tools.orchestration.discovery.DiscoveryService.discover",
            "tools.orchestration.discovery.DiscoveryService.refresh",
            "tools.orchestration.configuration.load_configuration",
            "tools.orchestration.configuration.save_configuration",
        ):
            patch = mock.patch(target, side_effect=AssertionError(f"forbidden: {target}"))
            patch.start()
            self.addCleanup(patch.stop)


class ControlModelTests(OfflineTests):
    def test_no_workflow_boolean_or_numeric_fields_are_invented(self):
        self.assertTrue(all(
            field.kind not in ("boolean", "number")
            for workflow in workflow_definitions() for field in workflow.fields
        ))

    def test_real_configuration_booleans_and_numeric_metadata(self):
        schema = configuration_document(Configuration())["schema"]
        sources = schema["properties"]["sources"]["items"][0]["properties"]
        root = sources["singer_roots"]["items"][0]["properties"]
        self.assertEqual(root["recursive"]["field"]["kind"], "boolean")
        target = schema["properties"]["yue2_targets"]["items"][0]["properties"]
        port = target["administration"]["properties"]["port"]["field"]
        self.assertEqual((port["minimum"], port["maximum"], port["step"]), (1, 65535, 1))
        defaults = target["contracts"]["items"][0]["properties"]["defaults"]["properties"]
        self.assertEqual(defaults["offline"]["field"]["kind"], "boolean")
        self.assertEqual(defaults["empty_lyrics"]["field"]["kind"], "boolean")
        seed = defaults["seed"]["field"]
        self.assertIsNone(seed["minimum"])
        self.assertIsNone(seed["maximum"])
        self.assertEqual(seed["step"], 1)
        self.assertIn("range is not established", seed["help"])
        top_p = defaults["abc_sampling"]["properties"]["top_p"]["field"]
        self.assertEqual((top_p["minimum"], top_p["maximum"], top_p["step"]), (0, 1, "any"))

    def test_preview_uses_issue4_pure_validation_without_scanning(self):
        source = DiscoverySource(
            "openutau", Path("/explicit/openutau"),
            singer_roots=(SingerRoot(Path("/explicit/singers"), recursive=True),),
        )
        config = Configuration(sources=(source,), workspace=Path("/explicit/workspace"))
        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("read")),
            mock.patch.object(Path, "stat", side_effect=AssertionError("stat")),
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("scan")),
        ):
            document = configuration_document(config)
            checked = configuration_from_value(document["value"])
        self.assertFalse(document["saved"])
        self.assertEqual(checked.sources[0].singer_roots, source.singer_roots)
        self.assertIn("readiness: unknown", document["schema"]["field"]["help"])
        for prohibited in (
            {"api_key": "should-not-be-accepted"},
            {"workspace": "/tmp/../unauthorized"},
            {"sources": [{"tool": "openutau", "root": "/x", "singer_roots": [{"path": "relative"}]}]},
        ):
            with self.subTest(prohibited=prohibited), self.assertRaises(ConfigurationError):
                configuration_from_value(prohibited)

    def test_numeric_constraints_are_enforced_by_the_same_model(self):
        target = YuE2Target(
            "local", "local", (ExecutionContract("native", "native", "0.1.6"),),
            local_root=Path("/explicit/runtime"),
            administration=SSHMetadata("example.invalid", 22, "operator"),
        )
        config = Configuration(yue2_targets=(target,))
        for port in (0, 65536):
            draft = configuration_value(config)
            draft["yue2_targets"][0]["administration"]["port"] = port
            with self.subTest(port=port), self.assertRaises(ConfigurationError):
                configuration_from_value(draft)
        for top_p in (-0.01, 1.01):
            contract = replace(
                target.contracts[0],
                defaults=GenerationDefaults(abc_sampling=SamplingDefaults(top_p=top_p)),
            )
            with self.subTest(top_p=top_p), self.assertRaises(ConfigurationError):
                configuration_value(Configuration(yue2_targets=(replace(target, contracts=(contract,)),)))

    def test_numeric_metadata_exactly_matches_existing_configuration_bounds(self):
        expected = {
            SSHMetadata: {"port": {"minimum": 1, "maximum": 65535}},
            SamplingDefaults: {
                field.name: {"minimum": 0, **({"maximum": 1} if field.name == "top_p" else {})}
                for field in dataclass_fields(SamplingDefaults)
            },
            GenerationDefaults: {
                key: {"exclusive_minimum": 0}
                for key in ("budget", "cfg_scale", "ode_steps", "context")
            },
            MixSettings: {
                key: {"exclusive_minimum": 0}
                for key in ("instrumental_gain", "vocal_gain", "master_gain")
            },
        }
        bounds = ("minimum", "maximum", "exclusive_minimum")
        for model, constraints in expected.items():
            with self.subTest(model=model.__name__):
                schema = configuration_structure(model)["properties"]
                actual = {
                    key: {bound: description[bound] for bound in bounds if bound in description}
                    for key, description in schema.items()
                    if any(bound in description for bound in bounds)
                }
                self.assertEqual(actual, constraints)

    def generation_configuration(self, **defaults):
        return Configuration(yue2_targets=(YuE2Target(
            "local", "local", (ExecutionContract(
                "native", "native", "0.1.6", defaults=GenerationDefaults(**defaults),
            ),), local_root=Path("/explicit/runtime"),
        ),))

    def test_sampling_lower_bounds_and_top_p_upper_bound_use_issue4_validation(self):
        for group in ("abc_sampling", "semantic_sampling"):
            for field in dataclass_fields(SamplingDefaults):
                for value in (0, 1):
                    with self.subTest(group=group, field=field.name, value=value):
                        config = self.generation_configuration(
                            **{group: SamplingDefaults(**{field.name: value})},
                        )
                        checked = configuration_from_value(configuration_value(config))
                        self.assertEqual(getattr(
                            getattr(checked.yue2_targets[0].contracts[0].defaults, group), field.name,
                        ), value)
                with (
                    self.subTest(group=group, field=field.name, value=-1),
                    self.assertRaises(ConfigurationError),
                ):
                    configuration_value(self.generation_configuration(
                        **{group: SamplingDefaults(**{field.name: -1})},
                    ))
            with self.subTest(group=group), self.assertRaises(ConfigurationError):
                configuration_value(self.generation_configuration(
                    **{group: SamplingDefaults(top_p=1.01)},
                ))

    def test_generation_exclusive_bounds_use_issue4_validation_without_invented_maxima(self):
        schema = configuration_structure(GenerationDefaults)["properties"]
        for key in ("budget", "cfg_scale", "ode_steps", "context"):
            self.assertNotIn("maximum", schema[key])
            for invalid in (-1, 0):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ConfigurationError):
                    configuration_value(self.generation_configuration(**{key: invalid}))
            valid = 0.001 if key in ("budget", "cfg_scale") else 1
            with self.subTest(key=key, valid=valid):
                config = self.generation_configuration(**{key: valid})
                self.assertEqual(configuration_from_value(configuration_value(config)), config)
        self.assertNotIn("minimum", schema["seed"])
        self.assertNotIn("maximum", schema["seed"])
        negative_seed = self.generation_configuration(seed=-1)
        self.assertEqual(configuration_from_value(configuration_value(negative_seed)), negative_seed)

    def test_ssh_port_inclusive_endpoints_are_valid_configuration(self):
        config = self.generation_configuration()
        for port in (1, 65535):
            with self.subTest(port=port):
                target = replace(
                    config.yue2_targets[0],
                    administration=SSHMetadata("example.invalid", port, "operator"),
                )
                checked = replace(config, yue2_targets=(target,))
                self.assertEqual(configuration_from_value(configuration_value(checked)), checked)

    def test_all_mix_gains_follow_existing_positive_finite_adapter_contract(self):
        adapter = FFmpegAdapter(Path("/explicit/ffmpeg"), ROOT)
        preset = Preset(
            "gain-audit", Path("input.mid"), Path("lyrics.txt"), (),
            "local", "instrumental", GenerationDefaults(),
        )
        for key in ("instrumental_gain", "vocal_gain", "master_gain"):
            for invalid in (-1, -0.0, 0, float("inf"), float("nan")):
                gains = asdict(MixSettings())
                gains[key] = invalid
                with self.subTest(key=key, invalid=invalid):
                    config = Configuration(presets=(replace(preset, mix=MixSettings(**gains)),))
                    with self.assertRaises(ConfigurationError):
                        configuration_value(config)
                    draft = configuration_value(Configuration(presets=(preset,)))
                    draft["presets"][0]["mix"][key] = invalid
                    with self.assertRaises(ConfigurationError):
                        configuration_from_value(draft)
                    with self.assertRaises(ValueError):
                        adapter.build(
                            Path("/explicit/instrumental.wav"), Path("/explicit/vocal.wav"),
                            Path("/explicit/mix.wav"), **gains,
                        )
            for valid in (0.001, 2.0):
                gains = asdict(MixSettings())
                gains[key] = valid
                config = Configuration(presets=(replace(preset, mix=MixSettings(**gains)),))
                with self.subTest(key=key, valid=valid):
                    self.assertEqual(configuration_from_value(configuration_value(config)), config)
                    with (
                        mock.patch("tools.orchestration.adapters.existing_input", side_effect=lambda p, cwd: p),
                        mock.patch("tools.orchestration.adapters.new_output", side_effect=lambda p, cwd: p),
                    ):
                        operation = adapter.build(
                            Path("/explicit/instrumental.wav"), Path("/explicit/vocal.wav"),
                            Path("/explicit/mix.wav"), **gains,
                        )
                    self.assertIn(f"volume={valid!r}", operation.command.arguments[8])

    def test_editor_numeric_projection_and_validation_remain_non_io(self):
        config = self.generation_configuration(
            budget=0.001, abc_sampling=SamplingDefaults(top_p=0),
        )
        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("file I/O")),
            mock.patch.object(Path, "stat", side_effect=AssertionError("filesystem check")),
            mock.patch("socket.create_connection", side_effect=AssertionError("network I/O")),
            mock.patch("tools.orchestration.configuration._atomic_write", side_effect=AssertionError("write")),
        ):
            value = configuration_value(config)
            self.assertEqual(configuration_from_value(value), config)
            self.assertEqual(configuration_structure(MixSettings)["properties"]["vocal_gain"]
                             ["exclusive_minimum"], 0)

    def test_file_metadata_and_path_limitations(self):
        for workflow in workflow_definitions()[1:3]:
            midi, lyrics, output = workflow.fields[:3]
            self.assertEqual(midi.accept, ".mid,.midi")
            self.assertEqual(lyrics.accept, ".txt")
            for field in (midi, lyrics):
                self.assertEqual(field.selection, "existing-file")
                self.assertIn("not a reusable absolute host path", field.path_limitation)
                self.assertIn("not read or uploaded", field.path_limitation)
            self.assertEqual(output.selection, "output-name")
            self.assertIn("absolute local path", output.path_limitation)
            self.assertIn("without overwrite", output.path_limitation)

    def test_dependencies_use_explicit_singer_metadata_and_do_not_relabel(self):
        workflow = workflow_definitions(reports=dependency_reports())[2]
        singer, phonemizer, renderer = workflow.fields[3:]
        for field in (phonemizer, renderer):
            self.assertEqual(field.dependency.parent, singer.key)
            first, second = field.dependency.bindings
            self.assertEqual((first.choices, second.choices), ((0,), (1,)))
            self.assertEqual(first.state, State.SUPPORTED)
            self.assertIn("not runtime compatibility or readiness", first.reason)
            self.assertEqual(field.default, "")
            self.assertIn(Provenance.MANUAL, field.provenance)
            self.assertTrue(all(item.readiness == Readiness.UNKNOWN for item in field.capabilities))

    def test_missing_cross_source_and_configured_dependency_evidence_is_unknown(self):
        report, = dependency_reports()
        for parent in (
            replace(report.capabilities[0], evidence=()),
            replace(report.capabilities[0], provenance=Provenance.CONFIGURED),
            replace(report.capabilities[0], source=replace(report.source, location="/another/source")),
        ):
            with self.subTest(parent=parent):
                reports = (DiscoveryReport(report.source, (parent, *report.capabilities[2:])),)
                renderer = workflow_definitions(reports=reports)[2].fields[5]
                binding, = renderer.dependency.bindings
                self.assertEqual(binding.choices, (0, 1))
                self.assertEqual(binding.state, State.UNKNOWN)
                self.assertIn("retained", binding.reason)

    def test_unsupported_choices_are_retained_as_unknown_not_validated(self):
        report, = dependency_reports()
        failed = replace(
            report.capabilities[2], state=State.UNAVAILABLE,
            readiness=Readiness.UNAVAILABLE, reason="not available",
        )
        report = replace(report, capabilities=(*report.capabilities[:2], failed, *report.capabilities[3:]))
        renderer = workflow_definitions(reports=(report,))[2].fields[5]
        self.assertEqual(renderer.capabilities[0], failed)
        self.assertEqual(renderer.dependency.bindings[1].choices, (0, 1))
        self.assertEqual(renderer.dependency.bindings[1].state, State.UNKNOWN)


class WorkflowTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.workflows = {item.key: item for item in workflow_definitions()}

    def test_all_eight_workflows(self):
        self.assertEqual(
            [item.label for item in self.workflows.values()],
            ["Generate MIDI", "Generate ABC", "Generate USTX", "Render Vocals",
             "Generate YuE2 Audio", "Mix Audio", "Play Audio", "Full Pipeline"],
        )

    def test_midi_generation_unresolved(self):
        midi = self.workflows["generate-midi"]
        self.assertFalse(midi.available)
        self.assertEqual(midi.state, State.UNRESOLVED)
        self.assertIn("unresolved/deferred", midi.reason)
        self.assertIn("existing compatible MIDI", midi.reason)
        self.assertIsNone(midi.operation)

    def test_required_and_optional_fields(self):
        for kind in ("abc", "ustx"):
            with self.subTest(kind=kind):
                workflow = self.workflows[f"generate-{kind}"]
                self.assertTrue(workflow.available)
                self.assertEqual(workflow.state, State.SUPPORTED)
                self.assertEqual(workflow.operation, kind)
                self.assertEqual(
                    [field.key for field in workflow.fields if field.required],
                    ["midi", "lyrics", f"{kind}_output"],
                )
                self.assertEqual(
                    [field.key for field in workflow.fields if not field.required],
                    ["singer", "phonemizer", "renderer"] if kind == "ustx" else [],
                )
                self.assertEqual(
                    [field.kind for field in workflow.fields[:3]],
                    ["input-path", "input-path", "output-path"],
                )

    def test_ustx_metadata_empty_and_unverified(self):
        for field in self.workflows["generate-ustx"].fields[3:]:
            self.assertEqual(field.default, "")
            self.assertEqual(field.configured, ())
            self.assertEqual(
                field.provenance, (Provenance.CONFIGURED, Provenance.MANUAL),
            )
            self.assertEqual(field.verification, Verification.UNVERIFIED)

    def test_in_memory_configuration_is_not_discovery_or_a_default(self):
        source = DiscoverySource(
            "openutau", ROOT, configured=(("singer", "operator-entered-value"),),
        )
        config = Configuration(sources=(source,))
        workflow = next(
            item for item in workflow_definitions(config) if item.key == "generate-ustx"
        )
        singer = workflow.fields[3]
        self.assertEqual(singer.default, "")
        selection, = singer.configured
        self.assertEqual(selection.source, source.identity)
        self.assertEqual(selection.provenance, Provenance.CONFIGURED)
        self.assertEqual(selection.value, "operator-entered-value")
        self.assertEqual(singer.verification, Verification.UNVERIFIED)

    def test_unavailable_workflows_have_reasons_and_no_fields(self):
        unavailable = [item for item in self.workflows.values() if not item.available]
        self.assertEqual(len(unavailable), 6)
        for item in unavailable:
            with self.subTest(workflow=item.key):
                self.assertTrue(item.reason.strip())
                self.assertEqual(item.fields, ())
                self.assertEqual(item.outputs, ())
                self.assertIsNone(item.operation)

    def test_abc_artifact_has_future_inspection_views_not_actions(self):
        output, = self.workflows["generate-abc"].outputs
        self.assertEqual(output.field, "abc_output")
        self.assertEqual(output.kind, "abc")
        self.assertTrue(output.inspectable)
        self.assertEqual(
            output.expected_artifact(Path("not-created.abc")),
            ExpectedArtifact(Path("not-created.abc"), "abc"),
        )
        self.assertEqual([view.key for view in output.views], ["raw-text", "score"])
        self.assertTrue(all(
            view.state == State.UNAVAILABLE and view.reason for view in output.views
        ))
        self.assertIn("no rendering dependency", output.views[1].reason)

    def test_definitions_do_not_supply_executable_commands(self):
        document = workflow_document()
        self.assertFalse(document["execution_enabled"])

        def inspect(value):
            if isinstance(value, dict):
                self.assertTrue(
                    {"command", "argv", "executable", "action", "rendering_dependency"}
                    .isdisjoint(value),
                )
                for item in value.values():
                    inspect(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    inspect(item)

        inspect(document)
        json.dumps(document)

    def test_definitions_do_not_read_or_scan_paths(self):
        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("file read")),
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("scan")),
            mock.patch.object(Path, "glob", side_effect=AssertionError("scan")),
        ):
            self.assertEqual(len(workflow_document()["workflows"]), 8)


class CapabilityProjectionTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.source = SourceIdentity("openutau", "/operator/openutau", "test-contract")

    def capability(self, key="singer", value="reported-value", **kwargs):
        return Capability(
            kwargs.pop("source", self.source), key, value,
            kwargs.pop("provenance", Provenance.FILESYSTEM),
            kwargs.pop("verification", Verification.METADATA),
            kwargs.pop("state", State.CANDIDATE),
            kwargs.pop("readiness", Readiness.UNKNOWN),
            **kwargs,
        )

    def workflows(self, *capabilities):
        report = DiscoveryReport(self.source, capabilities)
        return {
            item.key: item for item in workflow_definitions(reports=(report,))
        }

    def test_explicit_reports_reuse_records_without_discovery(self):
        capability = self.capability()
        report = DiscoveryReport(self.source, (capability,))
        workflow = next(
            item for item in workflow_definitions(reports=(report,))
            if item.key == "generate-ustx"
        )
        self.assertIs(workflow.reports[0], report)
        self.assertIs(workflow.fields[3].capabilities[0], capability)
        self.assertEqual(workflow.readiness, Readiness.UNKNOWN)
        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("file read")),
            mock.patch.object(Path, "iterdir", side_effect=AssertionError("scan")),
            mock.patch.object(Path, "glob", side_effect=AssertionError("scan")),
            mock.patch.object(Path, "rglob", side_effect=AssertionError("scan")),
        ):
            self.assertEqual(
                workflow_document(reports=(report,))["workflows"][2]["fields"][3]
                ["capabilities"], (asdict(capability),),
            )

    def test_absent_and_empty_reports_do_not_validate_or_disable_forms(self):
        for reports in ((), (DiscoveryReport(self.source, ()),)):
            with self.subTest(reports=reports):
                document = workflow_document(reports=reports)
                for workflow in document["workflows"]:
                    self.assertEqual(workflow["readiness"], Readiness.UNKNOWN)
                    self.assertTrue(workflow["readiness_reason"])
                    if workflow["key"] in ("generate-abc", "generate-ustx"):
                        self.assertTrue(workflow["available"])
                    for field in workflow["fields"]:
                        self.assertEqual(field["capabilities"], ())

    def test_state_and_readiness_are_independent(self):
        for state in (State.SUPPORTED, State.CONFIGURED, State.CANDIDATE):
            for readiness in (
                Readiness.UNKNOWN, Readiness.UNAVAILABLE, Readiness.UNAUTHORIZED,
                Readiness.NOT_APPLICABLE,
            ):
                with self.subTest(state=state, readiness=readiness):
                    record = self.capability(state=state, readiness=readiness)
                    projected = self.workflows(record)["generate-ustx"].fields[3].capabilities[0]
                    self.assertEqual(projected.state, state)
                    self.assertEqual(projected.readiness, readiness)
                    self.assertNotEqual(projected.readiness, Readiness.READY)

    def test_ready_is_only_preserved_from_existing_observed_ready_record(self):
        record = self.capability(
            state=State.READY, readiness=Readiness.READY,
            verification=Verification.OBSERVED,
        )
        workflows = self.workflows(record)
        projected = workflows["generate-ustx"].fields[3].capabilities[0]
        self.assertIs(projected, record)
        self.assertEqual(projected.state, State.READY)
        self.assertEqual(projected.readiness, Readiness.READY)
        self.assertEqual(projected.verification, Verification.OBSERVED)
        self.assertEqual(workflows["generate-ustx"].readiness, Readiness.UNKNOWN)
        self.assertFalse(workflows["render-vocals"].available)
        for state, readiness, verification in (
            (State.READY, Readiness.UNKNOWN, Verification.OBSERVED),
            (State.SUPPORTED, Readiness.READY, Verification.OBSERVED),
            (State.READY, Readiness.READY, Verification.METADATA),
        ):
            with self.subTest(state=state, readiness=readiness, verification=verification):
                with self.assertRaises(ValueError):
                    self.capability(
                        state=state, readiness=readiness, verification=verification,
                    )

    def test_unknown_and_failure_states_keep_existing_reasons(self):
        for state in (
            State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED,
            State.ERROR, State.UNRESOLVED,
        ):
            with self.subTest(state=state):
                record = self.capability(
                    state=state, value=None, reason=f"existing {state.value} reason",
                    verification=Verification.UNVERIFIED,
                )
                workflows = self.workflows(record)
                projected = workflows["generate-ustx"].fields[3].capabilities[0]
                self.assertIs(projected, record)
                self.assertEqual(projected.reason, f"existing {state.value} reason")
                self.assertEqual(projected.state, state)
                self.assertEqual(projected.readiness, Readiness.UNKNOWN)
                self.assertTrue(workflows["generate-ustx"].available)

    def test_configured_origins_and_defaults_are_not_promoted(self):
        for provenance in (
            Provenance.CONFIGURED, Provenance.MANUAL, Provenance.REFERENCE,
            Provenance.PREVIOUS, Provenance.PROJECT,
        ):
            with self.subTest(provenance=provenance):
                record = self.capability(
                    provenance=provenance, state=State.CONFIGURED,
                    verification=Verification.UNVERIFIED, default=True,
                    origin=Provenance.REFERENCE, reason="unvalidated selection",
                )
                projected = self.workflows(record)["generate-ustx"].fields[3].capabilities[0]
                self.assertEqual(projected, record)
                self.assertEqual(projected.provenance, provenance)
                self.assertEqual(projected.origin, Provenance.REFERENCE)
                self.assertEqual(projected.state, State.CONFIGURED)
                self.assertEqual(projected.readiness, Readiness.UNKNOWN)
                self.assertEqual(projected.verification, Verification.UNVERIFIED)
                self.assertTrue(projected.default)

    def test_reported_values_only_and_manual_entry_remains(self):
        singer = self.capability(default=True)
        renderer = self.capability(
            "renderer", "source-renderer", state=State.SUPPORTED,
            provenance=Provenance.SOURCE, verification=Verification.SOURCE,
        )
        other = self.capability("unrelated", "not-a-singer-choice")
        workflow = self.workflows(singer, renderer, other)["generate-ustx"]
        fields = {field.key: field for field in workflow.fields}
        self.assertEqual(fields["singer"].capabilities, (singer,))
        self.assertEqual(fields["renderer"].capabilities, (renderer,))
        self.assertEqual(fields["phonemizer"].capabilities, ())
        for key in ("singer", "phonemizer", "renderer"):
            field = fields[key]
            self.assertEqual(field.kind, "text")
            self.assertEqual(field.default, "")
            self.assertFalse(field.required)
            self.assertIn(Provenance.MANUAL, field.provenance)
            self.assertIn(Provenance.CONFIGURED, field.provenance)

    def test_configured_text_and_reported_choices_coexist_without_merging(self):
        configured_source = DiscoverySource(
            "openutau", ROOT, configured=(("singer", "configured-only"),),
        )
        record = self.capability()
        report = DiscoveryReport(self.source, (record,))
        workflow = next(
            item for item in workflow_definitions(
                Configuration(sources=(configured_source,)), reports=(report,),
            ) if item.key == "generate-ustx"
        )
        singer = workflow.fields[3]
        self.assertEqual(singer.default, "")
        self.assertEqual(singer.kind, "text")
        self.assertEqual(singer.capabilities, (record,))
        self.assertEqual(singer.configured[0].value, "configured-only")
        self.assertEqual(singer.configured[0].provenance, Provenance.CONFIGURED)
        self.assertNotEqual(singer.configured[0].source, record.source)

    def test_all_capability_metadata_survives_json_serialization(self):
        source = SourceIdentity(
            "openutau", "operator-remote-source", "source-contract", SourceKind.REMOTE,
        )
        record = self.capability(
            source=source, value="value with spaces", default=True,
            origin=Provenance.REFERENCE,
            reason="original reason", evidence=(("metadata", "operator evidence"),),
        )
        report = DiscoveryReport(source, (record,))
        document = json.loads(json.dumps(workflow_document(reports=(report,))))
        workflow = next(
            item for item in document["workflows"] if item["key"] == "generate-ustx"
        )
        expected = json.loads(json.dumps(asdict(record)))
        self.assertEqual(workflow["reports"][0]["source"], asdict(source))
        self.assertEqual(workflow["reports"][0]["capabilities"], [expected])
        self.assertEqual(workflow["fields"][3]["capabilities"], [expected])
        self.assertEqual(expected["state"], "candidate")
        self.assertEqual(expected["readiness"], "unknown")
        self.assertEqual(expected["verification"], "metadata")
        self.assertEqual(expected["provenance"], "filesystem")
        self.assertEqual(expected["reason"], "original reason")
        self.assertEqual(expected["value"], "value with spaces")
        self.assertTrue(expected["default"])
        self.assertEqual(expected["source"]["kind"], "remote")

    def test_reports_are_source_scoped_and_native_instrumental_remain_separate(self):
        reports = tuple(
            DiscoveryReport(
                SourceIdentity(tool, f"/operator/{tool}", f"{tool}-contract"),
                (self.capability("renderer", f"{tool}-renderer", source=SourceIdentity(
                    tool, f"/operator/{tool}", f"{tool}-contract",
                )),),
            )
            for tool in ("melisma", "openutau", "yue2-native", "yue2-instrumental")
        )
        workflows = {
            item.key: item for item in workflow_definitions(reports=reports)
        }
        ustx = workflows["generate-ustx"]
        self.assertEqual(ustx.fields[5].capabilities, reports[1].capabilities)
        yue2 = workflows["generate-yue2-audio"]
        self.assertEqual(yue2.reports, reports[2:])
        self.assertFalse(yue2.available)
        self.assertEqual(yue2.readiness, Readiness.UNKNOWN)

    def test_capability_records_never_enable_unimplemented_workflows(self):
        baseline = {item.key: item for item in workflow_definitions()}
        reports = []
        for tool in (
            "melisma", "openutau", "yue2-native", "yue2-instrumental",
            "ffmpeg", "ffprobe", "ffplay",
        ):
            source = SourceIdentity(tool, f"/operator/{tool}", "test-contract")
            reports.append(DiscoveryReport(source, (self.capability(
                "operation", "reported-operation", source=source, state=State.READY,
                readiness=Readiness.READY, verification=Verification.OBSERVED,
            ),)))
        for workflow in workflow_definitions(reports=tuple(reports)):
            original = baseline[workflow.key]
            self.assertTrue(workflow.reports)
            self.assertEqual(workflow.available, original.available)
            self.assertEqual(workflow.state, original.state)
            self.assertEqual(workflow.reason, original.reason)
            self.assertEqual(workflow.outputs, original.outputs)
            self.assertEqual(workflow.operation, original.operation)
            self.assertEqual(workflow.readiness, Readiness.UNKNOWN)

    def test_no_executable_commands_or_actions_with_capabilities(self):
        record = self.capability()
        document = workflow_document(
            reports=(DiscoveryReport(self.source, (record,)),),
        )
        self.assertFalse(document["execution_enabled"])

        def inspect(value):
            if isinstance(value, dict):
                self.assertTrue({"command", "argv", "executable", "action"}.isdisjoint(value))
                for item in value.values():
                    inspect(item)
            elif isinstance(value, (tuple, list)):
                for item in value:
                    inspect(item)

        inspect(document)


class ServerTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.logging = mock.patch.object(WorkflowHandler, "log_message")
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.server = create_server(0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            *self.server.server_address, timeout=5,
        )
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_loopback_default_and_live_endpoint(self):
        self.assertTrue(ipaddress.ip_address(self.server.server_address[0]).is_loopback)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, headers, body = self.request("GET", "/api/workflows")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body), json.loads(json.dumps(workflow_document())))

    def test_default_constructor_cannot_choose_a_public_host(self):
        with mock.patch("tools.web_ui.server.WorkflowServer") as server:
            create_server()
        server.assert_called_once_with(8765, configuration=None, reports=())

    def test_default_endpoint_has_unknown_unsupplied_evidence(self):
        document = json.loads(self.request("GET", "/api/workflows")[2])
        for workflow in document["workflows"]:
            self.assertEqual(workflow["reports"], [])
            self.assertEqual(workflow["readiness"], "unknown")
            for field in workflow["fields"]:
                self.assertEqual(field["capabilities"], [])

    def test_explicit_reports_are_serialized_at_startup_not_on_requests(self):
        source = SourceIdentity("openutau", "/operator/openutau", "test-contract")
        capability = Capability(
            source, "renderer", "reported-renderer", Provenance.SOURCE,
            Verification.SOURCE, State.SUPPORTED, reason="runtime not validated",
        )
        reports = (DiscoveryReport(source, (capability,)),)
        self.stop_server()
        self.server = create_server(0, reports=reports)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        expected = json.loads(json.dumps(workflow_document(reports=reports)))
        with mock.patch(
            "tools.web_ui.server.workflow_document",
            side_effect=AssertionError("request must only serve the startup snapshot"),
        ):
            for _ in range(2):
                status, _, body = self.request("GET", "/api/workflows")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), expected)
            self.assertEqual(self.request("POST", "/api/workflows", "{}")[0], 405)
        self.assertTrue(ipaddress.ip_address(self.server.server_address[0]).is_loopback)

    def test_json_endpoint_is_read_only(self):
        before = self.request("GET", "/api/workflows")[2]
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, body = self.request(
                    method, "/api/workflows", '{"workflows":[]}',
                )
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
                self.assertEqual(body, b"")
        self.assertEqual(self.request("GET", "/api/workflows")[2], before)

    def test_static_routes_and_head(self):
        for route, filename, content_type in (
            ("/", "index.html", "text/html"),
            ("/style.css", "style.css", "text/css"),
            ("/app.js", "app.js", "text/javascript"),
        ):
            with self.subTest(route=route):
                status, headers, body = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith(content_type))
                self.assertEqual(body, (STATIC / filename).read_bytes())
                self.assertIn("form-action 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, headers, body = self.request("HEAD", "/api/workflows")
        self.assertEqual(status, 200)
        self.assertGreater(int(headers["Content-Length"]), 0)
        self.assertEqual(body, b"")

    def test_arbitrary_paths_and_execution_routes_not_served(self):
        for path in (
            "/README.md", "/../README.md", "/%2e%2e/README.md",
            "/static/", "/api/run", "/api/configuration", "/api/artifacts",
            "/api/workflows?path=/etc/passwd",
            "/api/files", "/api/browse", "/api/upload", "/api/configuration/save",
            "/api/execute",
        ):
            with (
                self.subTest(path=path),
                mock.patch.object(
                    Path, "read_bytes", side_effect=AssertionError("arbitrary file read"),
                ),
            ):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_missing_static_asset_is_an_explicit_server_error(self):
        with (
            mock.patch.object(Path, "read_bytes", side_effect=OSError("missing")),
            mock.patch.object(WorkflowHandler, "log_error") as log,
        ):
            self.assertEqual(self.request("GET", "/app.js")[0], 500)
        log.assert_called()

    def restart(self, **kwargs):
        self.stop_server()
        self.server = create_server(0, **kwargs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def post(self, path="/api/capabilities/refresh", body="{}", headers=None):
        return self.request("POST", path, body, {
            "Content-Type": "application/json",
            "X-UI-CSRF": self.server.csrf,
            **(headers or {}),
        })

    def test_ui_configuration_schema_is_separate_from_workflow_projection(self):
        status, _, body = self.request("GET", "/api/ui")
        self.assertEqual(status, 200)
        ui = json.loads(body)
        self.assertEqual(ui["csrf"], self.server.csrf)
        self.assertEqual(ui["configuration"]["value"], configuration_value(Configuration()))
        self.assertFalse(ui["configuration"]["saved"])
        self.assertEqual(ui["refresh"]["state"], "unknown")
        self.assertEqual(self.request("GET", "/api/configuration")[0], 404)
        self.assertEqual(self.request("GET", "/api/ui", headers={"Host": "external.invalid"})[0], 403)

    def test_refresh_replaces_stale_reports_and_separates_yue2_contracts(self):
        sources = tuple(
            DiscoverySource(tool, Path("/explicit/yue2"))
            for tool in ("yue2-native", "yue2-instrumental")
        )
        reports = tuple(DiscoveryReport(source.identity, (Capability(
            source.identity, "model", f"fresh-{source.tool}", Provenance.CONFIGURED,
            Verification.UNVERIFIED, State.CONFIGURED,
        ),)) for source in sources)
        stale = (DiscoveryReport(SourceIdentity("melisma", "/stale", "stale"), ()),)
        refresh = mock.Mock(return_value=reports)
        self.restart(configuration=Configuration(sources=sources), reports=stale, refresh=refresh)
        refresh.assert_not_called()
        status, _, body = self.post()
        self.assertEqual(status, 200)
        refresh.assert_called_once_with(sources)
        self.assertEqual(self.server.reports, reports)
        document = json.loads(body)
        yue2 = document["workflows"][4]
        self.assertEqual(
            [report["source"]["tool"] for report in yue2["reports"]],
            ["yue2-native", "yue2-instrumental"],
        )
        self.assertEqual(
            [report["source"]["contract"] for report in yue2["reports"]],
            ["0.1.6", "0.1.5"],
        )
        self.assertNotIn("/stale", body.decode())
        self.assertFalse(document["execution_enabled"])
        self.assertEqual(json.loads(self.request("GET", "/api/workflows")[2]), document)

    def test_refresh_failure_replaces_stale_readiness_with_explicit_errors(self):
        source = DiscoverySource("openutau", Path("/explicit/openutau"))
        ready = Capability(
            source.identity, "singer", "old", Provenance.FILESYSTEM,
            Verification.OBSERVED, State.READY, Readiness.READY,
        )
        refresh = mock.Mock(side_effect=OSError("sensitive exception details must not be echoed"))
        self.restart(
            configuration=Configuration(sources=(source,)),
            reports=(DiscoveryReport(source.identity, (ready,)),), refresh=refresh,
        )
        status, _, body = self.post()
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["refresh"]["state"], "error")
        capability, = document["workflows"][2]["reports"][0]["capabilities"]
        self.assertEqual(capability["state"], "error")
        self.assertEqual(capability["readiness"], "unknown")
        self.assertIn("refresh failed", capability["reason"])
        self.assertNotIn("sensitive exception details", body.decode())
        self.assertNotIn('"ready"', body.decode())
        self.assertTrue(document["workflows"][2]["available"])

    def test_refresh_rejects_unconfigured_or_missing_report_identities(self):
        source = DiscoverySource("melisma", Path("/explicit/melisma"))
        foreign = DiscoveryReport(SourceIdentity("melisma", "/unauthorized", "x"), ())
        for reports in ((foreign,), (), (foreign, foreign)):
            with self.subTest(reports=reports):
                self.restart(
                    configuration=Configuration(sources=(source,)),
                    refresh=mock.Mock(return_value=reports),
                )
                document = json.loads(self.post()[2])
                self.assertEqual(document["refresh"]["state"], "error")
                self.assertNotIn("/unauthorized", json.dumps(document))

    def test_no_sources_refresh_does_not_invent_sources(self):
        refresh = mock.Mock(return_value=())
        self.restart(refresh=refresh)
        document = json.loads(self.post()[2])
        refresh.assert_called_once_with(())
        self.assertIn("No configured discovery sources", document["refresh"]["reason"])
        self.assertTrue(all(not workflow["reports"] for workflow in document["workflows"]))

    def test_refresh_malformed_and_cross_origin_requests_never_call_boundary(self):
        refresh = mock.Mock(return_value=())
        self.restart(refresh=refresh)
        for body in ("", "[]", "0", "{", '{"sources":[]}'):
            with self.subTest(body=body):
                self.assertEqual(self.post(body=body)[0], 400)
        for headers, status in (
            ({"X-UI-CSRF": ""}, 403),
            ({"X-UI-CSRF": "\u00e9"}, 403),
            ({"Origin": "https://external.invalid"}, 403),
            ({"Host": "external.invalid"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "text/plain"}, 400),
            ({"Content-Length": "-2"}, 400),
            ({"Content-Length": "1000000"}, 400),
            ({"Transfer-Encoding": "chunked"}, 400),
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.post(headers=headers)[0], status)
        refresh.assert_not_called()

    def test_concurrent_refresh_is_rejected_without_a_second_discovery(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(sources):
            entered.set()
            if not release.wait(4):
                raise RuntimeError("test refresh release timed out")
            return ()
        refresh = mock.Mock(side_effect=blocked)
        self.restart(refresh=refresh)
        results = []
        first = threading.Thread(target=lambda: results.append(self.post()), daemon=True)
        first.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.post()[0], 409)
            self.assertEqual(self.request("GET", "/api/workflows")[0], 200)
            refresh.assert_called_once_with(())
        finally:
            release.set()
            first.join(timeout=5)
        self.assertEqual(results[0][0], 200)
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(refresh.call_count, 2)

    def test_configuration_preview_never_mutates_active_sources_or_saves(self):
        source = DiscoverySource("melisma", Path("/active/root"))
        refresh = mock.Mock(return_value=(DiscoveryReport(source.identity, ()),))
        self.restart(configuration=Configuration(sources=(source,)), refresh=refresh)
        draft = configuration_value(Configuration(
            sources=(DiscoverySource("melisma", Path("/edited/not-authorized")),),
        ))
        before = self.server.workflow_json
        with (
            mock.patch.object(Path, "open", side_effect=AssertionError("filesystem read/write")),
            mock.patch.object(Path, "stat", side_effect=AssertionError("filesystem check")),
            mock.patch("tools.web_ui.server.configuration_from_value", wraps=configuration_from_value) as validate,
        ):
            status, _, body = self.post("/api/configuration-preview", json.dumps(draft))
        self.assertEqual(status, 200)
        self.assertEqual(validate.call_count, 1)
        result = json.loads(body)
        self.assertTrue(result["valid"])
        self.assertFalse(result["saved"])
        self.assertEqual(self.server.workflow_json, before)
        self.assertEqual(self.server.configuration.sources, (source,))
        self.post()
        refresh.assert_called_once_with((source,))

    def test_configuration_preview_rejects_secrets_without_echo_or_log(self):
        forbidden = "Bearer private-test-value"
        for draft in (
            {"api_key": forbidden},
            {"workspace": forbidden},
            {"sources": [{"tool": "melisma", "root": "/root", "configured": [["api_key", forbidden]]}]},
            {"workspace": "/relative/../parent"},
        ):
            with (
                self.subTest(draft=draft),
                mock.patch.object(WorkflowHandler, "log_message") as log,
            ):
                status, _, body = self.post("/api/configuration-preview", json.dumps(draft))
                self.assertEqual(status, 422)
                self.assertNotIn(forbidden, body.decode())
                self.assertNotIn(forbidden, str(log.call_args_list))
                self.assertFalse(json.loads(body)["saved"])
        self.assertEqual(
            self.post("/api/configuration-preview", '{"workspace":null,"workspace":null}')[0], 400,
        )
        self.assertEqual(self.post("/api/configuration-preview", "{" * 2000)[0], 400)
        self.assertEqual(self.post("/api/configuration-preview", " " * 131073)[0], 400)

    def test_validation_only_remote_configuration_is_not_contacted(self):
        target = YuE2Target(
            "gpu", "remote-http",
            (ExecutionContract("native", "native", "0.1.6"), ExecutionContract("instrumental", "pinned", "0.1.5")),
            endpoint="https://must-not-contact.invalid",
            api=AsyncAPI("jobs", "v1"),
            auth=AuthReference("environment", "OPERATOR_AUTH_REFERENCE"),
        )
        draft = configuration_value(Configuration(yue2_targets=(target,)))
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError("remote connection")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("remote DNS")),
        ):
            # Use a direct handler-independent validation call while sockets are forbidden.
            checked = configuration_from_value(draft)
            preview = configuration_document(checked)
        self.assertEqual(preview["value"]["yue2_targets"][0]["endpoint"], target.endpoint)
        self.assertFalse(preview["saved"])
        self.assertNotIn("ready", json.dumps(preview["value"]))


class ReadOnlyRefreshTests(unittest.TestCase):
    def test_exact_discovery_service_boundary_and_authorized_sources(self):
        from tools.web_ui.server import refresh_capabilities
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            singers = root / "authorized-singers"
            singers.mkdir()
            source = DiscoverySource(
                "openutau", root / "source",
                singer_roots=(SingerRoot(singers, recursive=False),),
            )
            config = Configuration(
                sources=(source,),
                yue2_targets=(YuE2Target(
                    "gpu", "remote-http", (ExecutionContract("native", "native", "0.1.6"),),
                    endpoint="https://never-contact.invalid", api=AsyncAPI("jobs", "v1"),
                ),),
            )
            original_refresh = DiscoveryService.refresh
            with (
                mock.patch("subprocess.run", side_effect=AssertionError("execute")),
                mock.patch("subprocess.Popen", side_effect=AssertionError("execute")),
                mock.patch("socket.create_connection", side_effect=AssertionError("remote")),
                mock.patch("tools.orchestration.configuration.save_configuration", side_effect=AssertionError("save")),
                mock.patch("tools.orchestration.configuration._atomic_write", side_effect=AssertionError("write")),
                mock.patch("tools.orchestration.adapters.ToolAdapter.execute", side_effect=AssertionError("adapter")),
                mock.patch("tools.orchestration.discovery.os.walk", return_value=()) as walk,
                mock.patch.object(
                    DiscoveryService, "refresh", autospec=True, side_effect=original_refresh,
                ) as refreshed,
            ):
                server = create_server(0, configuration=config)
                try:
                    refreshed.assert_not_called()
                    document = server.refresh()
                finally:
                    server.server_close()
                refreshed.assert_called_once()
                service, actual_sources = refreshed.call_args.args
                self.assertIsNone(service.probe)
                self.assertEqual(actual_sources, config.sources)
                walk.assert_called_once()
                self.assertEqual(walk.call_args.args[0], singers)
                self.assertFalse(walk.call_args.kwargs["followlinks"])
                self.assertEqual(document["workflows"][2]["reports"][0]["source"]["location"], str(source.root))
                self.assertNotIn("never-contact", json.dumps(document))


class BrowserControlTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("gjs"), "optional existing GJS runtime not available")
    def test_dynamic_browser_controls_and_dependencies(self):
        fixture = {
            "document": workflow_document(reports=dependency_reports()),
            "configuration": configuration_document(Configuration(
                sources=(DiscoverySource(
                    "openutau", Path("/explicit/source"),
                    singer_roots=(SingerRoot(Path("/explicit/singers")),),
                ),),
            )),
        }
        script = (ROOT / "tests/web_ui_controls.js").read_text()
        app = (STATIC / "app.js").read_text()
        setup, assertions = script.split("// APPLICATION_INSERTION_POINT")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".js") as script_file:
            script_file.write(setup + "\nconst fixture = " + json.dumps(fixture) + ";\n" + app + assertions)
            script_file.flush()
            result = subprocess.run(
                ["gjs", script_file.name], capture_output=True, text=True, timeout=10, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_file_content_reads_uploads_or_frontend_tool_enumerations(self):
        script = (STATIC / "app.js").read_text()
        for forbidden in (
            "FileReader", ".arrayBuffer(", ".text(", "FormData", "readAs",
            "webkitRelativePath", "webkitdirectory", "showDirectoryPicker",
            "TIGER", "DIFFSINGER", "torch", "vllm", "0.1.5", "0.1.6",
        ):
            self.assertNotIn(forbidden, script)
        self.assertIn("file.name", script)
        self.assertNotIn("input.files[0].", script)
        self.assertEqual(script.count('method: "POST"'), 1)
        self.assertIn('post("/api/capabilities/refresh", {})', script)
        self.assertIn('post("/api/configuration-preview", draft)', script)


if __name__ == "__main__":
    unittest.main()
