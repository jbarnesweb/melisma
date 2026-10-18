"""Bounded current-session execution using the existing adapter/result model."""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import fields, replace
from pathlib import Path

from .adapters import ToolAdapter
from .artifact_catalog import ArtifactCatalog, CatalogError, input_document
from .process import (
    LIVE_STATES, OUTPUT_LIMIT, Command, Operation, OperationResult, OwnedChild,
    ProcessResult, ProcessRunner, ProcessState, console_result, utc_now,
)


def command_document(command: Command) -> dict[str, object]:
    return {
        "executable": str(command.executable), "arguments": list(command.arguments),
        "cwd": str(command.cwd), "display": command.display,
    }


def operation_document(operation: Operation) -> dict[str, object]:
    return {
        "tool": operation.tool, "operation": operation.name,
        "command": command_document(operation.command),
        "expected": [
            {"path": str(item.path), "kind": item.kind, "allow_empty": item.allow_empty}
            for item in operation.expected
        ],
        "metadata": dict(operation.metadata),
        "inputs": [input_document(item) for item in operation.inputs],
    }


def process_document(result: ProcessResult) -> dict[str, object]:
    result = console_result(result)
    return {
        **{field.name: getattr(result, field.name)
           for field in fields(result) if field.name != "command"},
        "command": command_document(result.command), "signal": result.signal,
        "success": result.success,
    }


class Execution(ProcessRunner):
    """Adapter runner tracking a primary command and any validation subprocesses."""

    def __init__(
        self, operation: Operation, *, output_limit: int,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.operation = operation
        self.output_limit = output_limit
        self.lock = threading.RLock()
        self.created = utc_now()
        self.began = time.monotonic()
        self.process = ProcessResult(
            operation.command, ProcessState.CREATED, None, submitted_at=self.created,
        )
        self.child: OwnedChild | None = None
        self.primary = False
        self.stop_requested = False
        self.result: OperationResult | None = None
        self.completed_at: str | None = None
        self.duration: float | None = None
        self.thread: threading.Thread | None = None
        self.validation: list[ProcessResult] = []
        self.catalog = catalog
        self.catalog_error: str | None = None

    @property
    def is_complete(self) -> bool:
        with self.lock:
            return self.result is not None

    def run(
        self, command: Command, *, stdout_artifact: Path | None = None,
    ) -> ProcessResult:
        with self.lock:
            primary = command is self.operation.command
            if self.stop_requested:
                return ProcessResult(
                    command, ProcessState.CANCELLED, None,
                    error="Cancelled before process start", stop_requested=True,
                    submitted_at=self.created, completed_at=utc_now(),
                )
            starting = ProcessResult(
                command, ProcessState.STARTING, None, submitted_at=self.created,
            )
            if primary:
                self.process = starting
            try:
                child = OwnedChild(
                    command, output_limit=self.output_limit, capture_complete=True,
                )
            except OSError as error:
                result = replace(
                    starting, state=ProcessState.START_FAILED, error=str(error),
                    completed_at=utc_now(), duration=time.monotonic() - self.began,
                )
                if primary:
                    self.process = result
                else:
                    self.validation.append(result)
                return result
            self.child, self.primary = child, primary
        with child:
            bounded = child.wait()
            with self.lock:
                if primary:
                    self.process = replace(bounded, submitted_at=self.created)
                else:
                    self.validation.append(bounded)
            result = child.complete_result(stdout_artifact=stdout_artifact)
        with self.lock:
            bounded = console_result(result, self.output_limit)
            if primary:
                self.process = replace(bounded, submitted_at=self.created)
            else:
                self.validation[-1] = bounded
            self.child = None
        return result

    def execute(self, adapter: ToolAdapter) -> None:
        with self.lock:
            self.process = replace(self.process, state=ProcessState.STARTING)
        try:
            result = adapter.execute(self.operation, self)
        except (OSError, ValueError, RuntimeError) as error:
            # Keep the primary diagnostics if artifact/validation finalization fails.
            result = OperationResult(
                self.operation, self.process, (),
                (f"Operation finalization failed: {error}",),
                validation_processes=tuple(self.validation),
            )
        finally:
            with self.lock:
                child = self.child
            if child is not None:
                child.stop()
        with self.lock:
            self.process = replace(
                console_result(result.process, self.output_limit), submitted_at=self.created,
                completed_at=result.process.completed_at or utc_now(),
                duration=max(result.process.duration, 0),
            )
            if self.process.state in LIVE_STATES:
                self.process = replace(
                    self.process, state=ProcessState.REJECTED,
                    error=self.process.error or "Operation failed before process creation",
                )
            self.result = replace(
                result, process=self.process,
                validation_processes=tuple(
                    console_result(item, self.output_limit) for item in result.validation_processes
                ),
            )
            self.completed_at = utc_now()
            self.duration = time.monotonic() - self.began
            if self.catalog is not None:
                try:
                    self.catalog.register_result(self.result, self.id)
                    self.catalog.register_diagnostics(self.id, self.snapshot())
                except (CatalogError, OSError, ValueError) as error:
                    self.catalog_error = f"Artifact registration failed: {error}"
                finally:
                    self.catalog.end(self.operation)

    def stop(self, timeout: float = 1.0) -> dict[str, object]:
        with self.lock:
            if self.result is not None or (
                self.child is None and self.process.state not in LIVE_STATES
            ):
                return self.snapshot()
            child = self.child
            if child is not None and child.poll() is not None:
                return self.snapshot()
            self.stop_requested = True
        if child is not None:
            child.stop(timeout)
        return self.snapshot()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            process = self.process
            active = self.child.snapshot() if self.child is not None else None
            if active is not None and self.primary:
                process = replace(active, submitted_at=self.created)
            result = self.result
            stoppable = (active or process).state in LIVE_STATES
            return {
                "id": self.id, **operation_document(self.operation),
                "process": process_document(process),
                "complete": result is not None,
                "phase": "complete" if result is not None else
                "validating" if process.state not in LIVE_STATES else process.state.value,
                "stop_requested": self.stop_requested or process.stop_requested,
                "can_stop": result is None and not self.stop_requested and stoppable,
                "submitted_at": self.created, "completed_at": self.completed_at,
                "duration": self.duration if self.duration is not None else time.monotonic() - self.began,
                "success": result.success and not self.stop_requested if result else False,
                "catalog_error": self.catalog_error,
                "stale_inputs": self.catalog.changed_inputs(self.id) if self.catalog else [],
                "artifacts": [] if result is None else [
                    {
                        "path": str(item.expected.path), "kind": item.expected.kind,
                        "exists": item.exists, "size": item.size,
                        "valid": item.valid, "error": item.error,
                    } for item in result.artifacts
                ],
                "errors": list(result.errors) if result else [],
                "completion": result.completion if result else None,
                "validation_processes": [
                    process_document(item) for item in (
                        result.validation_processes if result else self.validation
                    )
                ],
                "active_validation": process_document(active)
                if active is not None and not self.primary else None,
            }


class RegistryFull(ValueError):
    pass


class RunRegistry:
    """Oldest completed runs evicted first; active runs are never evicted."""

    def __init__(
        self, *, max_runs: int = 32, max_active: int = 4,
        output_limit: int = OUTPUT_LIMIT,
        catalog: ArtifactCatalog | None = None,
    ) -> None:
        if not 1 <= max_active <= max_runs <= 32 or not 0 < output_limit <= OUTPUT_LIMIT:
            raise ValueError("invalid session retention bounds")
        self.max_runs, self.max_active, self.output_limit = max_runs, max_active, output_limit
        self.lock = threading.RLock()
        self.runs: OrderedDict[str, Execution] = OrderedDict()
        self.closed = False
        self.catalog = catalog

    def start(self, adapter: ToolAdapter, operation: Operation) -> dict[str, object]:
        with self.lock:
            if self.closed:
                raise RegistryFull("Session is shutting down")
            active = [run for run in self.runs.values() if not run.is_complete]
            if len(active) >= self.max_active:
                raise RegistryFull("Active run limit reached")
            paths = {item.path for run in active for item in run.operation.expected}
            if any(item.path in paths for item in operation.expected):
                raise RegistryFull("An active run already owns this output destination")
            if any(item.path in paths for item in operation.inputs):
                raise RegistryFull("An active run owns an input destination")
            if self.catalog is not None:
                operation = self.catalog.begin(operation)
            if len(self.runs) >= self.max_runs:
                oldest = next(key for key, run in self.runs.items() if run.is_complete)
                del self.runs[oldest]
            run = Execution(operation, output_limit=self.output_limit, catalog=self.catalog)
            self.runs[run.id] = run
            run.thread = threading.Thread(target=run.execute, args=(adapter,), daemon=True)
            snapshot = run.snapshot()
            run.thread.start()
            return snapshot

    def get(self, run_id: str) -> Execution:
        with self.lock:
            return self.runs[run_id]

    def list(self) -> list[dict[str, object]]:
        with self.lock:
            return [run.snapshot() for run in self.runs.values()]

    def close(self) -> None:
        with self.lock:
            self.closed = True
            runs = tuple(self.runs.values())
        for run in runs:
            run.stop()
        for run in runs:
            if run.thread is not None:
                run.thread.join()
