"""Versioned local configuration. Saved identities are never readiness evidence."""

from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import types
from dataclasses import MISSING, dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import get_args, get_origin, get_type_hints
from urllib.parse import urlsplit

from .capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, SourceIdentity, SourceKind,
    State, Verification, revalidate_selection,
)
from .discovery import DiscoveryService, DiscoverySource


SCHEMA_VERSION = 1


class ConfigurationError(ValueError):
    pass


class UnsupportedVersion(ConfigurationError):
    pass


@dataclass(frozen=True)
class AuthReference:
    kind: str
    name: str


@dataclass(frozen=True)
class SSHMetadata:
    host: str
    port: int = field(metadata={"minimum": 1, "maximum": 65535, "step": 1})
    user: str
    identity_file: Path | None = None


@dataclass(frozen=True)
class AsyncAPI:
    contract: str
    version: str
    job_model: str = "async-jobs"
    submit_path: str | None = None
    status_path: str | None = None
    result_path: str | None = None
    artifacts_path: str | None = None


@dataclass(frozen=True)
class SamplingDefaults:
    temperature: float | None = field(default=None, metadata={"minimum": 0})
    top_p: float | None = field(
        default=None, metadata={"minimum": 0, "maximum": 1},
    )
    top_k: int | None = field(default=None, metadata={"minimum": 0})
    repetition_penalty: float | None = field(default=None, metadata={"minimum": 0})
    repetition_window: int | None = field(default=None, metadata={"minimum": 0})
    min_new_tokens: int | None = field(default=None, metadata={"minimum": 0})
    max_new_tokens: int | None = field(default=None, metadata={"minimum": 0})


@dataclass(frozen=True)
class GenerationDefaults:
    style: str | None = None
    seed: int | None = None
    cot: str | None = None
    device: str | None = None
    backend: str | None = None
    quantization: str | None = None
    budget: float | None = field(default=None, metadata={"exclusive_minimum": 0})
    cfg_scale: float | None = field(default=None, metadata={"exclusive_minimum": 0})
    stage: str | None = None
    offline: bool | None = None
    composer: str | None = None
    empty_lyrics: bool | None = None
    overlap_policy: str | None = None
    abc_sampling: SamplingDefaults | None = None
    semantic_sampling: SamplingDefaults | None = None
    ode_steps: int | None = field(default=None, metadata={"exclusive_minimum": 0})
    ode_method: str | None = None
    context: int | None = field(default=None, metadata={"exclusive_minimum": 0})
    protocol: str | None = None


@dataclass(frozen=True)
class ModelIdentity:
    identity: str
    revision: str | None = None
    weights_sha256: str | None = None


@dataclass(frozen=True)
class ExecutionContract:
    operation: str
    contract: str
    version: str
    runtime: str | None = None
    model: ModelIdentity | None = None
    vae: ModelIdentity | None = None
    pinned_models_root: str | None = None
    defaults: GenerationDefaults = GenerationDefaults()


@dataclass(frozen=True)
class YuE2Target:
    id: str
    type: str
    contracts: tuple[ExecutionContract, ...]
    endpoint: str | None = None
    api: AsyncAPI | None = None
    auth: AuthReference | None = None
    local_root: Path | None = None
    local_executable: Path | None = None
    administration: SSHMetadata | None = None


@dataclass(frozen=True)
class SavedSelection:
    source: SourceIdentity
    key: str
    value: str
    provenance: Provenance = Provenance.CONFIGURED
    origin: Provenance | None = None

    def capability(self) -> Capability:
        return Capability(
            self.source, self.key, self.value, self.provenance,
            Verification.UNVERIFIED, State.CONFIGURED, origin=self.origin,
            reason="saved selection only; current validation required",
        )


@dataclass(frozen=True)
class MixSettings:
    instrumental_gain: float = field(default=1.0, metadata={"exclusive_minimum": 0})
    vocal_gain: float = field(default=0.5, metadata={"exclusive_minimum": 0})
    master_gain: float = field(default=0.707945784, metadata={"exclusive_minimum": 0})
    duration: str = "first"
    normalize: bool = False


@dataclass(frozen=True)
class Preset:
    id: str
    midi: Path
    lyrics: Path
    selections: tuple[SavedSelection, ...]
    target_id: str
    operation: str
    generation: GenerationDefaults
    mix: MixSettings = MixSettings()
    provenance: Provenance = Provenance.CONFIGURED


@dataclass(frozen=True)
class Configuration:
    sources: tuple[DiscoverySource, ...] = ()
    yue2_targets: tuple[YuE2Target, ...] = ()
    workspace: Path | None = None
    output: Path | None = None
    previous: tuple[SavedSelection, ...] = ()
    presets: tuple[Preset, ...] = ()


@dataclass(frozen=True)
class ConfigurationIssue:
    location: str
    state: State
    readiness: Readiness
    reason: str


@dataclass(frozen=True)
class PresetValidation:
    preset: Preset
    selections: tuple[Capability, ...]
    issues: tuple[ConfigurationIssue, ...]

    @property
    def valid(self) -> bool:
        return not self.issues and all(
            item.state not in (
                State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED,
                State.ERROR, State.UNRESOLVED,
            ) for item in self.selections
        )


@dataclass(frozen=True)
class LoadedConfiguration:
    configuration: Configuration
    reports: tuple[DiscoveryReport, ...]
    configured: tuple[Capability, ...]
    previous: tuple[Capability, ...]
    presets: tuple[PresetValidation, ...]
    issues: tuple[ConfigurationIssue, ...]

    def require_preset(self, preset_id: str) -> Preset:
        result = next((p for p in self.presets if p.preset.id == preset_id), None)
        if result is None:
            raise ConfigurationError(f"unknown preset: {preset_id}")
        if not result.valid:
            reasons = [item.reason for item in result.issues]
            reasons.extend(
                f"{item.key}: {item.reason}" for item in result.selections
                if item.state in (
                    State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED,
                    State.ERROR, State.UNRESOLVED,
                )
            )
            raise ConfigurationError("preset requires validation: " + "; ".join(reasons))
        return result.preset


def _encode(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ConfigurationError("unsupported configuration value")


def _decode(kind: type, value: object, location: str = "configuration") -> object:
    origin, args = get_origin(kind), get_args(kind)
    if origin is types.UnionType:
        for choice in args:
            try:
                return _decode(choice, value, location)
            except ConfigurationError:
                continue
        raise ConfigurationError(f"{location}: invalid optional value")
    if kind is type(None) and value is None:
        return None
    if origin is tuple:
        if not isinstance(value, list):
            raise ConfigurationError(f"{location}: expected array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], item, location) for item in value)
        if len(value) != len(args):
            raise ConfigurationError(f"{location}: incorrect tuple length")
        return tuple(_decode(t, item, location) for t, item in zip(args, value))
    if kind is Path and isinstance(value, str) and value and "\0" not in value:
        return Path(value)
    if isinstance(kind, type) and issubclass(kind, Enum):
        try:
            return kind(value)
        except (ValueError, TypeError) as error:
            raise ConfigurationError(f"{location}: invalid enum value") from error
    if is_dataclass(kind):
        if not isinstance(value, dict):
            raise ConfigurationError(f"{location}: expected object")
        hints = get_type_hints(kind)
        if set(value) - hints.keys():
            # Unknown fields are rejected, including credential-bearing extensions.
            raise ConfigurationError(f"{location}: unknown or prohibited fields")
        kwargs = {
            name: _decode(hints[name], item, f"{location}.{name}")
            for name, item in value.items()
        }
        try:
            return kind(**kwargs)
        except (TypeError, ValueError) as error:
            raise ConfigurationError(f"{location}: invalid or missing fields") from error
    if kind is str and isinstance(value, str) and "\0" in value:
        raise ConfigurationError(f"{location}: NUL is prohibited")
    if kind in (str, int, bool) and type(value) is kind:
        return value
    if kind is float and type(value) in (int, float):
        try:
            number = float(value)
        except OverflowError as error:
            raise ConfigurationError(f"{location}: number out of range") from error
        if math.isfinite(number):
            return number
    raise ConfigurationError(f"{location}: invalid value type")


def _nonsecret(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"password|private.?key|api.?key|bearer|credential|token|secret", key, re.I):
                # No fields bearing credential contents exist in this schema.
                # Token-count generation settings are not credential fields.
                if key not in ("min_new_tokens", "max_new_tokens"):
                    raise ConfigurationError("credential fields are prohibited")
            _nonsecret(item)
    elif isinstance(value, (list, tuple)):
        if len(value) == 2 and isinstance(value[0], str) and re.search(
            r"password|private.?key|api.?key|bearer|credential|token|secret", value[0], re.I,
        ) and not value[0].endswith((".min_new_tokens", ".max_new_tokens")):
            raise ConfigurationError("credential-bearing key/value pairs are prohibited")
        for item in value:
            _nonsecret(item)
    elif isinstance(value, str):
        if re.search(
            r"-----BEGIN .*PRIVATE KEY-----|\bBearer\s+\S+|"
            r"(?:password|api[_-]?key|access[_-]?token|runpod[_-]?(?:api[_-]?)?token)\s*[:=]\s*\S+",
            value, re.I,
        ):
            raise ConfigurationError("credential contents are prohibited")
        if "://" in value:
            url = urlsplit(value)
            if url.username is not None or url.password is not None or url.query or url.fragment:
                raise ConfigurationError("credential-capable URL components are prohibited")


def _absolute(path: Path | None) -> None:
    if path is not None and (not path.is_absolute() or ".." in path.parts):
        raise ConfigurationError("local configured paths must be explicit absolute paths")


def _identifier(value: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
        raise ConfigurationError("invalid stable identifier")


def _selection_key(key: str) -> None:
    allowed = {
        "singer", "phonemizer", "renderer", "format", "operation", "model", "vae",
        "id", "seed", "cot", "device", "backend", "quantization", "budget",
        "cfg-scale", "stage", "offline", "composer", "duration", "normalize",
        "volume", "overlap-policy",
    }
    if key not in allowed and not re.fullmatch(
        r"(?:SongRequest|GenerationConfig|Sampling)\.[A-Za-z0-9_.]+", key,
    ):
        raise ConfigurationError("unsupported or credential-bearing selection key")
    if re.search(r"password|api.?key|token|secret|credential|private.?key", key, re.I):
        if not key.endswith((".min_new_tokens", ".max_new_tokens")):
            raise ConfigurationError("credential selections are prohibited")


def _unique(values: tuple[str, ...]) -> None:
    if len(values) != len(set(values)):
        raise ConfigurationError("duplicate configured identity")


def _generation(defaults: GenerationDefaults) -> None:
    for field, choices in (
        ("cot", ("full", "melody", "off")),
        ("backend", ("torch", "torch-eager", "vllm")),
        ("quantization", ("none", "fp8")),
        ("stage", ("plan", "audio")),
        ("composer", ("model", "agent")),
        ("overlap_policy", ("vocal", "error")),
    ):
        value = getattr(defaults, field)
        if value is not None and value not in choices:
            raise ConfigurationError(f"invalid generation {field}")
    for value in (defaults.budget, defaults.cfg_scale, defaults.ode_steps, defaults.context):
        if value is not None and value <= 0:
            raise ConfigurationError("generation limits must be positive")
    for sampling in (defaults.abc_sampling, defaults.semantic_sampling):
        if sampling is None:
            continue
        for field in fields(sampling):
            value = getattr(sampling, field.name)
            if value is not None and value < 0:
                raise ConfigurationError("sampling values must be nonnegative")
        if sampling.top_p is not None and sampling.top_p > 1:
            raise ConfigurationError("top_p must not exceed one")
        if (
            sampling.min_new_tokens is not None and sampling.max_new_tokens is not None
            and sampling.min_new_tokens > sampling.max_new_tokens
        ):
            raise ConfigurationError("inverted sampling token limits")


def _preset(preset: Preset) -> None:
    _identifier(preset.id)
    _identifier(preset.target_id)
    if preset.operation not in ("native", "instrumental"):
        raise ConfigurationError("unknown preset operation")
    if preset.midi.suffix.lower() not in (".mid", ".midi") or preset.lyrics.suffix.lower() != ".txt":
        raise ConfigurationError("presets reference MIDI and lyric inputs, not generated artifacts")
    if ".." in preset.midi.parts or ".." in preset.lyrics.parts:
        raise ConfigurationError("preset input must not traverse parents")
    if any(
        value <= 0 for value in (
            preset.mix.instrumental_gain, preset.mix.vocal_gain, preset.mix.master_gain,
        )
    ) or preset.mix.duration not in ("first", "shortest", "longest"):
        raise ConfigurationError("invalid mix settings")
    _unique(tuple(f"{s.source}|{s.key}" for s in preset.selections))
    _generation(preset.generation)


def _validate(config: Configuration) -> None:
    _absolute(config.workspace)
    _absolute(config.output)
    _unique(tuple(str(source.identity) for source in config.sources))
    _unique(tuple(target.id for target in config.yue2_targets))
    _unique(tuple(preset.id for preset in config.presets))
    _unique(tuple(f"{s.source}|{s.key}" for s in config.previous))
    for source in config.sources:
        if source.kind != SourceKind.LOCAL or source.target is not None:
            raise ConfigurationError("sources are local identities; configure execution targets separately")
        if source.contract is not None and not source.contract.strip():
            raise ConfigurationError("configured source contract must not be blank")
        for path in (source.root, source.executable, source.models_root, source.validation_report):
            _absolute(path)
        if source.tool != "openutau" and source.singer_roots:
            raise ConfigurationError("singer authorization belongs to an OpenUtau source")
        for root in source.singer_roots:
            _absolute(root.path)
        for key, value in (*source.configured, *source.references):
            _selection_key(key)
            if not key or not value:
                raise ConfigurationError("configured selections require a key and value")
        # Issue #3 accepts explicit environment evidence; never persist arbitrary
        # environment variables, since those may hold secrets.
        for key, value in source.environment:
            if key != "YUE2_KIT":
                raise ConfigurationError("only the non-secret YUE2_KIT path is supported")
            _absolute(Path(value))
    for target in config.yue2_targets:
        _identifier(target.id)
        if target.type not in ("remote-http", "local"):
            raise ConfigurationError("unsupported execution-target type")
        if not target.contracts:
            raise ConfigurationError("execution target must declare operation contracts")
        _unique(tuple(c.operation for c in target.contracts))
        for contract in target.contracts:
            if contract.operation not in ("native", "instrumental") or not contract.contract or not contract.version:
                raise ConfigurationError("explicit native/instrumental contract identities required")
            for model in (contract.model, contract.vae):
                if model is not None and (
                    not model.identity or model.weights_sha256 is not None
                    and re.fullmatch(r"[a-f0-9]{64}", model.weights_sha256) is None
                ):
                    raise ConfigurationError("invalid model identity/hash")
            _generation(contract.defaults)
        if target.type == "remote-http":
            if target.endpoint is None or target.api is None:
                raise ConfigurationError("remote-http requires HTTPS endpoint and asynchronous API metadata")
            url = urlsplit(target.endpoint)
            try:
                port = url.port
            except ValueError as error:
                raise ConfigurationError("invalid HTTPS endpoint port") from error
            if (
                url.scheme != "https" or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment
                or any(c.isspace() for c in target.endpoint)
                or "\\" in target.endpoint
                or port is not None and not 0 < port < 65536
            ):
                raise ConfigurationError("remote endpoint must be credential-free HTTPS")
            if target.local_root is not None or target.local_executable is not None:
                raise ConfigurationError("remote-http must not use local runtime paths")
        else:
            if target.endpoint is not None or target.api is not None or target.auth is not None:
                raise ConfigurationError("local target must not contain HTTP configuration")
            if target.local_root is None:
                raise ConfigurationError("local target requires its own explicit runtime root")
            _absolute(target.local_root)
            _absolute(target.local_executable)
        if target.api is not None:
            if not target.api.contract or not target.api.version or target.api.job_model != "async-jobs":
                raise ConfigurationError("explicit asynchronous job contract required")
            for route in (
                target.api.submit_path, target.api.status_path,
                target.api.result_path, target.api.artifacts_path,
            ):
                if route is not None and (
                    not route.startswith("/") or route.startswith("//")
                    or "?" in route or "#" in route or ".." in route.split("/")
                    or "\\" in route or any(c.isspace() for c in route)
                ):
                    raise ConfigurationError("API metadata routes must be service-relative paths")
        if target.auth is not None:
            if target.auth.kind not in ("environment", "secret-store") or not target.auth.name:
                raise ConfigurationError("authentication must be a non-secret reference")
            if target.auth.kind == "environment" and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", target.auth.name,
            ) is None:
                raise ConfigurationError("invalid authentication environment-variable name")
            if target.auth.kind == "secret-store" and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]*", target.auth.name,
            ) is None:
                raise ConfigurationError("invalid secret-store identifier")
        if target.administration is not None:
            admin = target.administration
            if not admin.host or not admin.user or not 0 < admin.port < 65536:
                raise ConfigurationError("invalid administrative SSH metadata")
            _absolute(admin.identity_file)
    for selection in (*config.previous, *(s for p in config.presets for s in p.selections)):
        if not selection.key or not selection.value or not selection.source.location or not selection.source.contract:
            raise ConfigurationError("selection identity, key and value must be explicit")
        _selection_key(selection.key)
    for preset in config.presets:
        _preset(preset)


def _checked(config: Configuration) -> Configuration:
    raw = _encode(config)
    _nonsecret(raw)
    result = _decode(Configuration, raw)
    _validate(result)
    return replace(
        result, sources=tuple(
            replace(source, contract=source.identity.contract) for source in result.sources
        ),
    )


def configuration_value(config: Configuration) -> dict[str, object]:
    """Checked, non-secret serialization without I/O, discovery, or persistence."""
    return _encode(_checked(config))


def configuration_from_value(value: object) -> Configuration:
    """Validate an editor draft using exactly the persistence model, without I/O."""
    _nonsecret(value)
    return _checked(_decode(Configuration, value))


def configuration_structure(kind: type = Configuration) -> dict[str, object]:
    """Describe the actual model for editors; never resolve paths or resources."""
    origin, args = get_origin(kind), get_args(kind)
    if origin is types.UnionType:
        nonnull = next(choice for choice in args if choice is not type(None))
        return {**configuration_structure(nonnull), "nullable": True}
    if origin is tuple:
        return {
            "kind": "array",
            "items": [configuration_structure(t) for t in args if t is not Ellipsis],
            "repeated": args[-1] is Ellipsis,
        }
    if is_dataclass(kind):
        hints = get_type_hints(kind)
        properties = {}
        for definition in fields(kind):
            description = configuration_structure(hints[definition.name])
            description.update(definition.metadata)
            if definition.default is not MISSING:
                description["default"] = _encode(definition.default)
            elif definition.default_factory is not MISSING:
                description["default"] = _encode(definition.default_factory())
            properties[definition.name] = description
        return {"kind": "object", "properties": properties}
    if isinstance(kind, type) and issubclass(kind, Enum):
        return {"kind": "text", "choices": [item.value for item in kind]}
    if kind in (int, float):
        return {"kind": "number", "step": 1 if kind is int else "any"}
    return {"kind": "boolean" if kind is bool else "text"}


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        if key in result:
            raise ConfigurationError("duplicate JSON field")
        result[key] = value
    return result


def _document(path: Path, kind: str) -> object:
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream, object_pairs_hook=_pairs)
    if not isinstance(data, dict) or set(data) != {"schema_version", "kind", "value"}:
        raise ConfigurationError("expected versioned configuration document")
    version = data["schema_version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise UnsupportedVersion(f"unsupported schema version: {version!r}; expected {SCHEMA_VERSION}")
    if data["kind"] != kind:
        raise ConfigurationError("incorrect configuration document kind")
    _nonsecret(data["value"])
    return data["value"]


def _atomic_write(path: Path, kind: str, value: object) -> None:
    # Serialize before touching the destination or creating the temporary file.
    contents = json.dumps(
        {"schema_version": SCHEMA_VERSION, "kind": kind, "value": _encode(value)},
        indent=2, ensure_ascii=True, allow_nan=False,
    ) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_configuration(path: Path, config: Configuration) -> None:
    _atomic_write(path, "melisma-configuration", _checked(config))


def save_preset(path: Path, preset: Preset) -> None:
    checked = _checked(Configuration(presets=(preset,))).presets[0]
    _atomic_write(path, "melisma-preset", checked)


def _path_issue(path: Path, location: str, *, directory: bool) -> ConfigurationIssue | None:
    try:
        mode = path.stat().st_mode
        matches_type = stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)
        if matches_type:
            return None
    except (FileNotFoundError, NotADirectoryError):
        pass
    except PermissionError:
        return ConfigurationIssue(
            location, State.UNAUTHORIZED, Readiness.UNAUTHORIZED,
            f"configured path is inaccessible: {path}",
        )
    except OSError as error:
        return ConfigurationIssue(location, State.ERROR, Readiness.UNKNOWN, str(error))
    return ConfigurationIssue(
        location, State.UNAVAILABLE, Readiness.UNAVAILABLE,
        f"configured {'directory' if directory else 'file'} is missing, moved, or the wrong type: {path}",
    )


def validate_configuration(config: Configuration) -> LoadedConfiguration:
    """Refresh explicit local metadata only; never execute even help commands."""
    config = _checked(config)
    reports = DiscoveryService(probe=None).refresh(config.sources)
    identities = {source.identity for source in config.sources}
    if any(report.source not in identities for report in reports):
        raise ConfigurationError("capability report belongs to an unconfigured source")
    if len({r.source for r in reports}) != len(reports):
        raise ConfigurationError("duplicate capability report")
    issues: list[ConfigurationIssue] = []
    for source in config.sources:
        paths = (
            (source.root, "root", True), (source.executable, "executable", False),
            (source.models_root, "models_root", True),
            (source.validation_report, "validation_report", False),
            *((root.path, "singer_roots", True) for root in source.singer_roots),
        )
        for path, name, directory in paths:
            if path is not None:
                issue = _path_issue(path, f"{source.tool}.{name}", directory=directory)
                if issue is not None:
                    issues.append(issue)
    for path, name in ((config.workspace, "workspace"), (config.output, "output")):
        if path is not None:
            issue = _path_issue(path, name, directory=True)
            if issue is not None:
                issues.append(issue)
    for report in reports:
        issues.extend(
            ConfigurationIssue(f"{report.source.tool}.{item.key}", item.state, item.readiness, item.reason)
            for item in report.capabilities
            if item.reason and item.state in (
                State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED, State.ERROR, State.UNRESOLVED,
            )
        )
    for target in config.yue2_targets:
        issues.append(ConfigurationIssue(
            f"target.{target.id}", State.UNKNOWN, Readiness.UNKNOWN,
            "configured target only; execution/health readiness not checked",
        ))
        if target.type == "local":
            for path, name, directory in (
                (target.local_root, "root", True),
                (target.local_executable, "executable", False),
            ):
                if path is not None:
                    issue = _path_issue(path, f"target.{target.id}.{name}", directory=directory)
                    if issue is not None:
                        issues.append(issue)

    def revalidate(saved: SavedSelection) -> Capability:
        report = next((r for r in reports if r.source == saved.source), None)
        if report is None:
            return Capability(
                saved.source, saved.key, saved.value, saved.provenance,
                Verification.UNVERIFIED, State.UNKNOWN,
                reason="saved source is unconfigured, moved, or has no current capability report",
                origin=saved.origin or saved.provenance,
            )
        return revalidate_selection(report, saved.capability())

    configured = tuple(
        revalidate(SavedSelection(source.identity, key, value, provenance))
        for source in config.sources
        for pairs, provenance in (
            (source.configured, Provenance.CONFIGURED),
            (source.references, Provenance.REFERENCE),
        )
        for key, value in pairs
    )
    previous = tuple(revalidate(s) for s in config.previous)
    validations: list[PresetValidation] = []
    for preset in config.presets:
        selected = tuple(revalidate(s) for s in preset.selections)
        preset_issues: list[ConfigurationIssue] = []
        for singer in (s for s in selected if s.key == "singer"):
            singer_type = dict(singer.evidence).get("singer_type")
            renderer = next((
                s for s in selected if s.key == "renderer" and s.source == singer.source
            ), None)
            renderer_type = dict(renderer.evidence).get("singer_type") if renderer else None
            if (
                singer_type and singer_type != "unknown" and renderer_type
                and singer_type.casefold() != renderer_type.casefold()
            ):
                preset_issues.append(ConfigurationIssue(
                    preset.id, State.ERROR, Readiness.UNKNOWN,
                    "selected renderer does not support the selected singer type",
                ))
        target = next((t for t in config.yue2_targets if t.id == preset.target_id), None)
        contract = next((
            c for c in target.contracts if c.operation == preset.operation
        ), None) if target else None
        if target is None or contract is None:
            preset_issues.append(ConfigurationIssue(
                preset.id, State.UNAVAILABLE, Readiness.UNAVAILABLE,
                "selected target is missing or does not declare the selected operation",
            ))
        else:
            preset_issues.append(ConfigurationIssue(
                preset.id, State.UNKNOWN, Readiness.UNKNOWN,
                "target operation is configured, not currently capability/health validated",
            ))
        melisma_sources = tuple(s for s in config.sources if s.tool == "melisma")
        for path in (preset.midi, preset.lyrics):
            if not path.is_absolute() and len(melisma_sources) != 1:
                preset_issues.append(ConfigurationIssue(
                    preset.id, State.UNKNOWN, Readiness.UNKNOWN,
                    "relative preset inputs require exactly one configured Melisma root",
                ))
                continue
            resolved = path if path.is_absolute() else melisma_sources[0].root / path
            issue = _path_issue(resolved, f"{preset.id}.input", directory=False)
            if issue is not None:
                preset_issues.append(issue)
        validations.append(PresetValidation(preset, selected, tuple(preset_issues)))
    issues.extend(
        ConfigurationIssue(f"configured.{s.source.tool}.{s.key}", s.state, s.readiness, s.reason)
        for s in configured if s.reason
    )
    issues.extend(
        ConfigurationIssue(f"previous.{s.key}", s.state, s.readiness, s.reason)
        for s in previous if s.reason
    )
    for validation in validations:
        issues.extend(validation.issues)
        issues.extend(
            ConfigurationIssue(f"{validation.preset.id}.{s.key}", s.state, s.readiness, s.reason)
            for s in validation.selections if s.reason
        )
    return LoadedConfiguration(
        config, reports, configured, previous, tuple(validations), tuple(issues),
    )


def load_configuration(path: Path) -> LoadedConfiguration:
    config = _decode(Configuration, _document(path, "melisma-configuration"))
    return validate_configuration(config)


def load_preset(path: Path, config: Configuration) -> PresetValidation:
    preset = _decode(Preset, _document(path, "melisma-preset"))
    return validate_configuration(Configuration(
        config.sources, config.yue2_targets, config.workspace, config.output,
        config.previous, (preset,),
    )).presets[0]


def gloria_preset(*, openutau: SourceIdentity, target_id: str) -> Preset:
    """Restore the reference workflow, not the mismatched historical outputs."""
    return Preset(
        "gloria-five-notes", Path("examples/five-notes.mid"), Path("examples/gloria.txt"),
        tuple(
            SavedSelection(openutau, key, value, Provenance.REFERENCE)
            for key, value in (
                ("singer", "TIGER_DS_v102"),
                ("phonemizer", "OpenUtau.Core.DiffSinger.DiffSingerARPAPlusEnglishPhonemizer"),
                ("renderer", "DIFFSINGER"),
            )
        ),
        target_id, "instrumental",
        GenerationDefaults(
            style="Instrumental piano, no vocals, no singing, no choir, no spoken words",
            seed=831001, composer="agent", empty_lyrics=True,
            overlap_policy="vocal", offline=True,
        ),
        provenance=Provenance.REFERENCE,
    )
