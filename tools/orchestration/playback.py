"""Validated headless playback with owned-child EOF/error/stop interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .adapters import ToolAdapter
from .artifacts import existing_input
from .process import (
    Operation, OperationInput, OperationResult, OwnedChild, ProcessResult, ProcessRunner, ProcessState,
)


class PlaybackState(str, Enum):
    RUNNING = "running"
    EOF = "eof"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass(frozen=True)
class PlaybackResult:
    operation: Operation
    state: PlaybackState
    process: ProcessResult
    validation_process: ProcessResult

    @property
    def success(self) -> bool:
        return self.state == PlaybackState.EOF


def playback_state(result: ProcessResult, observed_live: bool) -> PlaybackState:
    if result.stop_requested:
        return PlaybackState.STOPPED
    if result.success and observed_live and not result.stderr.strip():
        return PlaybackState.EOF
    return PlaybackState.ERROR


class Playback:
    def __init__(
        self, operation: Operation, child: OwnedChild, validation: ProcessResult
    ) -> None:
        self.operation = operation
        self.child = child
        self.validation = validation

    @property
    def pid(self) -> int:
        return self.child.pid

    @property
    def state(self) -> PlaybackState:
        result = self.poll()
        return PlaybackState.RUNNING if result is None else result.state

    def result(self, process: ProcessResult) -> PlaybackResult:
        return PlaybackResult(
            self.operation, playback_state(process, self.child.observed_live),
            process, self.validation,
        )

    def poll(self) -> PlaybackResult | None:
        process = self.child.poll()
        return None if process is None else self.result(process)

    def wait(self, timeout: float | None = None) -> PlaybackResult:
        return self.result(self.child.wait(timeout))

    def stop(self, timeout: float = 5) -> PlaybackResult:
        return self.result(self.child.stop(timeout))

    def __enter__(self) -> Playback:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class FFplayAdapter(ToolAdapter):
    tool = "ffplay"
    operations = ("play",)

    def build(self, input_path: Path) -> Operation:
        input_path = existing_input(input_path, self.cwd)
        return self._operation(
            "play",
            ("-hide_banner", "-loglevel", "error", "-nodisp", "-autoexit",
             "-vn", "-i", str(input_path)),
            (),
            metadata=(("input", str(input_path)), ("stop_policy", "owned-child")),
            inputs=(OperationInput("audio", input_path, "audio"),),
        )

    def execute(self, operation: Operation, runner: ProcessRunner) -> OperationResult:
        try:
            with self.start(operation, runner) as playback:
                result = playback.wait()
        except PlaybackValidationError as error:
            process = ProcessResult(
                operation.command, ProcessState.REJECTED, None, error=str(error)
            )
            return OperationResult(
                operation, process, (), (str(error),),
                validation_processes=(error.validation,),
            )
        except PlaybackStartError as error:
            return OperationResult(
                operation, error.process, (), (str(error),),
                validation_processes=(error.validation,),
            )
        errors = () if result.success else ("playback did not reach natural EOF",)
        return OperationResult(
            operation, result.process, (), errors,
            completion={"playback_state": result.state.value},
            validation_processes=(result.validation_process,),
        )

    def start(self, operation: Operation, runner: ProcessRunner) -> Playback:
        if operation.tool != self.tool or operation.name != "play":
            raise ValueError("operation belongs to a different adapter")
        existing_input(Path(dict(operation.metadata)["input"]), self.cwd)
        if self.audio is None:
            raise ValueError("playback requires an explicit ffprobe executable")
        errors, validation = self.audio.validate(
            Path(dict(operation.metadata)["input"]), runner
        )
        if errors:
            # Retain the failed check instead of launching ffplay and trusting exit 0.
            raise PlaybackValidationError(errors, validation)
        try:
            child = OwnedChild(operation.command)
        except OSError as error:
            process = ProcessResult(
                operation.command, ProcessState.START_FAILED, None, error=str(error)
            )
            raise PlaybackStartError(process, validation) from error
        return Playback(operation, child, validation)


class PlaybackValidationError(ValueError):
    def __init__(self, errors: tuple[str, ...], validation: ProcessResult) -> None:
        super().__init__("; ".join(errors))
        self.validation = validation


class PlaybackStartError(RuntimeError):
    def __init__(self, process: ProcessResult, validation: ProcessResult) -> None:
        super().__init__(process.error)
        self.process = process
        self.validation = validation
