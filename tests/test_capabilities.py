"""Issue #3: fixture-only discovery, no OpenUtau initialization or model runtime."""

import hashlib
import json
import logging
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
    Capability, DiscoveryReport, DiscoveryService, DiscoverySource, FFmpegAdapter,
    FFplayAdapter, InstrumentalAdapter, MelismaAdapter, NativeYuE2Adapter,
    OpenUtauAdapter, Provenance, Readiness, SingerRoot, SourceIdentity, SourceKind,
    State, Verification, resolve_default,
)
from tools.orchestration.discovery import HELPER, RUNTIME_METADATA, metadata_probe


NATIVE_CLI = '''
def parser():
    q.add_argument("--vae", default="standard")
    q.add_argument("--device", default="auto")
    q.add_argument("--budget", type=float, default=24)
    q.add_argument("--backend", choices=("torch", "torch-eager", "vllm"), default="torch")
    q.add_argument("--quantization", choices=("none", "fp8"), default="none")
    q.add_argument("--cot", choices=("full", "melody", "off"))
    q.add_argument("--offline", action="store_true")
    q.add_argument("--stage", choices=("plan", "audio"), default="audio")
    q.add_argument("--verify-hashes", action="store_true")
    q.add_argument("--concurrency", default=1)
def model_paths(args):
    return "m-a-p/YuE2-3B", "m-a-p/" + local_vae.name
raise RuntimeError("source must never be imported")
'''
PROTOCOL = '''
CONTEXT = 24576
PROTOCOL_VERSION = "yue2-native-v1"
class Sampling:
    temperature: float = 1.0
    top_p: float = .95
    top_k: int = 100
class SongRequest:
    cot: str = "full"
    seed: int = 831001
    id: str = "song"
    def guidance(self):
        return (1.01 if self.cot == "off" else 1.0) if self.cfg_scale is None else self.cfg_scale
class GenerationConfig:
    abc: Sampling = field(default_factory=lambda: Sampling(.7, .9, 30))
    semantic: Sampling = field(default_factory=Sampling)
    ode_steps: int = 32
    context: int = CONTEXT
    version: str = PROTOCOL_VERSION
'''
RENDERERS = '''
public const string CLASSIC = "CLASSIC";
public const string WORLDLINE_R = "WORLDLINE-R";
public const string WORLDLINE_R2 = "WORLDLINE-R2";
public const string ENUNU = "ENUNU";
public const string VOGEN = "VOGEN";
public const string DIFFSINGER = "DIFFSINGER";
public const string VOICEVOX = "VOICEVOX";
static readonly string[] classicRenderers = new[] { WORLDLINE_R, CLASSIC };
static readonly string[] enunuRenderers = new[] { ENUNU };
static readonly string[] vogenRenderers = new[] { VOGEN };
static readonly string[] diffSingerRenderers = new[] { DIFFSINGER };
static readonly string[] voicevoxRenderers = new[] { VOICEVOX };
// DocManager.SearchAllPlugins() and Initialize() must never be executed.
'''
USAGE = (
    "usage: melisma [--format ir|abc|ustx] MIDI_FILE LYRIC_FILE [OUTPUT_FILE]\n"
    "melisma render-inputs MIDI_FILE LYRIC_FILE OUTPUT_DIR\n"
)
PINS = {
    "YuE2-3B": {
        "repo": "m-a-p/YuE2-3B",
        "revision": "14fc6c6f146441b1dd6363fcb2e01e82a6914cb7",
        "weights_sha256": "a" * 64,
        "weights_bytes": 7261441640,
    },
    "YuE2-Vae": {
        "repo": "m-a-p/YuE2-Vae",
        "revision": "9a94e1d0ea9f8087e98f77fa88df4a4068104d2a",
        "weights_sha256": "b" * 64,
        "weights_bytes": 530512720,
    },
}


class FixtureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executable = self.root / "bin/tool"
        self.file("bin/tool", "not executed by fake probe")
        self.executable.chmod(0o755)
        for tool in ("ffmpeg", "ffplay", "ffprobe"):
            self.file(f"bin/{tool}", "not executed by fake probe").chmod(0o755)
        self.file("src/main.c", (
            'enum ml_output_format { ML_OUTPUT_IR, ML_OUTPUT_ABC, ML_OUTPUT_USTX };\n'
            'enum ml_output_format format = ML_OUTPUT_IR;\n'
            '"ir" "abc" "ustx" "render-inputs"\n'
        ))
        self.file("src/midi.c", "ml_read_midi_file")
        self.file("OpenUtau.Core/Render/Renderers.cs", RENDERERS)
        self.file("pyproject.toml", (
            '[project]\nname = "yue2-infer"\nversion = "0.1.6"\n'
            'dependencies = ["torch==2.10.0", "numpy==2.2.6"]\n'
        ))
        self.file("src/yue2/cli.py", NATIVE_CLI)
        self.file("src/yue2/protocol.py", PROTOCOL)
        self.file("src/yue2/pipeline.py", (
            'device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"'
        ))
        self.file(HELPER / "assets/release.json", json.dumps({
            "package_version": "0.1.5", "models": PINS,
        }))
        self.file(HELPER / "requirements-inference.txt", "numpy==2.2.6\n")
        self.file(HELPER / "scripts/setup_runtime.py", (
            'expected = {"yue2-infer": release["package_version"], "torch": "2.10.0", "numpy": "2.2.6"}'
        ))
        self.file(HELPER / "scripts/instrumental.py", (
            'p.add_argument("--device", default="cuda:0")\n'
            'p.add_argument("--composer", choices=("model", "agent"), default="model")\n'
            'p.add_argument("--seed", default=831001)\n'
            'raise RuntimeError("must never be imported")\n'
        ))
        self.probe = mock.Mock(side_effect=self.metadata)
        self.service = DiscoveryService(probe=self.probe)
        self.logging = mock.patch.object(logging.getLogger("tools.orchestration.discovery"), "warning")
        self.log = self.logging.start()
        self.addCleanup(self.logging.stop)

    def file(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def source(self, tool, **kwargs):
        executable = self.root / "bin" / tool if tool in ("ffmpeg", "ffplay", "ffprobe") else self.executable
        return DiscoverySource(tool, self.root, executable, **kwargs)

    def metadata(self, executable, args, cwd):
        self.assertEqual(cwd, self.root)
        if args == ():
            return 2, USAGE
        if args == ("-I", "-S", "-B", "-c", RUNTIME_METADATA):
            return 0, json.dumps({
                "python": "3.12.3",
                "packages": {"torch": "2.10.0", "numpy": "2.2.6", "yue2-infer": "0.1.5"},
            })
        if args == ("-version",):
            return 0, f"{executable.name} version fixture\n"
        if args == ("-hide_banner", "-h", "full"):
            return 0, "-nodisp disable display\n-autoexit exit on EOF\n-vn disable video\n-i input\n-hide_banner hide\n-loglevel level\n"
        if args[:2] == ("-hide_banner", "-h"):
            kind, name = args[2].split("=")
            if kind == "filter":
                return 0, (
                    f"Filter {name}\n"
                    '   volume <string> set volume (default "1.0")\n'
                    "   duration <int> mixing duration (default longest)\n"
                    "   normalize <boolean> normalize (default true)\n"
                )
            return 0, f"{kind.title()} {name} [fixture]\n"
        self.fail(f"forbidden metadata probe: {args}")

    def discover(self, tool, **kwargs):
        return self.service.discover(self.source(tool, **kwargs))

    def values(self, report, key):
        return [value.value for value in report.values(key)]

    def snapshot(self):
        return {
            str(path.relative_to(self.root)): (
                path.stat().st_mode, path.stat().st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
            )
            for path in self.root.rglob("*") if not path.is_symlink()
        }


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.identity = SourceIdentity("tool", "/explicit/path", "test-contract")

    def cap(self, value, provenance, **kwargs):
        return Capability(
            self.identity, "selection", value, provenance,
            kwargs.pop("verification", Verification.UNVERIFIED),
            kwargs.pop("state", State.CONFIGURED), **kwargs,
        )

    def report(self, *values):
        return DiscoveryReport(self.identity, values)

    def test_states_and_provenance_are_constrained_and_immutable(self):
        for state in (State.SUPPORTED, State.CONFIGURED, State.CANDIDATE):
            item = self.cap("bank", Provenance.SOURCE, state=state)
            self.assertEqual(item.readiness, Readiness.UNKNOWN)
            with self.assertRaises(AttributeError):
                item.state = State.READY
        self.assertEqual(len({item.value for item in Provenance}), len(Provenance))
        with self.assertRaises(ValueError):
            self.cap("bank", Provenance.SOURCE, state="made-up")

    def test_ready_requires_observed_evidence_and_both_axes(self):
        for kwargs in (
            {"state": State.READY},
            {"readiness": Readiness.READY},
            {"state": State.READY, "readiness": Readiness.READY},
        ):
            with self.assertRaises(ValueError):
                self.cap("bank", Provenance.FILESYSTEM, **kwargs)
        ready = self.cap(
            "bank", Provenance.FILESYSTEM, state=State.READY,
            readiness=Readiness.READY, verification=Verification.OBSERVED,
        )
        self.assertEqual(ready.state, State.READY)

    def test_failures_and_unknown_require_reasons(self):
        for state in (State.UNKNOWN, State.UNAUTHORIZED, State.UNAVAILABLE, State.ERROR, State.UNRESOLVED):
            with self.assertRaises(ValueError):
                self.cap(None, Provenance.SOURCE, state=state)

    def test_full_default_precedence(self):
        tool = self.cap("tool", Provenance.TOOL_DEFAULT, default=True)
        environment = self.cap("env", Provenance.ENVIRONMENT)
        filesystem = self.cap("file", Provenance.FILESYSTEM)
        previous = self.cap("previous", Provenance.PREVIOUS)
        valid = self.cap("previous", Provenance.SOURCE, verification=Verification.SOURCE)
        project = self.cap("preset", Provenance.PROJECT)
        manual = self.cap("typed", Provenance.MANUAL)
        for available, expected in (
            ((valid, filesystem, environment, tool), tool),
            ((valid, environment), environment),
            ((valid, filesystem), filesystem),
            ((valid,), replace(valid, provenance=Provenance.PREVIOUS, origin=Provenance.SOURCE, default=True)),
            ((), replace(project, default=True)),
        ):
            result = resolve_default(
                self.report(*available), "selection", previous=previous, project=project, manual=manual,
            )
            self.assertEqual(result, expected)
        self.assertEqual(
            resolve_default(self.report(), "selection", manual=manual),
            replace(manual, default=True),
        )

    def test_previous_revalidated_not_reused_verbatim(self):
        previous = self.cap(
            "bank", Provenance.PREVIOUS, verification=Verification.OBSERVED,
            state=State.READY, readiness=Readiness.READY,
        )
        current = self.cap("bank", Provenance.SOURCE, state=State.CANDIDATE, verification=Verification.METADATA)
        result = resolve_default(self.report(current), "selection", previous=previous)
        self.assertEqual(result.readiness, Readiness.UNKNOWN)
        self.assertEqual(result.verification, Verification.METADATA)
        self.assertEqual(result.origin, Provenance.SOURCE)

    def test_configured_or_reference_echo_cannot_revalidate_previous(self):
        previous = self.cap("missing", Provenance.PREVIOUS)
        for provenance in (Provenance.CONFIGURED, Provenance.REFERENCE):
            configured = self.cap("missing", provenance, verification=Verification.METADATA)
            result = resolve_default(self.report(configured), "selection", previous=previous)
            self.assertIsNone(result.value)

    def test_previous_missing_invalid_or_different_source_rejected(self):
        previous = self.cap("old", Provenance.PREVIOUS)
        unavailable = self.cap("old", Provenance.CONFIGURED, state=State.UNAVAILABLE, reason="gone")
        for current, prior in (
            ((), previous),
            ((unavailable,), previous),
            ((self.cap("old", Provenance.SOURCE),), replace(previous, source=replace(self.identity, contract="other"))),
        ):
            result = resolve_default(self.report(*current), "selection", previous=prior)
            self.assertIsNone(result.value)
            self.assertEqual(result.state, State.UNKNOWN)

    def test_fallback_not_relabeled_or_promoted(self):
        preset = self.cap(
            "reference", Provenance.PROJECT, state=State.READY,
            readiness=Readiness.READY, verification=Verification.OBSERVED,
        )
        result = resolve_default(self.report(), "selection", project=preset)
        self.assertEqual(result.provenance, Provenance.PROJECT)
        self.assertEqual(result.verification, Verification.UNVERIFIED)
        self.assertEqual(result.readiness, Readiness.UNKNOWN)
        with self.assertRaises(ValueError):
            resolve_default(self.report(), "selection", project=replace(preset, provenance=Provenance.FILESYSTEM))

    def test_unknown_readiness_is_not_false_availability(self):
        unknown = self.cap("remote/id", Provenance.TOOL_DEFAULT, default=True)
        result = resolve_default(self.report(unknown), "selection")
        self.assertEqual(result.readiness, Readiness.UNKNOWN)
        self.assertNotEqual(result.state, State.READY)


class DiscoveryTests(FixtureTest):
    def test_melisma_exact_operations_and_unresolved_midi(self):
        report = self.discover("melisma")
        self.assertEqual(
            set(self.values(report, "operation")),
            {"midi-import", "ir", "abc", "ustx", "render-inputs"},
        )
        self.assertEqual(report.values("midi-generation")[0].state, State.UNRESOLVED)
        self.assertEqual(resolve_default(report, "format").value, "ir")
        self.assertEqual(self.probe.call_args.args[1], ())

    def test_melisma_source_only_and_usage_only(self):
        source_only = self.service.discover(DiscoverySource("melisma", self.root))
        self.assertEqual(len(source_only.values("operation")), 5)
        self.probe.assert_not_called()
        self.file("src/main.c", "unrecognized source")
        self.file("src/midi.c", "")
        report = self.service.discover(self.source("melisma"), force=True)
        self.assertEqual(len(report.values("operation")), 5)
        self.assertTrue(all(item.verification == Verification.OBSERVED for item in report.values("operation")))

    def test_adapter_discovery_surfaces(self):
        for adapter_class, tool in (
            (MelismaAdapter, "melisma"), (OpenUtauAdapter, "openutau"),
            (NativeYuE2Adapter, "yue2-native"), (InstrumentalAdapter, "yue2-instrumental"),
            (FFmpegAdapter, "ffmpeg"), (FFplayAdapter, "ffplay"),
        ):
            with self.subTest(tool=tool):
                adapter = adapter_class(self.executable, self.root)
                self.assertEqual(adapter.discover(self.service).source.tool, tool)
                with self.assertRaises(ValueError):
                    adapter.discover(self.service, self.source("ffprobe"))

    def test_openutau_no_authorized_root_never_scans_data(self):
        with mock.patch("tools.orchestration.discovery.os.walk", side_effect=AssertionError("unauthorized scan")):
            report = self.discover("openutau")
        installed = report.values("installed-singers")[0]
        self.assertEqual(installed.state, State.UNAUTHORIZED)
        self.assertEqual(installed.readiness, Readiness.UNAUTHORIZED)
        self.assertFalse(report.values("singer"))
        self.probe.assert_not_called()

    def test_openutau_renderer_lists_not_constructible_unlisted_kind(self):
        report = self.discover("openutau")
        self.assertEqual(
            set(self.values(report, "renderer")),
            {"WORLDLINE-R", "CLASSIC", "ENUNU", "VOGEN", "DIFFSINGER", "VOICEVOX"},
        )
        self.assertNotIn("WORLDLINE-R2", self.values(report, "renderer"))
        self.assertTrue(all(item.state == State.SUPPORTED for item in report.values("renderer")))

    def test_openutau_explicit_readonly_candidate_metadata(self):
        root = self.root / "authorized/Singers"
        self.file("authorized/Singers/Tiger/character.txt", "name=fixture")
        self.file("authorized/Singers/Tiger/dsconfig.yaml", "acoustic: not-loaded.onnx")
        self.file("authorized/Singers/Tiger/character.yaml", "default_phonemizer: Example.Phonemizer\n")
        before = self.snapshot()
        report = self.discover(
            "openutau", singer_roots=(SingerRoot(root),),
            configured=(("singer", "SavedMissing"),),
            references=(("phonemizer", "Reference.Class"),),
        )
        self.assertEqual(self.snapshot(), before)
        singer = next(item for item in report.values("singer") if item.value == "Tiger")
        self.assertEqual(singer.state, State.CANDIDATE)
        self.assertEqual(dict(singer.evidence)["singer_type"], "diffsinger")
        self.assertEqual(singer.readiness, Readiness.UNKNOWN)
        self.assertEqual(report.values("singer")[0].state, State.CONFIGURED)
        self.assertEqual(self.values(report, "phonemizer"), ["Reference.Class", "Example.Phonemizer"])
        self.probe.assert_not_called()

    def test_openutau_depth_explicit_and_symlinks_not_followed(self):
        root = self.root / "authorized"
        self.file("authorized/outer/inner/character.txt", "")
        self.file("outside/secret/character.txt", "")
        (root / "link").symlink_to(self.root / "outside", target_is_directory=True)
        shallow = self.discover("openutau", singer_roots=(SingerRoot(root),))
        deep = self.discover("openutau", singer_roots=(SingerRoot(root, recursive=True),))
        self.assertEqual(self.values(shallow, "singer"), [])
        self.assertEqual(self.values(deep, "singer"), ["outer/inner"])

    def test_openutau_explicit_types_and_unparsed_yaml_unknown(self):
        for name, yaml in (
            ("Voice", "singer_type: voicevox\n"),
            ("Complex", "singer_type: !custom dynamic\n"),
        ):
            self.file(f"banks/{name}/character.txt", "")
            self.file(f"banks/{name}/character.yaml", yaml)
        report = self.discover("openutau", singer_roots=(SingerRoot(self.root / "banks"),))
        types = {item.value: dict(item.evidence)["singer_type"] for item in report.values("singer")}
        self.assertEqual(types, {"Complex": "unknown", "Voice": "voicevox"})

    def test_openutau_missing_and_permission_errors_not_empty_success(self):
        report = self.discover("openutau", singer_roots=(SingerRoot(self.root / "missing"),))
        self.assertEqual(report.values("installed-singers")[0].state, State.ERROR)
        root = self.root / "banks"
        root.mkdir()
        with mock.patch("tools.orchestration.discovery.os.walk", side_effect=PermissionError("denied")):
            report = self.discover("openutau", singer_roots=(SingerRoot(root),))
        self.assertIn("PermissionError", report.values("installed-singers")[0].reason)

    def test_openutau_bad_bank_metadata_does_not_hide_other_candidates(self):
        self.file("banks/Bad/character.txt", "")
        self.file("banks/Bad/character.yaml", "").write_bytes(b"\xff")
        self.file("banks/Good/character.txt", "")
        report = self.discover("openutau", singer_roots=(SingerRoot(self.root / "banks"),))
        self.assertEqual(self.values(report, "singer"), ["Bad", "Good"])
        self.assertEqual(report.values("singer-metadata")[0].state, State.ERROR)
        self.assertEqual(dict(report.values("singer")[0].evidence)["singer_type"], "unknown")

    def test_openutau_never_initializes_or_loads_plugins(self):
        # A configured executable is a trap: even --help must not be invoked.
        with mock.patch("subprocess.run", side_effect=AssertionError("OpenUtau execution")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("OpenUtau execution")):
            report = DiscoveryService().discover(self.source("openutau"))
        self.assertEqual(len(report.values("renderer")), 6)
        self.assertFalse((self.root / "Plugins").exists())
        self.assertFalse((self.root / "prefs.json").exists())

    def test_native_and_pinned_contracts_and_runtime_states_separate(self):
        native = self.discover("yue2-native")
        helper = self.discover("yue2-instrumental")
        self.assertEqual(self.values(native, "package-version"), ["0.1.6"])
        self.assertEqual(self.values(helper, "package-version"), ["0.1.5"])
        self.assertEqual(native.source.contract, "0.1.6")
        self.assertEqual(helper.source.contract, "0.1.5")
        self.assertFalse(native.values("dependency.yue2-infer"))
        self.assertEqual(helper.values("dependency.yue2-infer")[0].state, State.CANDIDATE)
        self.assertEqual(resolve_default(native, "device").value, "auto")
        self.assertEqual(resolve_default(helper, "device").value, "cuda:0")
        self.assertEqual(len(helper.values("model-identity")), 2)
        for item in helper.values("model-identity"):
            self.assertEqual(dict(item.evidence)["revision"], PINS[item.value.split("/")[-1]]["revision"])

    def test_native_parser_options_defaults_and_protocol(self):
        report = self.discover("yue2-native")
        self.assertEqual(set(self.values(report, "cot")), {"full", "melody", "off"})
        self.assertEqual(set(self.values(report, "backend")), {"torch", "torch-eager", "vllm"})
        self.assertEqual(set(self.values(report, "quantization")), {"none", "fp8"})
        self.assertEqual(resolve_default(report, "cot").value, "full")
        self.assertEqual(resolve_default(report, "budget").value, "24")
        self.assertEqual(resolve_default(report, "seed").value, "831001")
        self.assertEqual(resolve_default(report, "id").value, "song")
        self.assertEqual(set(self.values(report, "device")), {"auto", "cpu", "cuda", "mps"})
        self.assertEqual(self.values(report, "GenerationConfig.version"), ["yue2-native-v1"])
        self.assertEqual(self.values(report, "GenerationConfig.abc.temperature"), ["0.7"])
        self.assertEqual(self.values(report, "GenerationConfig.semantic.top_k"), ["100"])
        self.assertEqual(resolve_default(report, "cfg-scale.off").value, "1.01")
        self.assertEqual(resolve_default(report, "cfg-scale.melody").value, "1.0")
        self.assertNotIn("--verify-hashes", self.values(report, "option"))
        self.assertNotIn("--concurrency", self.values(report, "option"))

    def test_remote_model_id_local_candidate_and_hashes_not_ready(self):
        self.file("models/YuE2-3B/config.json", "{}")
        self.file("models/YuE2-3B/weights_manifest.json", '{"sha256": "declared-only"}')
        report = self.discover("yue2-native", configured=(("vae", "organization/custom-id"),))
        self.assertEqual(report.values("model.local")[0].state, State.CANDIDATE)
        self.assertEqual(report.values("model.local.weight-manifest")[0].state, State.CANDIDATE)
        self.assertEqual(report.values("weight-validation")[0].state, State.UNKNOWN)
        self.assertEqual(self.values(report, "vae.id"), ["organization/custom-id"])
        self.assertTrue(all(item.readiness != Readiness.READY for item in report.capabilities))
        self.assertEqual(resolve_default(report, "vae").value, "m-a-p/YuE2-Vae")

    def test_nonstandard_checkpoint_candidates_are_metadata_not_models(self):
        self.file("models/alternate/config.json", '{"model_type":"experimental"}')
        self.file("models/broken/config.json", "[]")
        self.file("outside/checkpoint/config.json", "{}")
        (self.root / "models/link").symlink_to(self.root / "outside/checkpoint", target_is_directory=True)
        report = self.discover("yue2-native")
        candidate = next(item for item in report.values("checkpoint-candidate") if item.state == State.CANDIDATE)
        self.assertEqual(candidate.value, str(self.root / "models/alternate"))
        self.assertEqual(candidate.readiness, Readiness.UNKNOWN)
        self.assertEqual(dict(candidate.evidence)["model_type"], "experimental")
        self.assertTrue(any(item.state == State.ERROR for item in report.values("checkpoint-candidate")))
        self.assertEqual(resolve_default(report, "model").value, "m-a-p/YuE2-3B")

    def test_explicit_model_root_and_configured_path_provenance(self):
        self.file("custom/YuE2-3B/config.json", "{}")
        self.file("other/model/config.json", "{}")
        report = self.discover(
            "yue2-native", models_root=self.root / "custom",
            configured=(("model", str(self.root / "other/model")),),
        )
        configured = report.values("model")[0]
        self.assertEqual(configured.provenance, Provenance.CONFIGURED)
        self.assertEqual(configured.verification, Verification.UNVERIFIED)
        self.assertEqual(len(report.values("model.local")), 2)
        self.assertEqual(configured.readiness, Readiness.UNKNOWN)
        self.assertEqual(resolve_default(report, "model").value, "m-a-p/YuE2-3B")
        self.assertEqual(resolve_default(report, "model").provenance, Provenance.TOOL_DEFAULT)

    def test_explicit_environment_root_without_ambient_search(self):
        kit = self.root / "kit"
        self.file("kit/models/YuE2-3B/config.json", "{}")
        with mock.patch.dict("os.environ", {"YUE2_KIT": "/must/not/access"}):
            report = self.discover("yue2-native", environment=(("YUE2_KIT", str(kit)),))
        default = resolve_default(report, "model")
        self.assertEqual(default.value, str(kit / "models/YuE2-3B"))
        self.assertEqual(default.provenance, Provenance.ENVIRONMENT)
        missing = self.discover("yue2-native", environment=(("YUE2_KIT", str(self.root / "absent")),))
        self.assertEqual(resolve_default(missing, "model").provenance, Provenance.TOOL_DEFAULT)

    def test_runtime_incomplete_gpu_unknown_and_model_independent(self):
        self.probe.side_effect = lambda *_: (0, '{"packages": {}, "python": "3.12"}')
        self.file("models/YuE2-3B/config.json", "{}")
        report = self.discover("yue2-native")
        self.assertEqual(report.values("dependencies")[0].state, State.UNAVAILABLE)
        self.assertEqual(report.values("dependency.torch")[0].readiness, Readiness.UNAVAILABLE)
        self.assertEqual(report.values("gpu")[0].state, State.UNKNOWN)
        self.assertEqual(report.values("model.local")[0].state, State.CANDIDATE)

    def test_installed_packages_do_not_imply_importability_gpu_or_weights(self):
        report = self.discover("yue2-native")
        self.assertEqual(report.values("dependencies")[0].state, State.CANDIDATE)
        self.assertEqual(report.values("dependencies")[0].readiness, Readiness.UNKNOWN)
        self.assertEqual(report.values("gpu")[0].readiness, Readiness.UNKNOWN)
        self.assertEqual(report.values("weight-validation")[0].readiness, Readiness.UNKNOWN)

    def test_helper_package_mismatch_is_runtime_failure_not_native_failure(self):
        original = self.metadata
        def probe(executable, args, cwd):
            if args[:2] == ("-I", "-S"):
                return 0, '{"packages": {"torch":"2.10.0","numpy":"2.2.6","yue2-infer":"0.1.6"}}'
            return original(executable, args, cwd)
        self.probe.side_effect = probe
        helper = self.discover("yue2-instrumental")
        native = self.discover("yue2-native")
        self.assertEqual(helper.values("dependencies")[0].state, State.UNAVAILABLE)
        self.assertEqual(native.values("dependencies")[0].state, State.CANDIDATE)

    def test_existing_hash_and_no_gpu_reports_retained_not_promoted(self):
        self.file("pinned/YuE2-3B/config.json", "{}")
        self.file("pinned/YuE2-Vae/config.json", "{}")
        self.file("pinned/models-verified.json", json.dumps({
            "passed": True, "gpu_ready": False, "models": PINS,
        }))
        report = self.discover("yue2-instrumental", models_root=self.root / "pinned")
        self.assertEqual(report.values("validation-evidence")[0].verification, Verification.RECORDED)
        self.assertEqual(report.values("weight-validation")[0].state, State.UNKNOWN)
        self.assertEqual(report.values("gpu-evidence")[0].state, State.UNAVAILABLE)
        self.assertEqual(report.values("gpu")[0].readiness, Readiness.UNKNOWN)
        self.assertTrue(all(item.readiness != Readiness.READY for item in report.capabilities))

    def test_source_version_mismatch_and_invalid_report_are_visible(self):
        self.file("pyproject.toml", '[project]\nversion="9.0"\ndependencies=[]')
        bad = self.file("bad.json", "[not valid]")
        report = self.discover("yue2-native", validation_report=bad)
        self.assertEqual(report.values("contract")[0].state, State.ERROR)
        self.assertEqual(report.values("validation-evidence")[0].state, State.ERROR)

    def test_unavailable_interpreter_does_not_hide_source_options(self):
        self.probe.side_effect = FileNotFoundError("missing Python")
        report = self.discover("yue2-instrumental")
        self.assertEqual(report.values("dependencies")[0].state, State.ERROR)
        self.assertEqual(resolve_default(report, "device").value, "cuda:0")
        self.assertEqual(len(report.values("model-identity")), 2)

    def test_ffmpeg_specific_filters_encoder_muxer_and_defaults(self):
        report = self.discover("ffmpeg")
        self.assertEqual(set(self.values(report, "filter")), {"volume", "amix", "volumedetect"})
        self.assertEqual(self.values(report, "encoder"), ["pcm_s16le"])
        self.assertEqual(self.values(report, "muxer"), ["wav"])
        self.assertEqual(resolve_default(report, "normalize").value, "true")
        self.assertEqual(resolve_default(report, "duration").value, "longest")
        self.assertEqual(resolve_default(report, "volume").provenance, Provenance.TOOL_DEFAULT)

    def test_ffmpeg_partial_failure_retains_other_checks(self):
        original = self.metadata
        def probe(executable, args, cwd):
            if args[-1:] == ("filter=amix",):
                return 0, "Unknown filter 'amix'."
            return original(executable, args, cwd)
        self.probe.side_effect = probe
        report = self.discover("ffmpeg")
        self.assertEqual(report.values("filter.amix")[0].state, State.ERROR)
        self.assertIn("pcm_s16le", self.values(report, "encoder"))

    def test_ffplay_options_and_no_audible_playback(self):
        report = self.discover("ffplay")
        self.assertEqual(set(self.values(report, "option")), {
            "-nodisp", "-autoexit", "-vn", "-i", "-hide_banner", "-loglevel",
        })
        self.assertEqual(report.values("audio-device")[0].readiness, Readiness.UNKNOWN)
        self.assertEqual(
            [call.args[1] for call in self.probe.call_args_list],
            [("-version",), ("-hide_banner", "-h", "full")],
        )

    def test_ffplay_missing_option_and_missing_binary(self):
        self.probe.side_effect = lambda *_: (0, "-nodisp no display\n")
        report = self.discover("ffplay")
        self.assertEqual(report.values("option.autoexit")[0].state, State.UNAVAILABLE)
        self.probe.side_effect = FileNotFoundError("ffplay missing")
        report = self.service.discover(self.source("ffplay"), force=True)
        self.assertEqual(report.values("executable")[0].state, State.ERROR)

    def test_independent_failures_simultaneously_logged(self):
        def fail(executable, args, cwd):
            raise FileNotFoundError("runtime unavailable")
        self.probe.side_effect = fail
        reports = self.service.refresh(tuple(self.source(tool) for tool in (
            "melisma", "openutau", "yue2-native", "yue2-instrumental", "ffplay",
        )))
        self.assertEqual(len(reports), 5)
        self.assertEqual(reports[1].values("installed-singers")[0].state, State.UNAUTHORIZED)
        self.assertEqual(reports[2].values("dependencies")[0].state, State.ERROR)
        self.assertEqual(reports[3].values("gpu")[0].state, State.UNKNOWN)
        self.assertEqual(reports[4].values("executable")[0].state, State.ERROR)
        self.assertEqual(len(reports[0].values("operation")), 5)
        self.assertTrue(self.log.called)

    def test_cache_manual_refresh_expiry_and_invalidation(self):
        now = [0]
        service = DiscoveryService(probe=self.probe, ttl=10, clock=lambda: now[0])
        source = self.source("melisma")
        first = service.discover(source)
        self.assertIs(service.discover(source), first)
        self.assertEqual(self.probe.call_count, 1)
        refreshed = service.refresh((source,))[0]
        self.assertIsNot(refreshed, first)
        self.assertEqual(self.probe.call_count, 2)
        now[0] = 11
        service.discover(source)
        self.assertEqual(self.probe.call_count, 3)
        service.invalidate()
        service.discover(source)
        self.assertEqual(self.probe.call_count, 4)

    def test_refresh_replaces_cached_failures_and_changed_singer_results(self):
        root = self.root / "banks"
        root.mkdir()
        source = self.source("openutau", singer_roots=(SingerRoot(root),))
        self.assertFalse(self.service.discover(source).values("singer"))
        self.file("banks/New/character.txt", "")
        self.assertFalse(self.service.discover(source).values("singer"))
        self.assertEqual(self.values(self.service.refresh((source,))[0], "singer"), ["New"])
        self.probe.side_effect = OSError("temporarily missing")
        source = self.source("melisma")
        self.assertEqual(self.service.discover(source).values("executable")[0].state, State.ERROR)
        self.probe.side_effect = self.metadata
        self.assertEqual(self.service.refresh((source,))[0].values("executable")[0].state, State.READY)

    def test_cache_key_includes_paths_contracts_auth_and_model_configuration(self):
        source = self.source("melisma")
        service = DiscoveryService(probe=self.probe, clock=lambda: 0)
        variants = (
            source, replace(source, executable=self.root / "different"),
            replace(source, contract="different"), replace(source, configured=(("singer", "A"),)),
            replace(source, models_root=self.root / "models"),
            replace(source, singer_roots=(SingerRoot(self.root / "banks", recursive=False),)),
            replace(source, singer_roots=(SingerRoot(self.root / "banks", recursive=True),)),
            replace(source, environment=(("DISCOVERY_TEST", "A"),)),
            replace(source, environment=(("DISCOVERY_TEST", "B"),)),
            replace(source, environment=(("YUE2_KIT", str(self.root / "kit-a")),)),
            replace(source, environment=(("YUE2_KIT", str(self.root / "kit-b")),)),
            replace(source, validation_report=self.root / "validation-a.json"),
            replace(source, validation_report=self.root / "validation-b.json"),
            replace(source, references=(("phonemizer", "Reference.A"),)),
            replace(source, references=(("phonemizer", "Reference.B"),)),
            replace(source, kind=SourceKind.REMOTE, target="runpod:target-a"),
            replace(source, kind=SourceKind.REMOTE, target="runpod:target-b"),
        )
        reports = []
        for variant in variants:
            with self.subTest(source=variant):
                calls_before = self.probe.call_count
                report = service.discover(variant)
                self.assertEqual(report.source, variant.identity)
                for previous in reports:
                    self.assertIsNot(report, previous)
                reports.append(report)
                self.assertIs(service.discover(variant), report)
                self.assertEqual(
                    self.probe.call_count,
                    calls_before + (variant.kind == SourceKind.LOCAL),
                )
        for variant, report in zip(variants, reports):
            with self.subTest(cached_source=variant):
                self.assertIs(service.discover(replace(variant)), report)
        self.assertEqual(self.probe.call_count, 15)

    def test_remote_identity_never_accesses_files_or_network(self):
        source = self.source("yue2-native", kind=SourceKind.REMOTE, target="runpod:future")
        with mock.patch("pathlib.Path.open", side_effect=AssertionError("remote filesystem")):
            report = self.service.discover(source)
        self.probe.assert_not_called()
        self.assertEqual(report.values("discovery")[0].state, State.UNKNOWN)

    def test_all_refresh_is_readonly_and_only_metadata_queries(self):
        self.file("banks/Example/character.txt", "")
        before = self.snapshot()
        sources = tuple(self.source(tool) for tool in (
            "melisma", "yue2-native", "yue2-instrumental", "ffmpeg", "ffplay", "ffprobe",
        )) + (self.source("openutau", singer_roots=(SingerRoot(self.root / "banks"),)),)
        with mock.patch("subprocess.run", side_effect=AssertionError("hidden subprocess")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("hidden subprocess")), \
             mock.patch("socket.socket", side_effect=AssertionError("network")), \
             mock.patch("pathlib.Path.mkdir", side_effect=AssertionError("mkdir")), \
             mock.patch("pathlib.Path.unlink", side_effect=AssertionError("delete")):
            self.service.refresh(sources)
        self.assertEqual(before, self.snapshot())
        for call in self.probe.call_args_list:
            args = call.args[1]
            self.assertTrue(
                args == () or args == ("-version",)
                or args == ("-I", "-S", "-B", "-c", RUNTIME_METADATA)
                or args[:2] == ("-hide_banner", "-h"),
                args,
            )

    def test_metadata_probe_timeout_and_nonzero_are_failures(self):
        self.probe.side_effect = subprocess.TimeoutExpired("metadata", 5)
        report = self.discover("ffplay")
        self.assertEqual(report.values("executable")[0].state, State.ERROR)
        self.probe.side_effect = lambda *_: (9, "diagnostic")
        report = self.service.discover(self.source("ffplay"), force=True)
        self.assertIn("diagnostic", report.values("executable")[0].reason)

    def test_invalid_source_metadata_is_isolated_from_other_tools(self):
        self.file("pyproject.toml", '[project]\nname="no-version-or-dependencies"')
        reports = self.service.refresh((self.source("yue2-native"), self.source("melisma")))
        self.assertEqual(reports[0].values("package")[0].state, State.ERROR)
        self.assertEqual(len(reports[1].values("operation")), 5)

    def test_probe_disables_ffreport_filesystem_side_effect(self):
        with mock.patch.dict("os.environ", {"FFREPORT": f"file={self.root}/must-not-write"}), \
             mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess((), 0, "ffmpeg version fake")
            metadata_probe(self.executable, ("-version",), self.root)
        self.assertNotIn("FFREPORT", run.call_args.kwargs["env"])
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_paths_must_be_explicit_and_absolute(self):
        with self.assertRaises(ValueError):
            DiscoverySource("melisma", Path("."))
        with self.assertRaises(ValueError):
            SingerRoot(Path("singers"))
        with self.assertRaises(ValueError):
            DiscoverySource("ffplay", self.root, kind=SourceKind.REMOTE)

    def test_actual_safe_probe_with_fake_executable(self):
        script = self.file(
            "bin/fake-ffplay",
            f"#!{sys.executable}\nimport sys\n"
            "print('ffplay version fake' if sys.argv[1:] == ['-version'] else "
            "'-nodisp disable display\\n-autoexit exit\\n-vn disable video\\n-i input\\n-hide_banner hide\\n-loglevel level')\n",
        )
        script.chmod(0o755)
        before = self.snapshot()
        report = DiscoveryService().discover(replace(self.source("ffplay"), executable=script))
        self.assertEqual(report.values("executable")[0].state, State.READY)
        self.assertEqual(len(report.values("option")), 6)
        self.assertEqual(before, self.snapshot())

    def test_actual_runtime_probe_does_not_import_tool_or_site_hooks(self):
        self.file("sitecustomize.py", "raise RuntimeError('site hook executed')")
        self.file("torch.py", "raise RuntimeError('torch imported')")
        self.file("yue2.py", "raise RuntimeError('tool imported')")
        before = self.snapshot()
        status, output = metadata_probe(
            Path(sys.executable), ("-I", "-S", "-B", "-c", RUNTIME_METADATA), self.root,
        )
        self.assertEqual(status, 0)
        self.assertIsInstance(json.loads(output)["packages"], dict)
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
