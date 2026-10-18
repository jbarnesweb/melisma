"""Narrow, typed construction surfaces; source support is not runtime readiness."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from ..melisma_pipeline import (
    PipelineError, parse_volumedetect, validate_final_mix_analysis,
)
from .artifacts import AudioValidator, absolute, existing_input, inspect_artifact, new_output
from .capabilities import DiscoveryReport
from .discovery import DiscoveryService, DiscoverySource
from .process import (
    Command,
    ExpectedArtifact,
    Operation,
    OperationInput,
    OperationResult,
    ProcessResult,
    ProcessRunner,
    ProcessState,
)


class Adapter(Protocol):
    tool: str
    operations: tuple[str, ...]

    def execute(self, operation: Operation, runner: ProcessRunner) -> OperationResult: ...

    def discover(
        self, service: DiscoveryService, source: DiscoverySource | None = None, *,
        refresh: bool = False,
    ) -> DiscoveryReport: ...


class ToolAdapter:
    tool = ""
    operations: tuple[str, ...] = ()

    def __init__(
        self, executable: Path, cwd: Path, *, audio: AudioValidator | None = None
    ) -> None:
        self.cwd = cwd.resolve()
        self.executable = absolute(executable, self.cwd)
        self.audio = audio

    def discover(
        self, service: DiscoveryService, source: DiscoverySource | None = None, *,
        refresh: bool = False,
    ) -> DiscoveryReport:
        source = source or DiscoverySource(self.tool, self.cwd, self.executable)
        if (
            source.tool != self.tool or source.root != self.cwd
            or source.executable != self.executable
        ):
            raise ValueError("discovery source must match the adapter")
        return service.discover(source, force=refresh)

    def _operation(
        self,
        name: str,
        arguments: tuple[str, ...],
        expected: tuple[ExpectedArtifact, ...],
        *,
        metadata: tuple[tuple[str, str], ...] = (),
        environment: tuple[tuple[str, str | None], ...] = (),
        inputs: tuple[OperationInput, ...] = (),
    ) -> Operation:
        if name not in self.operations:
            raise ValueError(f"unsupported {self.tool} operation: {name}")
        return Operation(
            self.tool, name,
            Command(self.executable, arguments, self.cwd, environment),
            expected, metadata, inputs,
        )

    def execute(self, operation: Operation, runner: ProcessRunner) -> OperationResult:
        if operation.tool != self.tool or operation.name not in self.operations:
            raise ValueError("operation belongs to a different adapter")
        occupied = [
            artifact.path for artifact in operation.expected
            if artifact.path.exists() or artifact.path.is_symlink()
        ]
        if occupied:
            process = ProcessResult(
                operation.command, ProcessState.REJECTED, None,
                error=f"refusing existing outputs: {occupied}",
            )
        else:
            process = self.run(operation, runner)
        return self.finish(operation, process, runner)

    def run(self, operation: Operation, runner: ProcessRunner) -> ProcessResult:
        return runner.run(operation.command)

    def finish(
        self, operation: Operation, process: ProcessResult, runner: ProcessRunner
    ) -> OperationResult:
        artifacts = [inspect_artifact(item) for item in operation.expected]
        errors = [process.publication_error] if process.publication_error else []
        checks: list[ProcessResult] = []
        for index, artifact in enumerate(artifacts):
            if not artifact.valid:
                errors.append(f"{artifact.expected.path}: {artifact.error}")
            elif artifact.expected.kind == "audio":
                if self.audio is None:
                    errors.append("audio validation requires an explicit ffprobe executable")
                    artifacts[index] = replace(
                        artifact, valid=False, error="audio validation unavailable"
                    )
                else:
                    audio_errors, check = self.audio.validate(artifact.expected.path, runner)
                    errors.extend(audio_errors)
                    checks.append(check)
                    if audio_errors:
                        artifacts[index] = replace(
                            artifact, valid=False, error="; ".join(audio_errors)
                        )
        return self.complete(
            OperationResult(operation, process, tuple(artifacts), tuple(errors),
                            validation_processes=tuple(checks)),
            runner,
        )

    def complete(self, result: OperationResult, runner: ProcessRunner) -> OperationResult:
        return result


class MelismaAdapter(ToolAdapter):
    tool = "melisma"
    operations = ("ir", "abc", "ustx", "render-inputs")

    def build(
        self, mode: str, midi: Path, lyrics: Path, output: Path, *,
        singer: str | None = None,
        phonemizer: str | None = None,
        renderer: str | None = None,
        publish_stdout: bool = False,
    ) -> Operation:
        if mode not in self.operations:
            raise ValueError(f"unsupported Melisma operation: {mode}")
        midi = existing_input(midi, self.cwd)
        lyrics = existing_input(lyrics, self.cwd)
        inputs = (OperationInput("midi", midi, "midi"),
                  OperationInput("lyrics", lyrics, "lyrics"))
        metadata = (("--singer", singer), ("--phonemizer", phonemizer),
                    ("--renderer", renderer))
        if mode not in ("ustx", "render-inputs") and any(
            value is not None for _, value in metadata
        ):
            raise ValueError("USTX metadata is only supported for USTX and render-inputs")
        options: list[str] = []
        for flag, value in metadata:
            if value is not None:
                if not value or value.startswith("-"):
                    raise ValueError(f"invalid {flag} value")
                options.extend((flag, value))
        if mode == "render-inputs":
            output = absolute(output.parent, self.cwd) / output.name
            if output.is_symlink() or (
                output.exists() and (not output.is_dir() or any(output.iterdir()))
            ):
                raise ValueError("bundle directory must be absent or empty")
            if not output.parent.is_dir():
                raise ValueError("bundle parent must exist")
            expected = tuple(
                ExpectedArtifact(output / filename, kind)
                for filename, kind in (
                    ("bound.jsonl", "ir"), ("vocal.ustx", "ustx"),
                    ("accompaniment.abc", "abc"),
                )
            )
            arguments = ("render-inputs", *options, str(midi), str(lyrics), str(output))
        else:
            output = new_output(output, self.cwd)
            expected = (ExpectedArtifact(output, mode),)
            arguments = ("--format", mode, *options, str(midi), str(lyrics), str(output))
        if publish_stdout:
            if mode == "render-inputs":
                raise ValueError("stdout publication requires a single-file mode")
            return self._operation(
                mode, arguments[:-1], expected,
                metadata=(("stdout_artifact", str(output)),),
                inputs=inputs,
            )
        return self._operation(mode, arguments, expected, inputs=inputs)

    def run(self, operation: Operation, runner: ProcessRunner) -> ProcessResult:
        destination = dict(operation.metadata).get("stdout_artifact")
        if destination is not None:
            return runner.run(operation.command, stdout_artifact=Path(destination))
        return super().run(operation, runner)


class OpenUtauAdapter(ToolAdapter):
    tool = "openutau"
    operations = ("render",)

    def build(self, ustx: Path, output: Path) -> Operation:
        ustx = existing_input(ustx, self.cwd)
        output = new_output(output, self.cwd)
        if ustx.suffix.lower() != ".ustx" or output.suffix.lower() != ".wav":
            raise ValueError("OpenUtau requires .ustx input and .wav output")
        return self._operation(
            "render", ("render", str(ustx), "--out", str(output)),
            (ExpectedArtifact(output, "audio"),),
            inputs=(OperationInput("ustx", ustx, "ustx"),),
        )


class FFmpegAdapter(ToolAdapter):
    tool = "ffmpeg"
    operations = ("mix",)

    def build(
        self, instrumental: Path, vocal: Path, output: Path, *,
        instrumental_gain: float, vocal_gain: float, master_gain: float,
        duration: str, normalize: bool,
    ) -> Operation:
        gains = (instrumental_gain, vocal_gain, master_gain)
        if any(
            isinstance(gain, bool) or not isinstance(gain, (int, float))
            for gain in gains
        ):
            raise ValueError("gains must be positive finite numbers")
        try:
            numeric_gains = tuple(float(gain) for gain in gains)
        except OverflowError as error:
            raise ValueError("gain is outside the finite numerical range") from error
        if any(not math.isfinite(gain) or gain <= 0 for gain in numeric_gains):
            raise ValueError("gains must be positive finite numbers")
        if duration not in ("first", "shortest", "longest"):
            raise ValueError("duration must be first, shortest, or longest")
        if not isinstance(normalize, bool):
            raise ValueError("normalize must be an explicit boolean")
        instrumental = existing_input(instrumental, self.cwd)
        vocal = existing_input(vocal, self.cwd)
        output = new_output(output, self.cwd)
        if output.suffix.lower() != ".wav":
            raise ValueError("Phase 1 mix output must be .wav")
        graph = (
            f"[0:a]volume={numeric_gains[0]!r}[accompaniment];"
            f"[1:a]volume={numeric_gains[1]!r}[vocal];"
            f"[accompaniment][vocal]amix=inputs=2:duration={duration}:"
            f"normalize={int(normalize)}[mix];"
            f"[mix]volume={numeric_gains[2]!r}[out]"
        )
        return self._operation(
            "mix",
            ("-hide_banner", "-nostdin", "-n", "-i", str(instrumental),
             "-i", str(vocal), "-filter_complex", graph, "-map", "[out]",
             "-c:a", "pcm_s16le", str(output)),
            (ExpectedArtifact(output, "audio"),),
            metadata=(("duration", duration), ("normalize", str(normalize))),
            inputs=(OperationInput("instrumental", instrumental, "instrumental-audio"),
                    OperationInput("vocal", vocal, "vocal-audio")),
        )

    def complete(self, result: OperationResult, runner: ProcessRunner) -> OperationResult:
        if not result.success:
            return result
        output = result.operation.expected[0].path
        check = runner.run(Command(
            self.executable,
            ("-hide_banner", "-nostdin", "-nostats", "-i", str(output),
             "-af", "volumedetect", "-f", "null", "-"),
            self.cwd,
        ))
        errors = list(result.errors)
        analysis = None
        if not check.success:
            errors.append(f"post-mix peak analysis failed: {check.error or check.stderr}")
        else:
            try:
                analysis = parse_volumedetect(check.stderr)
                validate_final_mix_analysis(analysis)
            except PipelineError as error:
                errors.append(str(error))
        return replace(
            result, errors=tuple(errors), completion=analysis,
            validation_processes=(*result.validation_processes, check),
        )
