"""Separate native 0.1.6 and pinned instrumental 0.1.5 operation contracts."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path

from .adapters import ToolAdapter
from .artifacts import absolute, existing_input, inspect_artifact, new_output, read_json
from .process import ArtifactRecord, ExpectedArtifact, Operation, OperationResult, ProcessRunner


NATIVE_FILES = (
    "audio.flac", "prefix.npy", "semantic.npy", "latent.npy", "request.json", "config.json",
)
HELPER_FILES = (
    "summary.json", "native/result.json", "native/request.json", "native/audio.flac",
    "native/score.abc", "native/semantic.npy", "native/latent.npy", "native/prefix.npy",
    "native/config.json", "score.abc", "prompt.txt", "repair-report.json",
)
HELPER_SCRIPT = Path("skills/yue2-music/instrumental/scripts/instrumental.py")
PREPARED_FILES = (
    "request.json", "score.abc", "prompt.txt", "lyrics.txt", "style.txt",
    "score-check.json", "repair-report.json",
)


def truncation_errors(receipt: dict[str, object]) -> list[str]:
    errors: list[str] = []
    if receipt.get("status") != "complete":
        errors.append("generation status is not complete")
    flags = receipt.get("truncated")
    if not isinstance(flags, dict):
        return [*errors, "missing truncation metadata"]
    for name in ("abc", "semantic"):
        if flags.get(name) is not False:
            errors.append(f"{name} truncation is true, missing, or invalid")
    return errors


def verify_manifest(
    root: Path, entries: object, required: tuple[str, ...]
) -> tuple[list[ArtifactRecord], list[str]]:
    errors: list[str] = []
    artifacts: list[ArtifactRecord] = []
    if not isinstance(entries, dict):
        return artifacts, ["missing artifact hash manifest"]
    if not set(required) <= entries.keys():
        errors.append("incomplete artifact hash manifest")
    for name, receipt in entries.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute():
            errors.append(f"invalid manifest path: {name!r}")
            continue
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()):
            errors.append(f"manifest path escapes result directory: {name}")
            continue
        artifact = inspect_artifact(
            ExpectedArtifact(path, allow_empty=name == "lyrics.txt")
        )
        if artifact.valid:
            try:
                if not isinstance(receipt, dict):
                    raise ValueError("invalid hash receipt")
                size = receipt.get("bytes")
                digest = receipt.get("sha256")
                if (
                    type(size) is not int or size != artifact.size
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise ValueError("invalid hash/size receipt")
                hasher = hashlib.sha256()
                with path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hasher.update(chunk)
                if hasher.hexdigest() != digest:
                    raise ValueError("artifact hash mismatch")
            except (OSError, ValueError) as error:
                artifact = replace(artifact, valid=False, error=str(error))
        artifacts.append(artifact)
        if not artifact.valid:
            errors.append(f"{name}: {artifact.error}")
    return artifacts, errors


def generation_completion(
    result: OperationResult, root: Path, *, helper: bool = False
) -> OperationResult:
    errors = list(result.errors)
    artifacts = list(result.artifacts)
    metadata: dict[str, object] = {}
    try:
        native_root = root / "native" if helper else root
        receipt = read_json(native_root / "result.json")
        metadata["native"] = receipt
        errors.extend(truncation_errors(receipt))
        required = NATIVE_FILES
        if helper or dict(result.operation.metadata).get("cot") != "off":
            required = (*required, "score.abc")
        records, issues = verify_manifest(native_root, receipt.get("artifacts"), required)
        artifacts.extend(records)
        errors.extend(issues)
        if helper:
            summary = read_json(root / "summary.json")
            metadata["summary"] = summary
            errors.extend(truncation_errors(summary))
            if summary.get("package_version") != "0.1.5":
                errors.append("instrumental helper package version must be 0.1.5")
            for field in ("exact_prefix_verified", "exact_abc_verified"):
                if summary.get(field) is not True:
                    errors.append(f"instrumental summary lacks {field} confirmation")
            delivery = read_json(root / "delivery-manifest.json")
            metadata["delivery"] = delivery
            records, issues = verify_manifest(root, delivery.get("files"), HELPER_FILES)
            artifacts.extend(records)
            errors.extend(issues)
    except (OSError, ValueError, UnicodeError) as error:
        errors.append(f"invalid YuE2 completion metadata: {error}")
    return replace(result, artifacts=tuple(artifacts), errors=tuple(errors),
                   completion=metadata)


def validate_identity(run_id: str, seed: int) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id):
        raise ValueError("run ID must be a safe directory name")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")


class NativeYuE2Adapter(ToolAdapter):
    tool = "yue2-native"
    operations = ("generate",)
    contract_version = "0.1.6"

    def build(
        self, output: Path, *, style: str, lyrics: str, run_id: str,
        seed: int, model: Path, vae: Path, device: str, cot: str,
        abc: Path | None = None,
    ) -> Operation:
        validate_identity(run_id, seed)
        if not style or not isinstance(lyrics, str):
            raise ValueError("explicit style and lyrics are required")
        if cot not in ("full", "melody", "off") or (abc is not None and cot == "off"):
            raise ValueError("invalid COT mode for supplied ABC")
        if not device or device.startswith("-"):
            raise ValueError("an explicit device is required")
        output = new_output(output, self.cwd)
        arguments = [
            "-B", "-m", "yue2.cli", "generate", f"--style={style}", f"--lyrics={lyrics}",
            "--cot", cot, "--seed", str(seed), "--id", run_id, "--output", str(output),
            "--model", str(absolute(model, self.cwd)), "--vae", str(absolute(vae, self.cwd)),
            "--device", device, "--offline",
        ]
        if abc is not None:
            arguments.extend(("--abc-file", str(existing_input(abc, self.cwd))))
        root = output / run_id
        expected = (
            ExpectedArtifact(root / "audio.flac", "audio"),
            ExpectedArtifact(root / "result.json", "json"),
        )
        return self._operation(
            "generate", tuple(arguments), expected,
            metadata=(("contract_version", self.contract_version), ("cot", cot),
                      ("result_root", str(root)), ("stage", "audio")),
            environment=(("PYTHONPATH", str(self.cwd / "src")), ("PYTHONHOME", None)),
        )

    def complete(self, result: OperationResult, runner: ProcessRunner) -> OperationResult:
        root = Path(dict(result.operation.metadata)["result_root"])
        return generation_completion(result, root)


class InstrumentalAdapter(ToolAdapter):
    tool = "yue2-instrumental"
    operations = ("prepare", "generate", "verify")
    contract_version = "0.1.5"

    @property
    def script(self) -> Path:
        return self.cwd / HELPER_SCRIPT

    def _helper_operation(
        self, name: str, arguments: tuple[str, ...],
        expected: tuple[ExpectedArtifact, ...], root: Path,
    ) -> Operation:
        return self._operation(
            name, ("-B", str(self.script), *arguments), expected,
            metadata=(("contract_version", self.contract_version), ("result_root", str(root))),
            # Never inject the current 0.1.6 source into the pinned interpreter.
            environment=(("PYTHONPATH", None), ("PYTHONHOME", None)),
        )

    def prepare(
        self, abc: Path, output: Path, *, style: str, run_id: str, seed: int
    ) -> Operation:
        validate_identity(run_id, seed)
        if not style or style.startswith("-"):
            raise ValueError("an explicit instrumental style is required")
        abc = existing_input(abc, self.cwd)
        output = new_output(output, self.cwd)
        return self._helper_operation(
            "prepare",
            ("cover", "--abc", str(abc), "--composer", "agent", "--style", style,
             "--empty-lyrics", "--id", run_id, "--seed", str(seed),
             "--output", str(output), "--prepare-only", "--offline"),
            (ExpectedArtifact(output / "prepared" / "prepared-manifest.json", "json"),),
            output,
        )

    def generate(
        self, prepared: Path, output: Path, *, models_root: Path, device: str
    ) -> Operation:
        prepared = absolute(prepared, self.cwd)
        existing_input(prepared / "prepared-manifest.json", self.cwd)
        output = new_output(output, self.cwd)
        if not device or device.startswith("-"):
            raise ValueError("an explicit device is required")
        return self._helper_operation(
            "generate",
            ("generate", "--prepared", str(prepared), "--output", str(output),
             "--models-root", str(absolute(models_root, self.cwd)),
             "--device", device, "--offline"),
            (ExpectedArtifact(output / "native" / "audio.flac", "audio"),
             ExpectedArtifact(output / "native" / "result.json", "json"),
             ExpectedArtifact(output / "summary.json", "json"),
             ExpectedArtifact(output / "delivery-manifest.json", "json")),
            output,
        )

    def verify(self, directory: Path) -> Operation:
        directory = absolute(directory, self.cwd)
        if not directory.is_dir():
            raise ValueError("verification directory must exist")
        return self._helper_operation("verify", ("verify", str(directory)), (), directory)

    def complete(self, result: OperationResult, runner: ProcessRunner) -> OperationResult:
        root = Path(dict(result.operation.metadata)["result_root"])
        if result.operation.name == "generate":
            return generation_completion(result, root, helper=True)
        if result.operation.name == "prepare":
            errors = list(result.errors)
            artifacts = list(result.artifacts)
            metadata: dict[str, object] = {}
            try:
                prepared = root / "prepared"
                receipt = read_json(prepared / "prepared-manifest.json")
                metadata["prepared"] = receipt
                entries = receipt.get("files")
                if not isinstance(entries, dict) or not entries:
                    raise ValueError("empty preparation manifest")
                records, issues = verify_manifest(prepared, entries, PREPARED_FILES)
                artifacts.extend(records)
                errors.extend(issues)
            except (OSError, ValueError, UnicodeError) as error:
                errors.append(f"invalid preparation metadata: {error}")
            return replace(result, artifacts=tuple(artifacts), errors=tuple(errors),
                           completion=metadata)
        try:
            metadata = read_verification(result.process.stdout)
            return replace(result, completion=metadata)
        except (ValueError, TypeError) as error:
            return replace(result, errors=(*result.errors, str(error)))


def read_verification(stdout: str) -> dict[str, object]:
    import json

    metadata = json.loads(stdout)
    if not isinstance(metadata, dict) or metadata.get("verified") is not True:
        raise ValueError("helper did not report verified completion")
    return metadata
