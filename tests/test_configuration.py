"""Issue #4: temporary local metadata, never real installations or remote hosts."""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.orchestration import (
    AsyncAPI, AuthReference, Capability, Configuration, ConfigurationError,
    DiscoveryReport, DiscoverySource, ExecutionContract, GenerationDefaults,
    MelismaAdapter, MixSettings, ModelIdentity, Provenance, Readiness,
    SSHMetadata, SamplingDefaults, SavedSelection, SingerRoot, SourceKind,
    State, UnsupportedVersion, Verification, YuE2Target, gloria_preset,
    load_configuration, load_preset, save_configuration, save_preset,
    validate_configuration,
)
from tools.orchestration.configuration import SCHEMA_VERSION
from tools.orchestration.discovery import CONTRACTS


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "configuration.json"
        self.file("melisma/src/main.c", (
            'enum ml_output_format format = ML_OUTPUT_IR;\n'
            '"ir" "abc" "ustx" "render-inputs" ML_OUTPUT_ABC ML_OUTPUT_USTX'
        ))
        self.file("melisma/src/midi.c", "ml_read_midi")
        self.file("melisma/bin/melisma", "must not execute").chmod(0o755)
        self.file("melisma/examples/five-notes.mid", "fixture MIDI path only")
        self.file("melisma/examples/gloria.txt", "Glo - ri - a -- --")
        self.file("openutau/OpenUtau.Core/Render/Renderers.cs", '''
public const string DIFFSINGER = "DIFFSINGER";
static readonly string[] diffSingerRenderers = new[] { DIFFSINGER };
''')
        self.file("openutau/bin/openutau", "must not execute").chmod(0o755)
        self.file("singers/TIGER_DS_v102/character.txt", "name=TIGER")
        self.file("singers/TIGER_DS_v102/character.yaml", (
            "singer_type: diffsinger\n"
            "default_phonemizer: OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer\n"
        ))
        self.file("yue2/pyproject.toml", (
            '[project]\nversion = "0.1.6"\ndependencies = []\n'
        ))
        self.file("yue2/src/yue2/cli.py", '''
p.add_argument("--cot", choices=("full", "melody", "off"))
MODEL = "m-a-p/YuE2-3B"
VAE = "m-a-p/" + name
raise RuntimeError("never import source")
''')
        self.file("yue2/src/yue2/protocol.py", '''
class SongRequest:
    seed: int = 831001
    cot: str = "full"
''')
        self.file("yue2/src/yue2/pipeline.py", 'device = "cuda" if ready else "cpu"')
        self.melisma = DiscoverySource(
            "melisma", self.root / "melisma", self.root / "melisma/bin/melisma",
            contract=CONTRACTS["melisma"],
        )
        self.openutau = DiscoverySource(
            "openutau", self.root / "openutau", self.root / "openutau/bin/openutau",
            contract=CONTRACTS["openutau"],
            singer_roots=(SingerRoot(self.root / "singers", False),),
        )
        self.yue2 = DiscoverySource("yue2-native", self.root / "yue2", contract="0.1.6")
        self.native = ExecutionContract(
            "native", "yue2-native-v1", "0.1.6", runtime="native-runtime",
            model=ModelIdentity("operator/native-model", "revision-native"),
            vae=ModelIdentity("operator/native-vae"),
            defaults=GenerationDefaults(
                seed=42, cot="melody", backend="torch", offline=True,
                abc_sampling=SamplingDefaults(temperature=0.7, top_p=0.9, top_k=30),
            ),
        )
        self.helper = ExecutionContract(
            "instrumental", "pinned-instrumental-helper", "0.1.5",
            runtime="separate-pinned-runtime",
            model=ModelIdentity("operator/pinned-model", "pinned-model-revision", "a" * 64),
            vae=ModelIdentity("operator/pinned-vae", "pinned-vae-revision"),
            pinned_models_root="/operator/remote/models",
            defaults=GenerationDefaults(seed=831001, composer="agent", empty_lyrics=True),
        )
        self.target = YuE2Target(
            "gpu-primary", "remote-http", (self.native, self.helper),
            endpoint="https://offline.example.invalid/yue2",
            api=AsyncAPI(
                "operator-job-contract", "1",
                submit_path="/jobs", status_path="/jobs/{job_id}",
                result_path="/jobs/{job_id}/result",
                artifacts_path="/jobs/{job_id}/artifacts/{artifact_id}",
            ),
            auth=AuthReference("environment", "YUE2_ACCESS_TOKEN"),
        )
        self.preset = gloria_preset(
            openutau=self.openutau.identity, target_id=self.target.id,
        )
        self.config = Configuration(
            (self.melisma, self.openutau, self.yue2), (self.target,),
            self.root, self.root / "outputs",
            previous=(
                SavedSelection(
                    self.openutau.identity, "singer", "TIGER_DS_v102",
                    Provenance.PREVIOUS, Provenance.REFERENCE,
                ),
            ),
            presets=(self.preset,),
        )
        self.log = mock.patch.object(logging.getLogger("tools.orchestration.discovery"), "warning")
        self.log.start()
        self.addCleanup(self.log.stop)

    def file(self, name, contents):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        return path

    def save(self, config=None):
        save_configuration(self.path, config or self.config)
        return load_configuration(self.path)

    def raw(self):
        save_configuration(self.path, self.config)
        return json.loads(self.path.read_text())

    def reject_raw(self, document, error=ConfigurationError):
        self.path.write_text(json.dumps(document))
        with self.assertRaises(error):
            load_configuration(self.path)

    def cap(self, key, value, **kwargs):
        return Capability(
            self.openutau.identity, key, value,
            kwargs.pop("provenance", Provenance.FILESYSTEM),
            kwargs.pop("verification", Verification.METADATA),
            kwargs.pop("state", State.CANDIDATE), **kwargs,
        )

    def validate_with_reports(self, config, reports):
        with mock.patch("tools.orchestration.configuration.DiscoveryService.refresh", return_value=reports):
            return validate_configuration(config)

    def test_schema_round_trip_human_readable(self):
        loaded = self.save()
        self.assertEqual(loaded.configuration, self.config)
        document = json.loads(self.path.read_text())
        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["kind"], "melisma-configuration")
        self.assertIn('\n  "schema_version": 1,', self.path.read_text())
        self.assertNotIn("capabilities", self.path.read_text())
        self.assertNotIn("readiness", self.path.read_text())
        self.assertNotIn("verification", self.path.read_text())

    def test_restart_in_fresh_interpreter(self):
        save_configuration(self.path, self.config)
        code = (
            "from pathlib import Path; from tools.orchestration import load_configuration; "
            "import sys; c=load_configuration(Path(sys.argv[1])).configuration; "
            "assert c.yue2_targets[0].id == 'gpu-primary'; "
            "assert c.presets[0].target_id == 'gpu-primary'; "
            "assert c.previous[0].origin.value == 'reference'"
        )
        result = subprocess.run(
            (sys.executable, "-B", "-c", code, str(self.path)),
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_atomic_same_directory_closed_flushed_and_synced_before_replace(self):
        save_configuration(self.path, self.config)
        before = self.path.read_bytes()
        real_replace = os.replace
        real_fsync = os.fsync
        synced = []
        def sync(fd):
            synced.append(os.fstat(fd).st_ino)
            real_fsync(fd)
        def publish(source, destination):
            self.assertEqual(source.parent, self.path.parent)
            self.assertEqual(destination, self.path)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn(source.stat().st_ino, synced)
            self.assertIn('"schema_version": 1', source.read_text())
            # The NamedTemporaryFile context has closed before publishing.
            open_inodes = []
            for fd in Path("/proc/self/fd").iterdir():
                try:
                    open_inodes.append(fd.stat().st_ino)
                except FileNotFoundError:
                    pass
            self.assertNotIn(source.stat().st_ino, open_inodes)
            real_replace(source, destination)
        with mock.patch("tools.orchestration.configuration.os.replace", side_effect=publish) as replacement:
            with mock.patch("tools.orchestration.configuration.os.fsync", side_effect=sync):
                save_configuration(self.path, replace(self.config, output=self.root / "new"))
        replacement.assert_called_once()
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".configuration.json.*.tmp")), [])

    def test_write_failures_preserve_previous_and_cleanup(self):
        save_configuration(self.path, self.config)
        before = self.path.read_bytes()
        for stage in ("fsync", "replace"):
            with self.subTest(stage=stage):
                with mock.patch(f"tools.orchestration.configuration.os.{stage}", side_effect=OSError(stage)):
                    with self.assertRaises(OSError):
                        save_configuration(self.path, replace(self.config, output=self.root / "new"))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(load_configuration(self.path).configuration, self.config)
                self.assertEqual(list(self.root.glob(".configuration.json.*.tmp")), [])

    def test_partial_write_failure_preserves_previous(self):
        save_configuration(self.path, self.config)
        before = self.path.read_bytes()
        from tools.orchestration import configuration as module
        original = module.tempfile.NamedTemporaryFile
        def broken_file(**kwargs):
            stream = original(**kwargs)
            def write(contents):
                stream.file.write(contents[:25])
                raise OSError("simulated disk full")
            stream.write = write
            return stream
        with mock.patch.object(module.tempfile, "NamedTemporaryFile", side_effect=broken_file):
            with self.assertRaisesRegex(OSError, "disk full"):
                save_configuration(self.path, self.config)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".configuration.json.*.tmp")), [])

    def test_serialization_failure_does_not_touch_destination(self):
        save_configuration(self.path, self.config)
        before = self.path.read_bytes()
        with mock.patch("tools.orchestration.configuration.json.dumps", side_effect=TypeError("serialize")):
            with self.assertRaises(TypeError):
                save_configuration(self.path, self.config)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".configuration.json.*.tmp")), [])

    def test_versions_fail_explicitly(self):
        document = self.raw()
        for version in (0, -1, 2, 99, "1", True, None):
            with self.subTest(version=version):
                self.reject_raw({**document, "schema_version": version}, UnsupportedVersion)

    def test_strict_shapes_types_unknown_fields_and_duplicate_json_keys(self):
        document = self.raw()
        for value in (
            {**document, "unexpected": "value"},
            {**document, "kind": "wrong"},
            {**document, "value": {**document["value"], "capability_cache": {}}},
            {**document, "value": {**document["value"], "workspace": 123}},
            {**document, "value": {**document["value"], "yue2_targets": {}}},
        ):
            with self.subTest(value=value):
                self.reject_raw(value)
        self.path.write_text('{"schema_version":1,"schema_version":1,"kind":"melisma-configuration","value":{}}')
        with self.assertRaisesRegex(ConfigurationError, "duplicate"):
            load_configuration(self.path)
        self.path.write_text("{")
        with self.assertRaises(json.JSONDecodeError):
            load_configuration(self.path)

    def test_all_tool_source_paths_are_configurable(self):
        sources = (*self.config.sources, *(
            DiscoverySource(tool, self.root, self.root / f"custom/{tool}", contract=CONTRACTS[tool])
            for tool in ("ffmpeg", "ffprobe", "ffplay", "yue2-instrumental")
        ))
        config = replace(self.config, sources=sources)
        loaded = self.save(config)
        self.assertEqual(loaded.configuration.sources, sources)
        adapter = MelismaAdapter(self.melisma.executable, self.melisma.root)
        self.assertEqual(adapter.executable, loaded.configuration.sources[0].executable)
        self.assertTrue(any("missing, moved" in i.reason for i in loaded.issues))

    def test_empty_configuration_survives_without_inferred_defaults(self):
        loaded = self.save(Configuration())
        self.assertEqual(loaded.configuration, Configuration())
        self.assertEqual(loaded.reports, ())
        self.assertEqual(loaded.issues, ())

    def test_effective_source_contract_is_pinned_when_saved(self):
        source = replace(self.melisma, contract=None)
        save_configuration(self.path, Configuration(sources=(source,)))
        document = json.loads(self.path.read_text())
        self.assertEqual(document["value"]["sources"][0]["contract"], self.melisma.contract)
        with mock.patch.dict(CONTRACTS, {"melisma": "future-contract"}):
            loaded = load_configuration(self.path)
        self.assertEqual(loaded.configuration.sources[0].identity, self.melisma.identity)

    def test_remote_source_is_not_an_execution_target(self):
        source = replace(
            self.yue2, kind=SourceKind.REMOTE, target=self.target.id,
        )
        with self.assertRaises(ConfigurationError):
            save_configuration(self.path, replace(self.config, sources=(source,)))

    def test_paths_are_explicit_not_home_or_environment_expansions(self):
        for path in (Path("relative"), Path("~/data"), Path("$HOME/data"), self.root / ".." / "parent"):
            with self.subTest(path=path):
                with self.assertRaises(ConfigurationError):
                    save_configuration(self.path, replace(self.config, workspace=path))
        document = self.raw()
        document["value"]["workspace"] = "/invalid\0path"
        self.reject_raw(document)

    def test_path_permission_failures_are_not_missing_paths(self):
        from tools.orchestration.configuration import _path_issue
        with mock.patch.object(Path, "stat", side_effect=PermissionError("fixture denied")):
            issue = _path_issue(self.root / "denied", "fixture", directory=True)
        self.assertEqual(issue.state, State.UNAUTHORIZED)
        self.assertEqual(issue.readiness, Readiness.UNAUTHORIZED)

    def test_generation_token_counts_are_not_credentials(self):
        source = replace(
            self.yue2,
            configured=(("GenerationConfig.semantic.max_new_tokens", "9000"),),
        )
        defaults = GenerationDefaults(
            semantic_sampling=SamplingDefaults(min_new_tokens=200, max_new_tokens=9000),
        )
        target = replace(self.target, contracts=(replace(self.native, defaults=defaults),))
        loaded = self.save(replace(self.config, sources=(source,), yue2_targets=(target,)))
        self.assertEqual(loaded.configuration.sources[0], source)
        self.assertEqual(loaded.configuration.yue2_targets[0], target)

    def test_validation_report_and_explicit_environment_identity_persist(self):
        report = self.file("validation.json", '{"gpu_ready": false}')
        source = replace(
            self.yue2, validation_report=report, models_root=self.root / "models",
            environment=(("YUE2_KIT", str(self.root / "kit")),),
            configured=(("cot", "melody"),), references=(("seed", "831001"),),
        )
        loaded = self.save(replace(self.config, sources=(source,)))
        self.assertEqual(loaded.configuration.sources[0], source)
        evidence = loaded.reports[0].values("validation-evidence")
        self.assertEqual(evidence[0].verification, Verification.RECORDED)
        self.assertEqual(evidence[0].readiness, Readiness.UNKNOWN)

    def test_disabled_probes_mean_unknown_not_invalid_capabilities(self):
        executable = self.file("ffmpeg", "not executed")
        executable.chmod(0o755)
        source = DiscoverySource("ffmpeg", self.root, executable)
        loaded = self.save(replace(self.config, sources=(source,)))
        capabilities = loaded.reports[0].capabilities
        self.assertFalse(any(c.state == State.ERROR for c in capabilities))
        self.assertTrue(any(c.state == State.CANDIDATE and c.key == "executable" for c in capabilities))
        self.assertTrue(any(c.state == State.UNKNOWN and c.key == "filter.amix" for c in capabilities))
        executable.chmod(0o644)
        loaded = load_configuration(self.path)
        self.assertTrue(any(
            c.state == State.UNAVAILABLE and c.key == "executable"
            for c in loaded.reports[0].capabilities
        ))

    def test_authorized_roots_exact_paths_and_policy_survive(self):
        nested = self.root / "singers/nested"
        nested.mkdir()
        roots = (SingerRoot(self.root / "singers", False), SingerRoot(nested, True))
        source = replace(self.openutau, singer_roots=roots)
        loaded = self.save(replace(self.config, sources=(source,)))
        self.assertEqual(loaded.configuration.sources[0].singer_roots, roots)
        reports = loaded.reports[0].values("installed-singers")
        self.assertEqual({dict(item.evidence)["root"] for item in reports}, {str(r.path) for r in roots})
        self.assertNotIn(str(self.root), {dict(item.evidence)["root"] for item in reports})

    def test_no_inferred_singer_roots_or_preferences_or_home(self):
        source = replace(self.openutau, singer_roots=())
        self.file("openutau/prefs.json", '{"AdditionalSingerPath": "/must/not/read"}')
        from tools.orchestration import discovery
        real_text = discovery.text
        reads = []
        def read(path):
            reads.append(path)
            self.assertTrue(path.is_relative_to(self.openutau.root))
            self.assertNotEqual(path.name, "prefs.json")
            return real_text(path)
        save_configuration(self.path, replace(self.config, sources=(source,)))
        with mock.patch.object(discovery, "text", side_effect=read):
            loaded = load_configuration(self.path)
        self.assertEqual(loaded.configuration.sources[0].singer_roots, ())
        self.assertEqual(loaded.previous[0].state, State.UNAUTHORIZED)
        self.assertTrue(reads)

    def test_saved_nonrecursive_policy_does_not_expand(self):
        self.file("singers/deep/hidden/character.txt", "hidden")
        loaded = self.save()
        self.assertNotIn("deep/hidden", [c.value for c in loaded.reports[1].values("singer")])
        recursive = replace(self.openutau, singer_roots=(SingerRoot(self.root / "singers", True),))
        loaded = self.save(replace(self.config, sources=(recursive,)))
        self.assertIn("deep/hidden", [c.value for c in loaded.reports[0].values("singer")])

    def test_symlinks_do_not_expand_authorization(self):
        (self.root / "singers/link").symlink_to(self.root / "melisma", target_is_directory=True)
        loaded = self.save()
        self.assertEqual([c.value for c in loaded.reports[1].values("singer")], ["TIGER_DS_v102"])
        source = replace(self.openutau, singer_roots=(SingerRoot(self.root / "singers/link"),))
        loaded = self.save(replace(self.config, sources=(source,)))
        self.assertEqual(loaded.previous[0].state, State.ERROR)
        self.assertTrue(any("symlink" in i.reason for i in loaded.issues))

    def test_invalid_authorization_policy_rejected(self):
        document = self.raw()
        document["value"]["sources"][1]["singer_roots"][0]["recursive"] = "yes"
        self.reject_raw(document)

    def test_source_is_separate_from_targets_and_not_local_inference_readiness(self):
        loaded = self.save(replace(self.config, yue2_targets=(), presets=()))
        self.assertEqual(loaded.configuration.sources[2], self.yue2)
        self.assertEqual(loaded.configuration.yue2_targets, ())
        self.assertFalse(any(c.readiness == Readiness.READY for c in loaded.reports[2].capabilities))
        self.assertIsNone(loaded.configuration.sources[2].executable)
        self.assertTrue(loaded.reports[2].values("cot"))

    def test_remote_http_async_metadata_round_trip(self):
        loaded = self.save()
        target = loaded.configuration.yue2_targets[0]
        self.assertEqual(target, self.target)
        self.assertEqual(target.api.job_model, "async-jobs")
        self.assertIn("{job_id}", target.api.status_path)
        self.assertIn("{artifact_id}", target.api.artifacts_path)
        self.assertTrue(any(i.location == "target.gpu-primary" and i.readiness == Readiness.UNKNOWN for i in loaded.issues))

    def test_multiple_targets_stable_id_and_endpoint_changes(self):
        other = replace(self.target, id="gpu-secondary", endpoint="https://other.example.invalid/api")
        config = replace(self.config, yue2_targets=(self.target, other))
        self.assertEqual(self.save(config).configuration.yue2_targets, (self.target, other))
        changed = replace(self.target, endpoint="https://moved.example.invalid/api")
        loaded = self.save(replace(config, yue2_targets=(changed, other)))
        self.assertEqual(loaded.configuration.presets[0].target_id, self.target.id)
        self.assertNotIn("selected target is missing", " ".join(i.reason for i in loaded.issues))

    def test_duplicate_or_unsafe_target_ids_rejected(self):
        for targets in (
            (self.target, self.target), (replace(self.target, id="../gpu"),),
            (replace(self.target, id=""),),
        ):
            with self.subTest(targets=targets):
                with self.assertRaises(ConfigurationError):
                    save_configuration(self.path, replace(self.config, yue2_targets=targets))

    def test_native_and_pinned_helper_contracts_are_distinct(self):
        loaded = self.save()
        native, helper = loaded.configuration.yue2_targets[0].contracts
        self.assertEqual(native.version, "0.1.6")
        self.assertEqual(helper.version, "0.1.5")
        self.assertNotEqual(native.runtime, helper.runtime)
        self.assertNotEqual(native.model, helper.model)
        self.assertIsNone(native.pinned_models_root)
        self.assertEqual(helper.pinned_models_root, "/operator/remote/models")
        self.assertNotEqual(native.defaults.seed, helper.defaults.seed)

    def test_native_only_target_does_not_validate_instrumental_preset(self):
        target = replace(self.target, contracts=(self.native,))
        loaded = self.save(replace(self.config, yue2_targets=(target,)))
        self.assertTrue(any(i.state == State.UNAVAILABLE for i in loaded.presets[0].issues))
        with self.assertRaisesRegex(ConfigurationError, "selected operation"):
            loaded.require_preset(self.preset.id)

    def test_future_local_target_requires_independent_runtime_root(self):
        local = YuE2Target(
            "local-explicit", "local", (self.native,),
            local_root=self.root / "separate-runtime",
            local_executable=self.root / "separate-runtime/python",
        )
        loaded = self.save(replace(self.config, yue2_targets=(self.target, local)))
        self.assertEqual(loaded.configuration.yue2_targets[1], local)
        self.assertTrue(any(i.location == "target.local-explicit.root" and i.state == State.UNAVAILABLE for i in loaded.issues))
        with self.assertRaises(ConfigurationError):
            save_configuration(self.path, replace(self.config, yue2_targets=(replace(local, local_root=None),)))

    def test_endpoint_and_async_contract_schema_validation(self):
        for endpoint in (
            "http://offline.example.invalid", "https://", "ssh://host",
            "https://user:password@host", "https://host?token=secret",
            "https://host#credential", "https://host:invalid", "https://host:0",
            "https://host\\path", "https://host/a b",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ConfigurationError):
                    save_configuration(self.path, replace(self.config, yue2_targets=(replace(self.target, endpoint=endpoint),)))
        for api in (
            None, replace(self.target.api, job_model="single-request"),
            replace(self.target.api, status_path="https://other.invalid/status"),
            replace(self.target.api, submit_path="//other.invalid/jobs"),
            replace(self.target.api, submit_path="/jobs?credential=value"),
        ):
            with self.subTest(api=api):
                with self.assertRaises(ConfigurationError):
                    save_configuration(self.path, replace(self.config, yue2_targets=(replace(self.target, api=api),)))

    def test_auth_references_and_ssh_identity_path_not_secret_contents(self):
        key = self.file("keys/identity", "not read even if this were a private key")
        admin = SSHMetadata("admin.example.invalid", 22222, "operator", key)
        target = replace(self.target, auth=AuthReference("secret-store", "team/yue2/access"), administration=admin)
        loaded = self.save(replace(self.config, yue2_targets=(target,)))
        self.assertEqual(loaded.configuration.yue2_targets[0], target)
        self.assertNotIn(key.read_text(), self.path.read_text())

    def test_environment_secret_is_never_read(self):
        save_configuration(self.path, self.config)
        with mock.patch.dict(os.environ, {"YUE2_ACCESS_TOKEN": "credential-content-not-to-read"}):
            with mock.patch("os.getenv", side_effect=AssertionError("environment secret read")):
                loaded = load_configuration(self.path)
        self.assertEqual(loaded.configuration.yue2_targets[0].auth.name, "YUE2_ACCESS_TOKEN")
        self.assertNotIn("credential-content-not-to-read", self.path.read_text())

    def test_secret_bearing_fields_and_structures_rejected_on_load(self):
        for field in (
            "password", "bearer_token", "api_key", "runpod_api_token",
            "private_key", "credentials", "headers", "request_body",
        ):
            with self.subTest(field=field):
                document = self.raw()
                document["value"]["yue2_targets"][0][field] = {"nested": "credential-content"}
                self.reject_raw(document)
        document = self.raw()
        document["value"]["yue2_targets"][0]["auth"] = {"kind": "bearer", "value": "credential-content"}
        self.reject_raw(document)
        document = self.raw()
        document["value"]["sources"][0]["environment"] = [["API_KEY", "credential-content"]]
        self.reject_raw(document)

    def test_secret_contents_rejected_before_write_preserving_previous(self):
        save_configuration(self.path, self.config)
        before = self.path.read_bytes()
        for secret in (
            "Bearer abc.def.ghi", "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate",
            "password=credential-content", "runpod_api_token: credential-content",
        ):
            with self.subTest(secret=secret):
                target = replace(self.target, contracts=(replace(self.native, runtime=secret),))
                with self.assertRaises(ConfigurationError):
                    save_configuration(self.path, replace(self.config, yue2_targets=(target,)))
                self.assertEqual(self.path.read_bytes(), before)
        for source in (
            replace(self.melisma, configured=(("authorization", "credential-content"),)),
            replace(self.melisma, environment=(("SOME_ENV", "credential-content"),)),
        ):
            with self.assertRaises(ConfigurationError):
                save_configuration(self.path, replace(self.config, sources=(source,)))
        with self.assertRaises(ConfigurationError):
            save_preset(self.root / "preset.json", replace(self.preset, generation=GenerationDefaults(style="Bearer credential-content")))

    def test_previous_value_and_original_provenance_retained(self):
        loaded = self.save()
        selected = loaded.previous[0]
        self.assertEqual(selected.value, "TIGER_DS_v102")
        self.assertEqual(selected.provenance, Provenance.PREVIOUS)
        self.assertEqual(selected.origin, Provenance.REFERENCE)
        self.assertEqual(selected.verification, Verification.METADATA)
        self.assertEqual(selected.state, State.CANDIDATE)
        self.assertEqual(selected.readiness, Readiness.UNKNOWN)

    def test_source_configured_and_reference_selections_are_also_revalidated(self):
        source = replace(
            self.openutau, configured=(("renderer", "MISSING-RENDERER"),),
            references=(("singer", "TIGER_DS_v102"),),
        )
        loaded = self.save(replace(self.config, sources=(source,)))
        renderer, singer = loaded.configured
        self.assertEqual(renderer.value, "MISSING-RENDERER")
        self.assertEqual(renderer.state, State.UNAVAILABLE)
        self.assertEqual(renderer.provenance, Provenance.CONFIGURED)
        self.assertEqual(singer.state, State.CANDIDATE)
        self.assertEqual(singer.provenance, Provenance.REFERENCE)
        self.assertEqual(singer.readiness, Readiness.UNKNOWN)
        self.assertTrue(any(
            i.location == "configured.openutau.renderer" and i.state == State.UNAVAILABLE
            for i in loaded.issues
        ))

    def test_restart_revalidates_missing_and_reappearing_singer(self):
        self.save()
        (self.root / "singers/TIGER_DS_v102/character.txt").unlink()
        missing = load_configuration(self.path)
        self.assertEqual(missing.previous[0].state, State.UNAVAILABLE)
        self.assertEqual(missing.previous[0].value, "TIGER_DS_v102")
        self.assertEqual(missing.configuration.previous, self.config.previous)
        self.file("singers/TIGER_DS_v102/character.txt", "returned")
        current = load_configuration(self.path)
        self.assertEqual(current.previous[0].state, State.CANDIDATE)
        self.assertEqual(current.previous[0].origin, Provenance.REFERENCE)

    def test_moved_and_missing_sources_and_output_remain_explicit(self):
        moved = replace(self.openutau, root=self.root / "moved-openutau", singer_roots=())
        loaded = self.save(replace(self.config, sources=(moved,)))
        self.assertEqual(loaded.previous[0].value, "TIGER_DS_v102")
        self.assertEqual(loaded.previous[0].state, State.UNKNOWN)
        self.assertIn("moved", loaded.previous[0].reason)
        self.assertTrue(any(i.location == "openutau.root" and i.state == State.UNAVAILABLE for i in loaded.issues))
        self.assertTrue(any(i.location == "output" and i.state == State.UNAVAILABLE for i in loaded.issues))
        self.assertFalse((self.root / "outputs").exists())

    def test_invalid_unavailable_unauthorized_and_unknown_current_evidence(self):
        for state, readiness in (
            (State.ERROR, Readiness.UNKNOWN),
            (State.UNAVAILABLE, Readiness.UNAVAILABLE),
            (State.UNAUTHORIZED, Readiness.UNAUTHORIZED),
            (State.UNKNOWN, Readiness.UNKNOWN),
        ):
            with self.subTest(state=state):
                cap = self.cap(
                    "singer", "TIGER_DS_v102", state=state, readiness=readiness,
                    reason="fixture current validation state",
                )
                loaded = self.validate_with_reports(
                    self.config, (DiscoveryReport(self.openutau.identity, (cap,)),),
                )
                self.assertEqual(loaded.previous[0].state, state)
                self.assertEqual(loaded.previous[0].value, self.config.previous[0].value)
                self.assertNotEqual(loaded.previous[0].readiness, Readiness.READY)

    def test_configured_echo_cannot_validate_saved_value(self):
        for provenance in (
            Provenance.CONFIGURED, Provenance.REFERENCE, Provenance.PROJECT,
            Provenance.MANUAL, Provenance.PREVIOUS,
        ):
            cap = self.cap("singer", "TIGER_DS_v102", provenance=provenance)
            loaded = self.validate_with_reports(
                self.config, (DiscoveryReport(self.openutau.identity, (cap,)),),
            )
            self.assertEqual(loaded.previous[0].state, State.UNKNOWN)

    def test_partial_model_inventory_does_not_mean_unlisted_model_is_invalid(self):
        selection = SavedSelection(self.yue2.identity, "model", "operator/other-model")
        candidate = Capability(
            self.yue2.identity, "model", "some-local-candidate",
            Provenance.FILESYSTEM, Verification.METADATA, State.CANDIDATE,
        )
        loaded = self.validate_with_reports(
            replace(self.config, previous=(selection,)),
            (DiscoveryReport(self.yue2.identity, (candidate,)),),
        )
        self.assertEqual(loaded.previous[0].state, State.UNKNOWN)
        self.assertEqual(loaded.previous[0].value, selection.value)

    def test_different_default_does_not_replace_saved_selection(self):
        report = DiscoveryReport(self.openutau.identity, (
            self.cap("singer", "OTHER", provenance=Provenance.TOOL_DEFAULT, default=True),
            self.cap("singer", "TIGER_DS_v102"),
        ))
        loaded = self.validate_with_reports(self.config, (report,))
        self.assertEqual(loaded.previous[0].value, "TIGER_DS_v102")
        self.assertEqual(loaded.previous[0].provenance, Provenance.PREVIOUS)

    def test_no_stale_readiness_persisted_and_wrong_source_reports_rejected(self):
        ready = self.cap(
            "singer", "TIGER_DS_v102", state=State.READY,
            verification=Verification.OBSERVED, readiness=Readiness.READY,
        )
        loaded = self.validate_with_reports(
            self.config, (DiscoveryReport(self.openutau.identity, (ready,)),),
        )
        self.assertEqual(loaded.previous[0].readiness, Readiness.READY)
        self.assertEqual(self.save(loaded.configuration).previous[0].readiness, Readiness.UNKNOWN)
        unknown_source = replace(self.openutau.identity, location="/unconfigured/source")
        with self.assertRaises(ConfigurationError):
            self.validate_with_reports(self.config, (DiscoveryReport(unknown_source, ()),))

    def test_preset_save_load_and_gloria_restoration(self):
        path = self.root / "gloria.json"
        save_preset(path, self.preset)
        result = load_preset(path, self.config)
        self.assertEqual(result.preset, self.preset)
        self.assertEqual(result.preset.midi, Path("examples/five-notes.mid"))
        self.assertEqual(result.preset.lyrics, Path("examples/gloria.txt"))
        self.assertEqual(result.preset.generation.seed, 831001)
        self.assertEqual(result.preset.generation.composer, "agent")
        self.assertTrue(result.preset.generation.empty_lyrics)
        self.assertEqual(result.preset.mix, MixSettings())
        self.assertIsNone(result.preset.generation.cot)  # Score-dependent, not forced.
        self.assertEqual(result.preset.provenance, Provenance.REFERENCE)
        self.assertTrue(all(c.provenance == Provenance.REFERENCE for c in result.selections))
        self.assertEqual(result.selections[0].state, State.CANDIDATE)
        self.assertEqual(result.selections[1].state, State.UNKNOWN)  # Not loaded.
        self.assertEqual(result.selections[2].state, State.SUPPORTED)
        self.assertFalse(result.valid)

    def test_presets_require_validation_before_use(self):
        loaded = self.save()
        with self.assertRaisesRegex(ConfigurationError, "requires validation"):
            loaded.require_preset(self.preset.id)
        with self.assertRaisesRegex(ConfigurationError, "unknown preset"):
            loaded.require_preset("not-saved")
        config = replace(self.config, yue2_targets=())
        loaded = self.save(config)
        self.assertEqual(loaded.configuration.presets[0], self.preset)
        self.assertTrue(any(i.state == State.UNAVAILABLE for i in loaded.presets[0].issues))

    def test_preset_capability_changes_are_revalidated_not_bypassed(self):
        self.save()
        (self.root / "singers/TIGER_DS_v102/character.txt").unlink()
        loaded = load_configuration(self.path)
        self.assertEqual(loaded.presets[0].selections[0].state, State.UNAVAILABLE)
        self.assertEqual(loaded.presets[0].selections[0].provenance, Provenance.REFERENCE)
        self.assertEqual(loaded.configuration.presets[0], self.preset)
        with self.assertRaises(ConfigurationError):
            loaded.require_preset(self.preset.id)

    def test_preset_singer_renderer_compatibility_uses_current_evidence(self):
        self.file("singers/TIGER_DS_v102/character.yaml", "singer_type: classic\n")
        loaded = self.save()
        self.assertTrue(any(
            "does not support" in issue.reason and issue.state == State.ERROR
            for issue in loaded.presets[0].issues
        ))
        self.assertEqual(loaded.presets[0].selections[0].value, "TIGER_DS_v102")
        self.assertEqual(loaded.presets[0].selections[2].value, "DIFFSINGER")

    def test_gloria_has_no_endpoint_duplication_or_generated_blobs_or_golden_outputs(self):
        path = self.root / "preset.json"
        save_preset(path, self.preset)
        text = path.read_text()
        self.assertIn('"target_id": "gpu-primary"', text)
        for value in (
            self.target.endpoint, "endpoint", ".wav", ".ustx", ".flac",
            "base64", "golden", "reference/yue2-diffsinger-gloria",
        ):
            self.assertNotIn(value, text)
        document = json.loads(text)
        for field in ("audio", "ustx", "generated_outputs", "endpoint"):
            with self.subTest(field=field):
                document["value"][field] = "blob"
                path.write_text(json.dumps(document))
                with self.assertRaises(ConfigurationError):
                    load_preset(path, self.config)
                del document["value"][field]

    def test_invalid_generation_and_mix_values_rejected(self):
        for defaults in (
            GenerationDefaults(seed=True), GenerationDefaults(cot="invalid"),
            GenerationDefaults(budget=float("nan")),
            GenerationDefaults(cfg_scale=-1),
            GenerationDefaults(abc_sampling=SamplingDefaults(top_p=2)),
            GenerationDefaults(semantic_sampling=SamplingDefaults(min_new_tokens=20, max_new_tokens=10)),
        ):
            with self.subTest(defaults=defaults):
                with self.assertRaises(ConfigurationError):
                    save_preset(self.root / "preset.json", replace(self.preset, generation=defaults))
        for mix in (
            MixSettings(vocal_gain=0), MixSettings(normalize="false"),
            MixSettings(master_gain=float("inf")), MixSettings(duration="invalid"),
        ):
            with self.subTest(mix=mix):
                with self.assertRaises(ConfigurationError):
                    save_preset(self.root / "preset.json", replace(self.preset, mix=mix))

    def test_load_is_offline_nonexecuting_read_only_and_does_not_read_keys(self):
        identity = self.file("identity", "credential-content-must-not-be-read")
        target = replace(self.target, administration=SSHMetadata("offline.invalid", 2222, "operator", identity))
        config = replace(self.config, yue2_targets=(target,))
        save_configuration(self.path, config)
        save_preset(self.root / "preset.json", self.preset)
        before = {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*") if p.is_file()
        }
        real_open = Path.open
        def safe_open(path, *args, **kwargs):
            self.assertNotEqual(path, identity)
            self.assertTrue(path.is_relative_to(self.root), path)
            return real_open(path, *args, **kwargs)
        with mock.patch("socket.socket", side_effect=AssertionError("network")):
            with mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
                with mock.patch("subprocess.Popen", side_effect=AssertionError("process/SSH/SCP/provisioning/render")):
                    with mock.patch("tools.orchestration.process.ProcessRunner.run", side_effect=AssertionError("execute")):
                        with mock.patch.object(Path, "open", safe_open):
                            loaded = load_configuration(self.path)
                            preset = load_preset(self.root / "preset.json", config)
        self.assertEqual(loaded.configuration.yue2_targets[0], target)
        self.assertEqual(preset.preset, self.preset)
        after = {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.root.rglob("*") if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertFalse((self.root / "outputs").exists())
        self.assertNotIn("torch", sys.modules)


if __name__ == "__main__":
    unittest.main()
