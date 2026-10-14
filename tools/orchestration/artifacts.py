"""Artifact checks with no dependency installation or resource discovery."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .process import ArtifactRecord, Command, ExpectedArtifact, ProcessResult, ProcessRunner


def existing_input(path: Path, cwd: Path) -> Path:
    resolved = absolute(path, cwd)
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise ValueError(f"input must be an existing nonempty file: {resolved}")
    with resolved.open("rb"):
        pass
    return resolved


def absolute(path: Path, cwd: Path) -> Path:
    return (cwd / path).resolve()


def new_output(path: Path, cwd: Path) -> Path:
    # Do not resolve the final component: an existing dangling symlink is
    # still an occupied destination, not permission to overwrite its target.
    path = Path(path)
    resolved = absolute(path.parent, cwd) / path.name
    if resolved.exists() or resolved.is_symlink():
        raise ValueError(f"output already exists: {resolved}")
    if not resolved.parent.is_dir():
        raise ValueError(f"output parent must exist: {resolved.parent}")
    return resolved


def read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def inspect_artifact(expected: ExpectedArtifact) -> ArtifactRecord:
    path = expected.path
    exists = path.is_file()
    size = None
    try:
        if not exists:
            raise ValueError("missing regular file")
        size = path.stat().st_size
        if size == 0 and not expected.allow_empty:
            raise ValueError("empty artifact")
        if expected.kind == "json":
            read_json(path)
        elif expected.kind == "ir":
            records = [
                json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            ]
            if not records or not all(isinstance(record, dict) for record in records):
                raise ValueError("invalid JSON Lines IR")
            header = records[0]
            if (
                header.get("type") != "header" or header.get("version") != 1
                or type(header.get("ticks_per_quarter")) is not int
                or header["ticks_per_quarter"] <= 0
            ):
                raise ValueError("invalid IR header")
            for note in records[1:]:
                if (
                    note.get("type") != "note"
                    or any(type(note.get(key)) is not int
                           for key in ("pitch", "onset", "duration"))
                    or not 0 <= note["pitch"] <= 127
                    or note["onset"] < 0 or note["duration"] <= 0
                    or note.get("binding") not in ("onset", "continuation", "untexted")
                    or (note["binding"] == "untexted" and note.get("syllable") is not None)
                    or (note["binding"] != "untexted"
                        and not isinstance(note.get("syllable"), str))
                ):
                    raise ValueError("invalid IR note")
        elif expected.kind == "abc":
            text = path.read_text(encoding="utf-8")
            if not all(
                any(line.startswith(prefix) for line in text.splitlines())
                for prefix in ("X:", "M:", "L:", "K:", "V: Vocal", "V: Ins")
            ):
                raise ValueError("missing Melisma ABC header or voices")
        elif expected.kind == "ustx":
            # Melisma's YAML subset, not a general USTX parser/resource loader.
            text = path.read_text(encoding="utf-8")
            if not all(
                any(line.startswith(prefix) for line in text.splitlines())
                for prefix in ("ustx_version:", "resolution:", "tracks:", "voice_parts:")
            ):
                raise ValueError("missing Melisma USTX fields")
        elif expected.kind not in ("file", "audio"):
            raise ValueError(f"unknown artifact kind: {expected.kind}")
        return ArtifactRecord(expected, exists, size, True)
    except (OSError, ValueError, UnicodeError) as error:
        return ArtifactRecord(expected, exists, size, False, str(error))


class AudioValidator:
    """Explicit ffprobe boundary; records diagnostics, not audio-quality claims."""

    def __init__(self, executable: Path, cwd: Path) -> None:
        self.cwd = cwd.resolve()
        self.executable = absolute(executable, self.cwd)

    def validate(
        self, path: Path, runner: ProcessRunner
    ) -> tuple[tuple[str, ...], ProcessResult]:
        command = Command(
            self.executable,
            (
                "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels:format=duration",
                "-of", "json", str(path),
            ),
            self.cwd,
        )
        result = runner.run(command)
        errors: list[str] = []
        if not result.success or result.stderr.strip():
            errors.append(f"audio probe failed: {path}: {result.error or result.stderr}")
        else:
            try:
                data = json.loads(result.stdout)
                stream = data["streams"][0]
                duration = float(data["format"]["duration"])
                if (
                    not stream["codec_name"]
                    or int(stream["sample_rate"]) <= 0
                    or int(stream["channels"]) <= 0
                    or not math.isfinite(duration)
                    or duration <= 0
                ):
                    raise ValueError("invalid audio stream or duration")
            except (ValueError, TypeError, KeyError, IndexError) as error:
                errors.append(f"invalid audio probe metadata: {path}: {error}")
        return tuple(errors), result
