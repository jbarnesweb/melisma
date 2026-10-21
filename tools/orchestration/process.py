"""Structured execution records and the lifecycle of an exact owned child."""

from __future__ import annotations

import math
import os
import select
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from codecs import getincrementaldecoder
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO

from .provenance import ProductionConfiguration


class ProcessState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    SIGNALLED = "signalled"
    START_FAILED = "start-failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


LIVE_STATES = (ProcessState.CREATED, ProcessState.STARTING, ProcessState.RUNNING)
OUTPUT_LIMIT = 1024 * 1024
CAPTURE_GRACE = 0.5


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    submitted_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_complete: bool = True
    stderr_complete: bool = True
    publication_error: str | None = None

    @property
    def success(self) -> bool:
        return (
            self.state == ProcessState.EXITED
            and self.exit_status == 0
            and not self.stop_requested
            and self.error is None
            and self.stdout_complete
            and self.stderr_complete
        )

    @property
    def signal(self) -> int | None:
        if self.exit_status is not None and self.exit_status < 0:
            return -self.exit_status
        return None


@dataclass(frozen=True)
class OperationInput:
    role: str
    path: Path
    type: str = "file"
    artifact_id: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class Operation:
    tool: str
    name: str
    command: Command
    expected: tuple[ExpectedArtifact, ...]
    metadata: tuple[tuple[str, str], ...] = ()
    inputs: tuple[OperationInput, ...] = ()
    production: ProductionConfiguration | None = None

    @property
    def stage(self) -> str:
        """The execution-stage identity already used by artifact history."""
        return f"{self.tool}.{self.name}"


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
    """Complete machine output; console retention is a separate presentation."""

    def run(
        self, command: Command, *, stdout_artifact: Path | None = None,
    ) -> ProcessResult:
        submitted = utc_now()
        began = time.monotonic()
        try:
            child = OwnedChild(command, capture_complete=True)
        except OSError as error:
            return ProcessResult(
                command, ProcessState.START_FAILED, None, error=str(error),
                submitted_at=submitted, completed_at=utc_now(),
                duration=time.monotonic() - began,
            )
        with child:
            child.wait()
            return child.complete_result(stdout_artifact=stdout_artifact)


def console_result(result: ProcessResult, limit: int = OUTPUT_LIMIT) -> ProcessResult:
    """Detach bounded display text from a complete machine-consumed result."""
    changes: dict[str, str | bool] = {}
    for name in ("stdout", "stderr"):
        encoded = getattr(result, name).encode("utf-8")
        decoder = getincrementaldecoder("utf-8")(errors="replace")
        changes[name] = decoder.decode(encoded[:limit], final=len(encoded) <= limit)
        changes[f"{name}_truncated"] = (
            getattr(result, f"{name}_truncated") or len(encoded) > limit
        )
    return replace(result, **changes)


class CapturedStream:
    """Drain into a bounded prefix and optional complete binary spool.

    After child exit is observed, await EOF for CAPTURE_GRACE, including idle
    intervals. Expiry closes the reader and reports incomplete capture.
    """

    def __init__(
        self, stream: BinaryIO, limit: int, *, spool: BinaryIO | None = None,
    ) -> None:
        self.stream = stream
        self.spool = spool
        self.limit = limit
        self.data = bytearray()
        self.total = 0
        self.error: str | None = None
        self.complete = False
        self.lock = threading.Lock()
        self.deadline: float | None = None
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def finish(self) -> None:
        with self.lock:
            if self.deadline is None:
                self.deadline = time.monotonic() + CAPTURE_GRACE

    def _drain(self) -> None:
        try:
            while True:
                with self.lock:
                    deadline = self.deadline
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    with self.lock:
                        self.error = "EOF not received within 0.5 seconds after child exit; capture incomplete"
                    break
                timeout = 0.1 if remaining is None else min(0.1, remaining)
                ready, _, _ = select.select([self.stream], [], [], timeout)
                if not ready:
                    continue
                chunk = os.read(self.stream.fileno(), 8192)
                if not chunk:
                    with self.lock:
                        self.complete = True
                    break
                with self.lock:
                    self.total += len(chunk)
                    self.data.extend(chunk[:max(0, self.limit - len(self.data))])
                if self.spool is not None:
                    self.spool.write(chunk)
        except OSError as error:
            with self.lock:
                self.error = f"output capture failed: {error}"
        finally:
            self.stream.close()

    def snapshot(self) -> tuple[str, bool, int, str | None]:
        with self.lock:
            # Do not render half a multibyte character during incremental reads.
            decoder = getincrementaldecoder("utf-8")(errors="replace")
            text = decoder.decode(bytes(self.data), final=not self.thread.is_alive())
            return text, self.total > self.limit, self.total, self.error

    def read_complete(self) -> str:
        if self.spool is None or self.thread.is_alive():
            raise ValueError("complete output requires a finalized capture spool")
        self.spool.seek(0)
        return self.spool.read().decode("utf-8", errors="replace")

    @property
    def eof_received(self) -> bool:
        with self.lock:
            return self.complete


class OwnedChild:
    """One exact child, independently drained pipes and serialized termination."""

    def __init__(
        self, command: Command, *, output_limit: int = OUTPUT_LIMIT,
        capture_complete: bool = False,
    ) -> None:
        if not 0 < output_limit <= OUTPUT_LIMIT:
            raise ValueError("output limit must be in (0, 1 MiB]")
        self.command = command
        self._lock = threading.RLock()
        self._result: ProcessResult | None = None
        self._stop_requested = False
        self._escalated = False
        self.submitted_at = utc_now()
        self._began = time.monotonic()
        self._spools = ExitStack()
        try:
            out_spool = self._spools.enter_context(tempfile.TemporaryFile()) if capture_complete else None
            err_spool = self._spools.enter_context(tempfile.TemporaryFile()) if capture_complete else None
            self._child = subprocess.Popen(
                command.argv,
                cwd=command.cwd,
                env=command.child_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (OSError, ValueError):
            self._spools.close()
            raise
        self.started_at = utc_now()
        assert self._child.stdout is not None and self._child.stderr is not None
        self._stdout = CapturedStream(self._child.stdout, output_limit, spool=out_spool)
        self._stderr = CapturedStream(self._child.stderr, output_limit, spool=err_spool)
        self.observed_live = self._child.poll() is None

    @property
    def pid(self) -> int:
        return self._child.pid

    def poll(self) -> ProcessResult | None:
        with self._lock:
            if self._result is not None:
                return self._result
            returncode = self._child.poll()
            if returncode is None:
                self.observed_live = True
                return None
            # Drain all bytes written before exit before publishing a terminal record.
            self._stdout.finish()
            self._stderr.finish()
            self._stdout.thread.join()
            self._stderr.thread.join()
            self._result = self._snapshot(
                ProcessState.SIGNALLED if returncode < 0 else ProcessState.EXITED,
                returncode, utc_now(),
            )
            return self._result

    def _snapshot(
        self, state: ProcessState, returncode: int | None = None,
        completed: str | None = None,
    ) -> ProcessResult:
        stdout, out_cut, out_bytes, out_error = self._stdout.snapshot()
        stderr, err_cut, err_bytes, err_error = self._stderr.snapshot()
        return ProcessResult(
            self.command, state, returncode, stdout, stderr, self.pid,
            error="; ".join(
                f"{name}: {error}" for name, error in
                (("stdout", out_error), ("stderr", err_error)) if error is not None
            ) or None,
            stop_requested=self._stop_requested, escalated=self._escalated,
            submitted_at=self.submitted_at, started_at=self.started_at,
            completed_at=completed, duration=time.monotonic() - self._began,
            stdout_truncated=out_cut, stderr_truncated=err_cut,
            stdout_bytes=out_bytes, stderr_bytes=err_bytes,
            stdout_complete=self._stdout.eof_received, stderr_complete=self._stderr.eof_received,
        )

    def complete_result(self, *, stdout_artifact: Path | None = None) -> ProcessResult:
        result = self.poll()
        if result is None:
            raise ValueError("complete output requires child completion")
        publication_error = None
        if stdout_artifact is not None and result.success:
            if not stdout_artifact.is_absolute():
                raise ValueError("stdout artifact destination must be absolute")
            try:
                spool = self._stdout.spool
                if spool is None:
                    raise ValueError("stdout publication requires complete raw capture")
                spool.seek(0)
                with stdout_artifact.open("xb") as destination:
                    shutil.copyfileobj(spool, destination)
            except OSError as error:
                publication_error = f"stdout artifact publication failed: {error}"
        try:
            stdout = self._stdout.read_complete()
            stderr = self._stderr.read_complete()
        except OSError as error:
            diagnostic = f"complete output read failed: {error}"
            return replace(
                result, error=f"{result.error}; {diagnostic}" if result.error else diagnostic,
                publication_error=publication_error,
            )
        return replace(
            result, stdout=stdout, stderr=stderr,
            stdout_truncated=False, stderr_truncated=False,
            publication_error=publication_error,
        )

    def snapshot(self) -> ProcessResult:
        with self._lock:
            return self.poll() or self._snapshot(ProcessState.RUNNING)

    def wait(self, timeout: float | None = None) -> ProcessResult:
        self._child.wait(timeout=timeout)
        result = self.poll()
        assert result is not None
        return result

    def stop(self, timeout: float = 5.0) -> ProcessResult:
        if not math.isfinite(timeout) or not 0 < timeout <= 5:
            raise ValueError("stop timeout must be finite and in (0, 5] seconds")
        with self._lock:
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
        try:
            self.stop()
        finally:
            self._spools.close()
