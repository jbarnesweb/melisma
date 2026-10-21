"""Explicit single-operation reruns, not discovery, repair or execution planning."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum

from ..orchestration.adapters import ToolAdapter
from ..orchestration.artifact_catalog import ArtifactCatalog, semantic_output_type
from ..orchestration.execution import RunRegistry, StageState
from ..orchestration.process import Operation, ProcessState
from ..orchestration.yue2 import has_manifest_completion
from .currentness import ArtifactAssessment, Currentness, supports_production
from .readiness import CandidateReadiness, ReadinessAssessment
from .workflows import WorkflowDefinition, workflow_definitions


class RerunDecision(str, Enum):
    RERUN = "rerun"
    NOT_RERUNNABLE = "not-rerunnable"
    UNKNOWN = "unknown"


class ReasonCode(str, Enum):
    FAILED_ATTEMPT_SELECTED = "failed_attempt_selected"
    STOPPED_ATTEMPT_SELECTED = "stopped_attempt_selected"
    CANCELLED_ATTEMPT_SELECTED = "cancelled_attempt_selected"
    STALE_PRODUCTION_SELECTED = "stale_production_selected"
    READINESS_BLOCKED = "readiness_blocked"
    READINESS_UNKNOWN = "readiness_unknown"
    READINESS_MISMATCH = "readiness_mismatch"
    TARGET_NOT_RERUNNABLE = "target_not_rerunnable"
    TARGET_AMBIGUOUS = "target_ambiguous"
    TARGET_NOT_GENERATED = "target_not_generated"
    ACTIVE_EXECUTION_CONFLICT = "active_execution_conflict"
    EVIDENCE_MISSING = "evidence_missing"


@dataclass(frozen=True)
class RerunReason:
    code: ReasonCode
    run_id: str | None = None
    artifact_id: str | None = None


@dataclass(frozen=True)
class RerunResult:
    """Public response: no Operation, Execution, diagnostics or caller payloads."""

    decision: RerunDecision
    reasons: tuple[RerunReason, ...]
    run_id: str | None = None


@dataclass(frozen=True)
class BoundReadiness:
    candidate: Operation
    assessment: ReadinessAssessment


@dataclass(frozen=True)
class RerunTrigger:
    kind: str
    workflow: str
    stage: str
    run_id: str | None = None
    artifact_id: str | None = None
    artifact_type: str | None = None
    currentness: ArtifactAssessment | None = None


@dataclass(frozen=True)
class RerunRequest:
    """Trusted library evidence, not an untrusted browser request.

    Readiness must have been assessed for the bound exact candidate. Stale
    currentness must come from assess_currentness with this request's candidate
    in its ProductionSelection. Like Step 5, this boundary checks observable
    contradictions, not authentication of caller-constructed evidence.
    """

    workflow: str
    candidate: Operation | None
    triggers: tuple[RerunTrigger, ...] = ()
    readiness: tuple[BoundReadiness, ...] = ()


def _identifier(value: str | None) -> str | None:
    return value if value is not None and re.fullmatch(r"[0-9a-f]{32}", value) else None


def _result(decision: RerunDecision, *reasons: RerunReason) -> RerunResult:
    return RerunResult(decision, tuple(sorted(set(reasons), key=lambda item: (
        item.code.value, item.run_id or "", item.artifact_id or "",
    ))))


def _reason(code: ReasonCode, trigger: RerunTrigger) -> RerunReason:
    return RerunReason(code, _identifier(trigger.run_id), _identifier(trigger.artifact_id))


def _attempt(
    trigger: RerunTrigger, candidate: Operation, registry: RunRegistry,
) -> RerunResult:
    with registry.lock:
        run = registry.runs.get(trigger.run_id)
        if run is None:
            return _result(RerunDecision.UNKNOWN, _reason(ReasonCode.EVIDENCE_MISSING, trigger))
        with run.lock:
            previous, result, state = run.operation, run.result, run.stage_state
    if previous.stage != candidate.stage:
        return _result(
            RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.TARGET_NOT_RERUNNABLE, trigger),
        )
    if result is None:
        return _result(
            RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.ACTIVE_EXECUTION_CONFLICT, trigger),
        )
    cancelled = result.process.state == ProcessState.CANCELLED or (
        candidate.stage == "ffplay.play"
        and any(item.state == ProcessState.CANCELLED for item in result.validation_processes)
    )
    actual = (
        "cancelled" if state == StageState.STOPPED and cancelled
        else "stopped" if state == StageState.STOPPED
        else "failed" if state == StageState.FAILED else None
    )
    if actual != trigger.kind:
        return _result(
            RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.TARGET_NOT_RERUNNABLE, trigger),
        )
    selected = _reason({
        "failed": ReasonCode.FAILED_ATTEMPT_SELECTED,
        "stopped": ReasonCode.STOPPED_ATTEMPT_SELECTED,
        "cancelled": ReasonCode.CANCELLED_ATTEMPT_SELECTED,
    }[actual], trigger)
    return _result(RerunDecision.RERUN, selected)


def _stale(
    trigger: RerunTrigger, candidate: Operation, definition: WorkflowDefinition,
    catalog: ArtifactCatalog,
) -> RerunResult:
    missing = _result(RerunDecision.UNKNOWN, _reason(ReasonCode.EVIDENCE_MISSING, trigger))
    invalid = _result(
        RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.TARGET_NOT_RERUNNABLE, trigger),
    )
    with catalog.lock:
        entry = catalog.entries.get(trigger.artifact_id)
        run = catalog.runs.get(trigger.run_id)
    if entry is None:
        return missing
    if entry.producer_run is None and entry.origin in ("imported", "edited"):
        return _result(
            RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.TARGET_NOT_GENERATED, trigger),
        )
    assessment = trigger.currentness
    if trigger.run_id is None or assessment is None:
        return missing
    if (
        assessment.workflow != trigger.workflow or assessment.stage != trigger.stage
        or assessment.artifact_id != trigger.artifact_id
        or assessment.artifact_type != trigger.artifact_type
        or assessment.producer_run != trigger.run_id
    ):
        return invalid
    productions = tuple(
        item.provenance for item in run.productions if item.artifact_id == entry.id
    ) if run is not None and run.success else ()
    if (
        not productions and entry.producer_run == trigger.run_id
        and entry.provenance is not None and (run is None or run.success)
    ):
        productions = (entry.provenance,)
    if len(productions) != 1:
        return invalid if run is not None else missing
    recorded = productions[0]
    if (
        recorded.stage != candidate.stage or recorded.output_type != trigger.artifact_type
        or entry.type != trigger.artifact_type
        or not supports_production(definition, recorded.stage, recorded.output_type, entry.path)
        or not (
            recorded.output_type in {
                semantic_output_type(candidate.stage, item) for item in candidate.expected
            } or has_manifest_completion(candidate.stage)
        )
    ):
        return invalid
    if assessment.currentness == Currentness.UNKNOWN:
        return missing
    if assessment.currentness != Currentness.STALE:
        return invalid
    if not any(item.code in (
        "context_changed", "setting_changed", "input_changed", "upstream_stale",
    ) for item in assessment.reasons):
        return missing
    return _result(
        RerunDecision.RERUN, _reason(ReasonCode.STALE_PRODUCTION_SELECTED, trigger),
    )


def assess_rerun(
    request: RerunRequest, *, catalog: ArtifactCatalog, registry: RunRegistry,
) -> RerunResult:
    """Read-only, supplied-evidence assessment; never admission or a reservation.

    Known prevention dominates incomplete evidence. Failed/stopped/cancelled
    attempts qualify the workflow and exact operation, not the replacement's
    production context. Step 5 readiness must bind to that exact replacement.
    Stale evidence still identifies its exact historical production.
    """
    candidate = request.candidate
    definition = next((
        item for item in workflow_definitions() if item.key == request.workflow
    ), None)
    if definition is None:
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.TARGET_NOT_RERUNNABLE),
        )
    if candidate is None:
        return _result(RerunDecision.UNKNOWN, RerunReason(ReasonCode.EVIDENCE_MISSING))
    playback = request.workflow == "play-audio" and candidate.stage == "ffplay.play" and not candidate.expected
    if not (playback or definition.owns_production(candidate.stage) and candidate.expected):
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.TARGET_NOT_RERUNNABLE),
        )
    if any(
        item.candidate != candidate or item.assessment.workflow != request.workflow
        or item.assessment.stage != candidate.stage for item in request.readiness
    ):
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.READINESS_MISMATCH),
        )
    states = {item.assessment.readiness for item in request.readiness}
    if CandidateReadiness.BLOCKED in states:
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.READINESS_BLOCKED),
        )
    # Trigger contradictions still prevent a request whose readiness is unknown.
    results = []
    if states != {CandidateReadiness.READY}:
        results.append(_result(
            RerunDecision.UNKNOWN, RerunReason(ReasonCode.READINESS_UNKNOWN),
        ))
    triggers = set(request.triggers)
    attempts = {item.run_id for item in triggers if item.kind in ("failed", "stopped", "cancelled")}
    producers = {item.run_id for item in triggers if item.kind == "stale"}
    if len(attempts) > 1 or len(producers) > 1:
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.TARGET_AMBIGUOUS),
        )
    if not triggers:
        results.append(_result(RerunDecision.UNKNOWN, RerunReason(ReasonCode.EVIDENCE_MISSING)))
    for trigger in triggers:
        if trigger.workflow != request.workflow or trigger.stage != candidate.stage:
            results.append(_result(
                RerunDecision.NOT_RERUNNABLE, _reason(ReasonCode.TARGET_NOT_RERUNNABLE, trigger),
            ))
        elif trigger.kind in ("failed", "stopped", "cancelled"):
            results.append(_attempt(trigger, candidate, registry))
        elif trigger.kind == "stale":
            results.append(_stale(trigger, candidate, definition, catalog))
        else:
            results.append(_result(
                RerunDecision.UNKNOWN, _reason(ReasonCode.EVIDENCE_MISSING, trigger),
            ))
    reasons = tuple(reason for result in results for reason in result.reasons)
    prevented = any(
        result.decision == RerunDecision.NOT_RERUNNABLE for result in results
    )
    decision = (
        RerunDecision.NOT_RERUNNABLE if prevented
        else RerunDecision.UNKNOWN if any(item.decision == RerunDecision.UNKNOWN for item in results)
        else RerunDecision.RERUN
    )
    return _result(decision, *reasons)


def submit_rerun(
    request: RerunRequest, *, catalog: ArtifactCatalog, registry: RunRegistry,
    adapter: ToolAdapter,
) -> RerunResult:
    """Explicitly submit one approved candidate; normal admission can still fail.

    Registry/admission exceptions propagate unchanged. No retries, reservations,
    input binding, cleanup or other work is performed by this boundary.
    """
    assessment = assess_rerun(request, catalog=catalog, registry=registry)
    if assessment.decision != RerunDecision.RERUN:
        return assessment
    candidate = request.candidate
    if candidate is None:
        raise ValueError("Approved rerun requires an explicit candidate")
    if (
        registry.catalog is not catalog or adapter.tool != candidate.tool
        or candidate.name not in adapter.operations
        or adapter.executable != candidate.command.executable or adapter.cwd != candidate.command.cwd
    ):
        return _result(
            RerunDecision.NOT_RERUNNABLE, RerunReason(ReasonCode.TARGET_NOT_RERUNNABLE),
        )
    admitted = registry.start(adapter, candidate)
    return replace(assessment, run_id=admitted["id"])
