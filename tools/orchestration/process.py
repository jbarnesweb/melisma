"""Structured execution records and the lifecycle of an exact owned child."""

from __future__ import annotations

import math
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO


class ProcessState(str, Enum):
    EXITED = "exited"
    SIGNALLED = "signalled"
    START_FAILED = "start-failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Command:
    executable: Path
    arguments: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str | None], ...] = ()

    def __post_init__(self) -> None:
        if not self.executable.is_absolute() or not self.cwd.is_absolute():
            raise ValueError("executable and cwd must be absolute")
        if not isinstance(self.arguments, tuple) or any(
            not isinstance(value, str) or "\0" in value for value in self.arguments
        ):
            raise ValueError("arguments must be a tuple of NUL-free strings")
        if len(dict(self.environment)) != len(self.environment):
            raise ValueError("duplicate environment overrides")

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)

    @property
    def display(self) -> str:
        """Display only: this string is never executed."""
        return shlex.join(self.argv)

    def child_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key, value in self.environment:
            if not key or "=" in key or "\0" in key or (
                value is not None and "\0" in value
            ):
                raise ValueError("invalid environment override")
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return environment


@dataclass(frozen=True)
class ExpectedArtifact:
    path: Path
    kind: str = "file"
    allow_empty: bool = False


@dataclass(frozen=True)
class ArtifactRecord:
    expected: ExpectedArtifact
    exists: bool
    size: int | None
    valid: bool
    error: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    command: Command
    state: ProcessState
    exit_status: int | None
    stdout: str = ""
    stderr: str = ""
    pid: int | None = None
    error: str | None = None
    stop_requested: bool = False
    escalated: bool = False

    @property
    def success(self) -> bool:
        return (
            self.state == ProcessState.EXITED
            and self.exit_status == 0
            and not self.stop_requested
        )

    @property
    def signal(self) -> int | None:
        if self.exit_status is not None and self.exit_status < 0:
            return -self.exit_status
        return None


@dataclass(frozen=True)
class Operation:
    tool: str
    name: str
    command: Command
    expected: tuple[ExpectedArtifact, ...]
    metadata: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OperationResult:
    operation: Operation
    process: ProcessResult
    artifacts: tuple[ArtifactRecord, ...]
    errors: tuple[str, ...] = ()
    completion: dict[str, object] | None = None
    validation_processes: tuple[ProcessResult, ...] = ()

    @property
    def success(self) -> bool:
        return (
            self.process.success
            and all(artifact.valid for artifact in self.artifacts)
            and not self.errors
        )


def finished(
    command: Command,
    pid: int,
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    stop_requested: bool = False,
    escalated: bool = False,
) -> ProcessResult:
    return ProcessResult(
        command,
        ProcessState.SIGNALLED if returncode < 0 else ProcessState.EXITED,
        returncode,
        stdout,
        stderr,
        pid,
        stop_requested=stop_requested,
        escalated=escalated,
    )


class ProcessRunner:
    """Internal execution primitive, not an API accepting user command strings."""

    def run(self, command: Command) -> ProcessResult:
        try:
            child = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=command.child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except OSError as error:
            return ProcessResult(
                command, ProcessState.START_FAILED, None, error=str(error)
            )
        with child:
            stdout, stderr = child.communicate()
            return finished(
                command,
                child.pid,
                child.returncode,
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )


class OwnedChild:
    """Playback-only child ownership; bounded stop never targets a process name."""

    def __init__(self, command: Command) -> None:
        self.command = command
        self._stdout: BinaryIO = tempfile.TemporaryFile()
        self._stderr: BinaryIO = tempfile.TemporaryFile()
        self._result: ProcessResult | None = None
        self._stop_requested = False
        self._escalated = False
        try:
            self._child = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=command.child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=self._stdout,
                stderr=self._stderr,
                shell=False,
            )
        except (OSError, ValueError):
            self._stdout.close()
            self._stderr.close()
            raise
        self.observed_live = self._child.poll() is None

    @property
    def pid(self) -> int:
        return self._child.pid

    def poll(self) -> ProcessResult | None:
        if self._result is not None:
            return self._result
        returncode = self._child.poll()
        if returncode is None:
            self.observed_live = True
            return None
        self._stdout.seek(0)
        self._stderr.seek(0)
        self._result = finished(
            self.command,
            self.pid,
            returncode,
            self._stdout.read().decode("utf-8", errors="replace"),
            self._stderr.read().decode("utf-8", errors="replace"),
            stop_requested=self._stop_requested,
            escalated=self._escalated,
        )
        self._stdout.close()
        self._stderr.close()
        return self._result

    def wait(self, timeout: float | None = None) -> ProcessResult:
        self._child.wait(timeout=timeout)
        result = self.poll()
        assert result is not None
        return result

    def stop(self, timeout: float = 5.0) -> ProcessResult:
        if not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("stop timeout must be finite and in (0, 5] seconds")
        completed = self.poll()
        if completed is not None:
            return completed
        self._stop_requested = True
        self._child.terminate()
        try:
            return self.wait(timeout)
        except subprocess.TimeoutExpired:
            self._escalated = True
            self._child.kill()
            return self.wait(timeout)

    def __enter__(self) -> OwnedChild:
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
