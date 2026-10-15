"""Read-only local metadata refresh. Never import tool code or initialize runtimes."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, SourceIdentity, SourceKind,
    State, Verification,
)


LOG = logging.getLogger(__name__)
HELPER = Path("skills/yue2-music/instrumental")
CONTRACTS = {
    "melisma": "melisma-phase1",
    "openutau": "openutau-headless-phase1",
    "yue2-native": "0.1.6",
    "yue2-instrumental": "0.1.5",
    "ffmpeg": "ffmpeg-phase1",
    "ffprobe": "ffprobe-phase1",
    "ffplay": "ffplay-phase1",
}


@dataclass(frozen=True)
class SingerRoot:
    """Constructing this object explicitly authorizes ONLY this singer tree."""

    path: Path
    recursive: bool = False

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("authorized singer root must be absolute")


@dataclass(frozen=True)
class DiscoverySource:
    tool: str
    root: Path
    executable: Path | None = None
    contract: str | None = None
    kind: SourceKind = SourceKind.LOCAL
    target: str | None = None
    singer_roots: tuple[SingerRoot, ...] = ()
    configured: tuple[tuple[str, str], ...] = ()
    references: tuple[tuple[str, str], ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    models_root: Path | None = None
    # Existing operator-selected reports, never generated during discovery.
    validation_report: Path | None = None

    def __post_init__(self) -> None:
        if self.tool not in CONTRACTS:
            raise ValueError(f"unsupported discovery tool: {self.tool}")
        for path in (self.root, self.executable, self.models_root, self.validation_report):
            if path is not None and not path.is_absolute():
                raise ValueError("discovery paths must be explicit absolute paths")
        if self.kind == SourceKind.REMOTE and not self.target:
            raise ValueError("remote identity requires an explicit target")

    @property
    def identity(self) -> SourceIdentity:
        location = self.target if self.kind == SourceKind.REMOTE else str(self.root)
        if self.executable is not None:
            location = f"{location}|{self.executable}"
        return SourceIdentity(
            self.tool, str(location), self.contract or CONTRACTS[self.tool], self.kind
        )


class Probe(Protocol):
    def __call__(
        self, executable: Path, arguments: tuple[str, ...], cwd: Path,
    ) -> tuple[int, str]: ...


class MetadataProbeDisabled(ValueError):
    pass


def metadata_probe(
    executable: Path, arguments: tuple[str, ...], cwd: Path,
) -> tuple[int, str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # FFREPORT makes even help/version queries write a report file.
    environment.pop("FFREPORT", None)
    result = subprocess.run(
        (str(executable), *arguments), cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        timeout=5, check=False, shell=False,
        env=environment,
    )
    return result.returncode, result.stdout


def text(path: Path) -> str:
    # Metadata only; accidental weights/logs are not read into memory.
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError(f"metadata exceeds 1 MiB: {path}")
    return data.decode("utf-8-sig")


def object_json(path: Path) -> dict[str, object]:
    value = json.loads(text(path))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def literal(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


class Collector:
    def __init__(self, source: DiscoverySource, probe: Probe | None) -> None:
        self.source = source
        self.probe = probe
        self.items: list[Capability] = []

    def add(
        self, key: str, value: object = None, *,
        provenance: Provenance = Provenance.SOURCE,
        verification: Verification = Verification.SOURCE,
        state: State = State.SUPPORTED,
        readiness: Readiness = Readiness.UNKNOWN,
        reason: str | None = None, default: bool = False,
        evidence: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.items.append(Capability(
            self.source.identity, key, None if value is None else str(value),
            provenance, verification, state, readiness, reason, default, evidence,
        ))

    def failure(
        self, key: str, reason: str, *, state: State = State.ERROR,
        readiness: Readiness = Readiness.UNKNOWN,
    ) -> None:
        self.add(
            key, verification=Verification.UNVERIFIED,
            state=state, readiness=readiness, reason=reason,
        )

    def attempt(self, key: str, action: Callable[[], None]) -> None:
        try:
            action()
        except MetadataProbeDisabled as error:
            self.failure(key, str(error), state=State.UNKNOWN)
        except (
            OSError, ValueError, UnicodeError, SyntaxError, KeyError, TypeError, ImportError,
            subprocess.SubprocessError,
        ) as error:
            self.failure(key, f"{type(error).__name__}: {error}")

    def query(self, arguments: tuple[str, ...], allowed: tuple[int, ...] = (0,)) -> str:
        executable = self.source.executable
        if self.probe is None:
            raise MetadataProbeDisabled("executable metadata probes disabled; runtime readiness unknown")
        if executable is None:
            raise ValueError("no executable/interpreter configured")
        code, output = self.probe(executable, arguments, self.source.root)
        if code not in allowed:
            raise ValueError(f"metadata query {arguments[:2]} exited {code}: {output[:500]}")
        return output

    def observed(self, key: str, value: str, output: str) -> None:
        self.add(
            key, value, provenance=Provenance.FILESYSTEM,
            verification=Verification.OBSERVED, state=State.READY,
            readiness=Readiness.READY, evidence=(("output", output[:500]),),
        )

    def executable_candidate(self) -> None:
        path = self.source.executable
        if path is None or not path.is_file() or not os.access(path, os.X_OK):
            self.failure(
                "executable", "executable not configured or not executable",
                state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE,
            )
        else:
            self.add(
                "executable", path, provenance=Provenance.FILESYSTEM,
                verification=Verification.METADATA, state=State.CANDIDATE,
                reason="file is executable; runtime not initialized",
            )

    def configured(self) -> None:
        for pairs, provenance in (
            (self.source.configured, Provenance.CONFIGURED),
            (self.source.references, Provenance.REFERENCE),
        ):
            for key, value in pairs:
                self.add(
                    key, value, provenance=provenance,
                    verification=Verification.UNVERIFIED, state=State.CONFIGURED,
                    reason="selection/reference only; not installed-resource evidence",
                )


def melisma(c: Collector) -> None:
    def source() -> None:
        main = text(c.source.root / "src/main.c")
        for mode in ("ir", "abc", "ustx"):
            if f'ML_OUTPUT_{mode.upper()}' in main and f'"{mode}"' in main:
                c.add("operation", mode, evidence=(("file", "src/main.c"),))
        if '"render-inputs"' in main:
            c.add("operation", "render-inputs", evidence=(("file", "src/main.c"),))
        importer = text(c.source.root / "src/midi.c")
        if "ml_read_midi" in importer:
            c.add("operation", "midi-import", evidence=(("file", "src/midi.c"),))
        match = re.search(r'enum ml_output_format format = ML_OUTPUT_(\w+);', main)
        if match:
            c.add("format", match[1].lower(), provenance=Provenance.TOOL_DEFAULT, default=True)
    c.attempt("source", source)
    c.failure("midi-generation", "no MIDI producer in the Phase 1 interface", state=State.UNRESOLVED)
    if c.source.executable is not None:
        def usage() -> None:
            output = c.query((), (2,))
            if not all(token in output for token in ("usage:", "MIDI_FILE", "ir|abc|ustx", "render-inputs")):
                raise ValueError("unrecognized Melisma usage contract")
            c.observed("executable", str(c.source.executable), output)
            for operation in ("midi-import", "ir", "abc", "ustx", "render-inputs"):
                if not any(item.key == "operation" and item.value == operation for item in c.items):
                    c.add(
                        "operation", operation, verification=Verification.OBSERVED,
                        evidence=(("usage", output[:500]),),
                    )
        c.attempt("executable", usage)


def openutau(c: Collector) -> None:
    # Never execute OpenUtau, even help: source/binary correspondence is unknown.
    def renderers() -> None:
        path = c.source.root / "OpenUtau.Core/Render/Renderers.cs"
        source = text(path)
        constants = dict(re.findall(r'const string (\w+)\s*=\s*"([^"]+)"', source))
        arrays = re.findall(r'string\[\]\s+(\w+)Renderers\s*=\s*new\[\]\s*\{([^}]+)\}', source)
        if not arrays:
            raise ValueError("unsupported renderer source layout")
        for singer_type, names in arrays:
            for name in names.split(","):
                value = constants.get(name.strip())
                if value is None:
                    raise ValueError(f"unresolved renderer constant: {name}")
                c.add("renderer", value, evidence=(("singer_type", singer_type), ("file", str(path))))
    c.attempt("renderer", renderers)
    c.attempt("executable", c.executable_candidate)
    if not c.source.singer_roots:
        c.failure(
            "installed-singers", "no explicitly authorized singer/data root supplied",
            state=State.UNAUTHORIZED, readiness=Readiness.UNAUTHORIZED,
        )
    for authorized in c.source.singer_roots:
        c.attempt("installed-singers", lambda root=authorized: scan_singers(c, root))


def static_yaml_scalar(source: str, key: str) -> str | None:
    # Deliberately limited YAML subset; never deserialize tags or resolve includes.
    match = re.search(
        rf"(?m)^{re.escape(key)}:\s*([A-Za-z0-9_.+ -]+)\s*(?:#.*)?$", source
    )
    return match[1].strip() if match else None


def scan_singers(c: Collector, authorized: SingerRoot) -> None:
    root = authorized.path
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"authorized singer root missing or symlink: {root}")
    found = 0
    def walk_error(error: OSError) -> None:
        raise error
    for directory, subdirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        path = Path(directory)
        subdirs[:] = sorted(
            name for name in subdirs
            if not (path / name).is_symlink()
            and (authorized.recursive or path == root)
        )
        if "character.txt" not in files or (path / "character.txt").is_symlink():
            continue
        if path == root:
            continue
        info = ""
        yaml = path / "character.yaml"
        metadata_unknown = yaml.is_symlink()
        if yaml.is_symlink():
            c.failure("singer-metadata", f"refusing symlink metadata: {yaml}")
        elif yaml.is_file():
            metadata_unknown = True
            def read_metadata() -> None:
                nonlocal info, metadata_unknown
                info = text(yaml)
                metadata_unknown = False
            c.attempt("singer-metadata", read_metadata)
        singer_type = static_yaml_scalar(info, "singer_type")
        if singer_type not in ("classic", "enunu", "diffsinger", "vogen", "voicevox"):
            singer_type = None
        if singer_type is None:
            if (path / "enuconfig.yaml").is_file() and not (path / "enuconfig.yaml").is_symlink():
                singer_type = "enunu"
            elif (path / "dsconfig.yaml").is_file() and not (path / "dsconfig.yaml").is_symlink():
                singer_type = "diffsinger"
            elif not info and not metadata_unknown:
                singer_type = "classic"
        found += 1
        singer_id = path.relative_to(root).as_posix()
        c.add(
            "singer", singer_id, provenance=Provenance.FILESYSTEM,
            verification=Verification.METADATA, state=State.CANDIDATE,
            reason="static voicebank candidate; assets/phonemizer/rendering unvalidated",
            evidence=(("path", str(path)), ("singer_type", singer_type or "unknown")),
        )
        phonemizer = static_yaml_scalar(info, "default_phonemizer")
        if phonemizer:
            c.add(
                "phonemizer", phonemizer, provenance=Provenance.CONFIGURED,
                verification=Verification.METADATA, state=State.CONFIGURED,
                evidence=(("singer", singer_id), ("file", str(yaml))),
                reason="voicebank reference only; phonemizer not loaded or validated",
            )
    c.add(
        "installed-singers", found, provenance=Provenance.FILESYSTEM,
        verification=Verification.METADATA, state=State.CANDIDATE,
        reason="enumeration complete for this authorized root only",
        evidence=(("root", str(root)), ("recursive", str(authorized.recursive))),
    )


def parser_options(c: Collector, path: Path) -> None:
    tree = ast.parse(text(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "add_argument" or not node.args:
            continue
        flag = literal(node.args[0])
        if not isinstance(flag, str) or not flag.startswith("--"):
            continue
        key = flag[2:]
        if c.source.tool == "yue2-native" and key in ("verify-hashes", "input", "concurrency"):
            continue
        kwargs = {item.arg: literal(item.value) for item in node.keywords}
        evidence = (("file", str(path)), ("line", str(node.lineno)))
        for value in kwargs.get("choices") or ():
            c.add(key, value, evidence=evidence)
        if "default" in kwargs and kwargs["default"] is not None and key not in ("model", "vae"):
            c.add(
                key, kwargs["default"], provenance=Provenance.TOOL_DEFAULT,
                default=True, evidence=evidence,
            )
        if kwargs.get("action") in ("store_true", "store_false"):
            c.add(
                key, kwargs.get("action") == "store_false",
                provenance=Provenance.TOOL_DEFAULT, default=True, evidence=evidence,
            )
        c.add("option", flag, evidence=evidence)


def protocol_defaults(c: Collector, path: Path) -> None:
    tree = ast.parse(text(path))
    constants: dict[str, object] = {}
    sampling: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Sampling":
            sampling = {
                field.target.id: literal(field.value)
                for field in node.body
                if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
                and field.value is not None
            }
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            constants[node.targets[0].id] = literal(node.value)
        if not isinstance(node, ast.ClassDef) or node.name not in ("SongRequest", "GenerationConfig", "Sampling"):
            continue
        for field in node.body:
            if not isinstance(field, ast.AnnAssign) or field.value is None or not isinstance(field.target, ast.Name):
                continue
            value = constants.get(field.value.id) if isinstance(field.value, ast.Name) else literal(field.value)
            if value is not None:
                c.add(
                    f"{node.name}.{field.target.id}", value,
                    provenance=Provenance.TOOL_DEFAULT, default=True,
                    evidence=(("file", str(path)), ("line", str(field.lineno))),
                )
            elif node.name == "GenerationConfig" and isinstance(field.value, ast.Call):
                factory = next((
                    keyword.value for keyword in field.value.keywords
                    if keyword.arg == "default_factory"
                ), None)
                call = factory.body if isinstance(factory, ast.Lambda) else factory
                if isinstance(call, ast.Name) and call.id == "Sampling":
                    values = sampling
                elif (
                    isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "Sampling"
                ):
                    values = {**sampling, **dict(zip(sampling, (literal(arg) for arg in call.args)))}
                else:
                    continue
                for key, value in values.items():
                    c.add(
                        f"GenerationConfig.{field.target.id}.{key}", value,
                        provenance=Provenance.TOOL_DEFAULT, default=True,
                        evidence=(("file", str(path)), ("line", str(field.lineno))),
                    )
    for key in ("cot", "seed", "id"):
        default = next((item for item in c.items if item.key == f"SongRequest.{key}"), None)
        if default is not None:
            c.items.append(replace(default, key=key))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "guidance":
            continue
        for branch in ast.walk(node):
            if not isinstance(branch, ast.IfExp) or not isinstance(branch.test, ast.Compare):
                continue
            test = branch.test
            if (
                not isinstance(test.left, ast.Attribute) or test.left.attr != "cot"
                or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq)
                or len(test.comparators) != 1
            ):
                continue
            mode, yes, no = literal(test.comparators[0]), literal(branch.body), literal(branch.orelse)
            if not isinstance(mode, str) or not isinstance(yes, (int, float)) or not isinstance(no, (int, float)):
                continue
            for value in sorted({item.value for item in c.items if item.key == "cot" and item.value is not None}):
                c.add(
                    f"cfg-scale.{value}", yes if value == mode else no,
                    provenance=Provenance.TOOL_DEFAULT, default=True,
                    evidence=(("file", str(path)), ("cot", value)),
                )


# -I -S avoids user/site startup hooks and tool imports. Paths come from the
# selected interpreter/venv, not the harness interpreter or ambient PYTHONPATH.
RUNTIME_METADATA = r"""
import importlib.metadata as m, json, pathlib, sys, sysconfig
exe = pathlib.Path(sys.executable).absolute()
venv = exe.parent.parent
paths = set(sysconfig.get_paths()[k] for k in ('purelib', 'platlib'))
if (venv / 'pyvenv.cfg').is_file():
    cfg = (venv / 'pyvenv.cfg').read_text()
    if 'include-system-site-packages = true' not in cfg.lower():
        paths.clear()
    paths.update(str(p) for p in (venv / 'lib').glob('python*/site-packages'))
    paths.add(str(venv / 'Lib/site-packages'))
packages = {}
for d in m.distributions(path=sorted(paths)):
    name = d.metadata.get('Name', '').lower().replace('_', '-')
    packages[name] = d.version
print(json.dumps({'packages': packages, 'python': sys.version.split()[0]}))
"""


def runtime(c: Collector, expected: dict[str, str]) -> None:
    def dependencies() -> None:
        report = json.loads(c.query(("-I", "-S", "-B", "-c", RUNTIME_METADATA)))
        if not isinstance(report, dict) or not isinstance(report.get("packages"), dict):
            raise ValueError("invalid runtime metadata response")
        packages = report["packages"]
        failures = []
        for name, version in expected.items():
            installed = packages.get(name)
            matches = isinstance(installed, str) and installed.split("+")[0] == version
            if not matches:
                failures.append(name)
            c.add(
                f"dependency.{name}", installed,
                provenance=Provenance.FILESYSTEM, verification=Verification.METADATA,
                state=State.CANDIDATE if matches else State.UNAVAILABLE,
                readiness=Readiness.UNKNOWN if matches else Readiness.UNAVAILABLE,
                reason="installed metadata only; imports not tested" if matches else "missing or incompatible distribution",
                evidence=(("required", version), ("interpreter", str(c.source.executable))),
            )
        c.add(
            "dependencies", "complete-metadata" if not failures else "incomplete",
            provenance=Provenance.FILESYSTEM, verification=Verification.METADATA,
            state=State.CANDIDATE if not failures else State.UNAVAILABLE,
            readiness=Readiness.UNKNOWN if not failures else Readiness.UNAVAILABLE,
            reason="package imports/ABI readiness not tested" if not failures else f"missing/incompatible: {', '.join(failures)}",
            evidence=(("python", str(report.get("python", "unknown"))),),
        )
    c.attempt("dependencies", dependencies)
    c.failure(
        "gpu", "GPU/driver/BF16/free-memory readiness not probed; no Torch import or inference",
        state=State.UNKNOWN,
    )


def model_candidate(c: Collector, key: str, path: Path) -> None:
    if path.is_dir():
        c.add(
            key, path, provenance=Provenance.FILESYSTEM,
            verification=Verification.METADATA, state=State.CANDIDATE,
            reason="directory exists; weights not loaded or validated",
        )
        if key.endswith(".local"):
            c.items.append(replace(c.items[-1], key=key[:-6]))
        manifest = path / "weights_manifest.json"
        if manifest.is_file():
            c.add(
                f"{key}.weight-manifest", text(manifest),
                provenance=Provenance.FILESYSTEM, verification=Verification.METADATA,
                state=State.CANDIDATE,
                reason="declared weight metadata is not hash-validation evidence",
                evidence=(("file", str(manifest)),),
            )
    else:
        c.add(
            key, path, provenance=Provenance.CONFIGURED,
            verification=Verification.METADATA, state=State.UNAVAILABLE,
            readiness=Readiness.UNAVAILABLE, reason="local model directory missing",
        )


def validation_evidence(c: Collector) -> None:
    c.failure("weight-validation", "weight hashes not computed during ordinary refresh", state=State.UNKNOWN)
    report = c.source.validation_report
    if report is None and c.source.tool == "yue2-instrumental" and c.source.models_root is not None:
        candidate = c.source.models_root / "models-verified.json"
        if candidate.is_file():
            report = candidate
    if report is not None:
        def recorded() -> None:
            value = object_json(report)
            c.add(
                "validation-evidence", json.dumps(value, sort_keys=True),
                provenance=Provenance.FILESYSTEM, verification=Verification.RECORDED,
                state=State.CANDIDATE,
                reason="historical report only; binding/freshness/hashes not revalidated",
                evidence=(("file", str(report)), ("contract", c.source.identity.contract)),
            )
            if value.get("gpu_ready") is False or value.get("cuda") == []:
                c.add(
                    "gpu-evidence", "unavailable",
                    provenance=Provenance.FILESYSTEM, verification=Verification.RECORDED,
                    state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE,
                    reason="prior report has no ready GPU; current hardware state remains unknown",
                    evidence=(("file", str(report)),),
                )
        c.attempt("validation-evidence", recorded)


def native_yue2(c: Collector) -> None:
    expected: dict[str, str] = {}
    def package() -> None:
        import tomllib

        project = tomllib.loads(text(c.source.root / "pyproject.toml"))["project"]
        version = project["version"]
        c.add("package-version", version, evidence=(("file", "pyproject.toml"),))
        if version != c.source.identity.contract:
            c.failure("contract", f"source version {version} differs from adapter contract {c.source.identity.contract}")
        for spec in project["dependencies"]:
            match = re.fullmatch(r"([A-Za-z0-9_-]+)==([\w.+-]+)", spec)
            if not match:
                raise ValueError(f"unsupported dependency constraint: {spec}")
            expected[match[1].lower().replace("_", "-")] = match[2]
    c.attempt("package", package)
    c.attempt("options", lambda: parser_options(c, c.source.root / "src/yue2/cli.py"))
    c.attempt("defaults", lambda: protocol_defaults(c, c.source.root / "src/yue2/protocol.py"))
    def devices() -> None:
        path = c.source.root / "src/yue2/pipeline.py"
        tree = ast.parse(text(path))
        values: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign) and isinstance(node.value, ast.IfExp)
                and any(isinstance(target, ast.Name) and target.id == "device" for target in node.targets)
            ):
                values.update(
                    child.value for child in ast.walk(node.value)
                    if isinstance(child, ast.Constant) and isinstance(child.value, str)
                )
        for value in sorted(values & {"cuda", "mps", "cpu"}):
            c.add("device", value, evidence=(("file", str(path)),))
    c.attempt("device-options", devices)
    def models() -> None:
        cli = text(c.source.root / "src/yue2/cli.py")
        environment = dict(c.source.environment)
        kit = Path(environment["YUE2_KIT"]) if "YUE2_KIT" in environment else c.source.root
        if not kit.is_absolute():
            raise ValueError("YUE2_KIT must be absolute")
        default_root = kit / "models"
        root = c.source.models_root or default_root
        for key, name, remote in (
            ("model", "YuE2-3B", "m-a-p/YuE2-3B"),
            ("vae", "YuE2-Vae", "m-a-p/YuE2-Vae"),
        ):
            # Recognize the inspected contract without executing model_paths().
            if remote not in cli and not (key == "vae" and '"m-a-p/"' in cli):
                c.failure(key, "unrecognized model default source", state=State.UNKNOWN)
                continue
            selected = dict(c.source.configured).get(key)
            local = root / name
            c.attempt(f"{key}.local", lambda key=key, local=local: model_candidate(c, f"{key}.local", local))
            default_local = default_root / name
            value = str(default_local) if default_local.is_dir() else remote
            c.add(
                key, value,
                provenance=Provenance.ENVIRONMENT if "YUE2_KIT" in environment and default_local.is_dir() else Provenance.TOOL_DEFAULT,
                state=State.CONFIGURED, default=True,
                reason="source default identity only; not installed/validated readiness",
                evidence=(("file", "src/yue2/cli.py"),),
            )
            if selected and Path(selected).is_absolute() and Path(selected) != local:
                c.attempt(
                    f"{key}.configured-local",
                    lambda key=key, selected=selected: model_candidate(c, f"{key}.local", Path(selected)),
                )
            elif selected and not Path(selected).is_absolute():
                c.add(
                    f"{key}.id", selected, provenance=Provenance.CONFIGURED,
                    verification=Verification.UNVERIFIED, state=State.CONFIGURED,
                )
        legacy = root / "YuE2-Vae-legacy"
        if legacy.is_dir():
            model_candidate(c, "vae.local", legacy)
        if root.is_dir():
            for path in sorted(root.iterdir()):
                if (
                    path.is_symlink() or not path.is_dir()
                    or path.name in ("YuE2-3B", "YuE2-Vae", "YuE2-Vae-legacy")
                ):
                    continue
                config = path / "config.json"
                if config.is_file() and not config.is_symlink():
                    def checkpoint(path: Path = path, config: Path = config) -> None:
                        metadata = object_json(config)
                        c.add(
                            "checkpoint-candidate", path,
                            provenance=Provenance.FILESYSTEM,
                            verification=Verification.METADATA, state=State.CANDIDATE,
                            reason="local checkpoint metadata; model/VAE role and weights unvalidated",
                            evidence=(("model_type", str(metadata.get("model_type", "unknown"))),),
                        )
                    c.attempt("checkpoint-candidate", checkpoint)
    c.attempt("models", models)
    if expected:
        runtime(c, expected)
    else:
        c.failure("dependencies", "dependency requirements could not be established", state=State.UNKNOWN)
        c.failure("gpu", "GPU not probed", state=State.UNKNOWN)
    validation_evidence(c)


def instrumental(c: Collector) -> None:
    expected: dict[str, str] = {}
    root = c.source.root / HELPER
    def release() -> None:
        release = object_json(root / "assets/release.json")
        version = release.get("package_version")
        c.add("package-version", version)
        if version != c.source.identity.contract or version != "0.1.5":
            raise ValueError("helper release must pin adapter contract yue2-infer==0.1.5")
        requirements = text(root / "requirements-inference.txt")
        for line in requirements.splitlines():
            match = re.fullmatch(r"([A-Za-z0-9_-]+)==([\w.+-]+)", line.strip())
            if match:
                expected[match[1].lower().replace("_", "-")] = match[2]
        # The current requirements file pins a wheel URL; the source's doctor
        # dict is the authoritative dependency-version contract, not that URL.
        setup = ast.parse(text(root / "scripts/setup_runtime.py"))
        for node in ast.walk(setup):
            if (
                isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(target, ast.Name) and target.id == "expected" for target in node.targets)
            ):
                for key, value in zip(node.value.keys, node.value.values):
                    name, version = literal(key), literal(value)
                    if isinstance(name, str) and isinstance(version, str):
                        expected[name] = version
        if "torch" not in expected:
            raise ValueError("pinned runtime dependency contract unavailable")
        expected["yue2-infer"] = "0.1.5"
        models = release.get("models")
        if not isinstance(models, dict) or set(models) != {"YuE2-3B", "YuE2-Vae"}:
            raise ValueError("invalid pinned model structure")
        for name, model in models.items():
            if not isinstance(model, dict):
                raise ValueError("invalid pinned model identity")
            c.add(
                "model-identity", model.get("repo"),
                evidence=tuple((key, str(model.get(key))) for key in ("revision", "weights_sha256", "weights_bytes")),
            )
            c.add("required-model-directory", name)
            if c.source.models_root is not None:
                c.attempt(f"model.{name}", lambda name=name: model_candidate(c, f"model.{name}", c.source.models_root / name))
    c.attempt("release", release)
    c.attempt("options", lambda: parser_options(c, root / "scripts/instrumental.py"))
    c.add("gpu-requirement", "CUDA, BF16 compute capability >=8, 24 GiB-class allocation")
    if expected:
        runtime(c, expected)
    else:
        c.failure("dependencies", "pinned runtime requirements unavailable", state=State.UNKNOWN)
        c.failure("gpu", "GPU not probed", state=State.UNKNOWN)
    if c.source.models_root is None:
        c.failure("models-root", "no pinned models root configured", state=State.UNKNOWN)
    validation_evidence(c)


def ffmpeg(c: Collector) -> None:
    def version() -> None:
        output = c.query(("-version",))
        if not output.startswith("ffmpeg version "):
            raise ValueError("unrecognized ffmpeg version")
        c.observed("executable", str(c.source.executable), output.splitlines()[0])
        c.observed("version", output.splitlines()[0], output.splitlines()[0])
    c.attempt("executable", version)
    for name in ("volume", "amix", "volumedetect"):
        def filter_help(name: str = name) -> None:
            output = c.query(("-hide_banner", "-h", f"filter={name}"))
            if not re.search(rf"(?m)^Filter {re.escape(name)}\s*$", output):
                raise ValueError(f"required filter unavailable: {name}")
            c.observed("filter", name, output)
            if name == "amix":
                for key in ("duration", "normalize"):
                    match = re.search(rf"(?m)^\s+{key}\s+.*\(default ([^)]+)\)", output)
                    if match:
                        c.add(key, match[1], provenance=Provenance.TOOL_DEFAULT, verification=Verification.OBSERVED, default=True)
            if name == "volume":
                match = re.search(r'(?m)^\s+volume\s+.*\(default "([^"]+)"\)', output)
                if match:
                    c.add("volume", match[1], provenance=Provenance.TOOL_DEFAULT, verification=Verification.OBSERVED, default=True)
        c.attempt(f"filter.{name}", filter_help)
    def encoder() -> None:
        output = c.query(("-hide_banner", "-h", "encoder=pcm_s16le"))
        if not re.search(r"(?m)^Encoder pcm_s16le\b", output):
            raise ValueError("required PCM 16-bit encoder unavailable")
        c.observed("encoder", "pcm_s16le", output)
    c.attempt("encoder", encoder)
    def wav() -> None:
        output = c.query(("-hide_banner", "-h", "muxer=wav"))
        if not re.search(r"(?m)^Muxer wav\b", output):
            raise ValueError("required WAV muxer unavailable")
        c.observed("muxer", "wav", output)
    c.attempt("muxer", wav)


def ffplay(c: Collector) -> None:
    def version() -> None:
        output = c.query(("-version",))
        if not output.startswith("ffplay version "):
            raise ValueError("unrecognized ffplay version")
        c.observed("executable", str(c.source.executable), output.splitlines()[0])
        c.observed("version", output.splitlines()[0], output.splitlines()[0])
    c.attempt("executable", version)
    def options() -> None:
        output = c.query(("-hide_banner", "-h", "full"))
        for flag in ("nodisp", "autoexit", "vn", "i", "hide_banner", "loglevel"):
            if re.search(rf"(?m)^-{flag}(?:\s|$)", output):
                c.observed("option", f"-{flag}", f"listed by ffplay -h full: -{flag}")
            else:
                c.failure(f"option.{flag}", f"required option -{flag} not reported", state=State.UNAVAILABLE, readiness=Readiness.UNAVAILABLE)
    c.attempt("options", options)
    c.failure("audio-device", "audible playback/device readiness not tested", state=State.UNKNOWN)


def ffprobe(c: Collector) -> None:
    output = c.query(("-version",))
    if not output.startswith("ffprobe version "):
        raise ValueError("unrecognized ffprobe version")
    c.observed("executable", str(c.source.executable), output.splitlines()[0])
    c.observed("version", output.splitlines()[0], output.splitlines()[0])


DISCOVERERS = {
    "melisma": melisma, "openutau": openutau, "yue2-native": native_yue2,
    "yue2-instrumental": instrumental, "ffmpeg": ffmpeg,
    "ffplay": ffplay, "ffprobe": ffprobe,
}


class DiscoveryService:
    """Bounded, in-memory TTL cache keyed by the ENTIRE immutable input."""

    def __init__(
        self, *, probe: Probe | None = metadata_probe, ttl: float = 5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl < 0:
            raise ValueError("cache TTL must be nonnegative")
        self.probe, self.ttl, self.clock = probe, ttl, clock
        self._cache: dict[DiscoverySource, tuple[float, DiscoveryReport]] = {}

    def discover(self, source: DiscoverySource, *, force: bool = False) -> DiscoveryReport:
        cached = self._cache.get(source)
        if not force and cached is not None and self.clock() - cached[0] < self.ttl:
            return cached[1]
        c = Collector(source, self.probe)
        if source.kind == SourceKind.REMOTE:
            c.failure("discovery", "remote execution excluded from local refresh", state=State.UNKNOWN)
        else:
            c.configured()
            if self.probe is None and source.executable is not None and source.tool != "openutau":
                c.attempt("executable", c.executable_candidate)
            c.attempt("discovery", lambda: DISCOVERERS[source.tool](c))
        result = DiscoveryReport(source.identity, tuple(c.items))
        for item in result.issues:
            log = LOG.warning if item.state in (
                State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED, State.ERROR,
                State.UNRESOLVED,
            ) else LOG.info
            log(
                "discovery tool=%s source=%s contract=%s capability=%s state=%s readiness=%s: %s",
                source.tool, source.identity.location, source.identity.contract,
                item.key, item.state.value, item.readiness.value, item.reason,
            )
        if len(self._cache) >= 32:
            self._cache.clear()
        self._cache[source] = (self.clock(), result)
        return result

    def refresh(self, sources: tuple[DiscoverySource, ...]) -> tuple[DiscoveryReport, ...]:
        """Manual refresh never reuses stale metadata, including failed queries."""
        return tuple(self.discover(source, force=True) for source in sources)

    def invalidate(self) -> None:
        self._cache.clear()
