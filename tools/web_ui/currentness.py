"""Read-only assessment of explicitly selected productions, not execution planning."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path

from ..orchestration.artifact_catalog import (
    ArtifactCatalog, CatalogEntry, ConsumedRun, semantic_output_type,
)
from ..orchestration.execution import StageState
from ..orchestration.process import ExpectedArtifact, Operation
from ..orchestration.provenance import (
    ProductionProvenance, production_provenance, same_production_context,
)
from ..orchestration.yue2 import has_manifest_completion
from .workflows import WorkflowDefinition, workflow_definitions


class Currentness(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


class Availability(str, Enum):
    PRESENT = "present"
    MISSING = "missing"


class OutputIntegrity(str, Enum):
    ORIGINAL = "original"
    MODIFIED = "modified"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductionSelection:
    """Caller-selected workflow, successful output/run and captured candidate.

    Multiple operations can belong to one workflow. None means no candidate,
    not permission to guess from saved settings or the latest stage output.
    """

    workflow: str
    artifact_id: str
    candidate: Operation | None
    producer_run: str | None = None


@dataclass(frozen=True)
class CurrentnessReason:
    code: str
    field: str | None = None
    input_role: str | None = None
    artifact_type: str | None = None
    upstream_artifact: str | None = None
    upstream_workflow: str | None = None
    upstream_run: str | None = None


@dataclass(frozen=True)
class ArtifactAssessment:
    workflow: str
    stage: str | None
    artifact_id: str
    producer_run: str | None
    artifact_type: str
    currentness: Currentness
    availability: Availability
    integrity: OutputIntegrity
    reasons: tuple[CurrentnessReason, ...] = ()


@dataclass(frozen=True)
class _Context:
    selection: ProductionSelection
    entry: CatalogEntry
    recorded: ProductionProvenance | None
    candidate: ProductionProvenance | None
    assessment: ArtifactAssessment


def _declared_output_types(operation: Operation) -> set[str]:
    return {
        semantic_output_type(operation.stage, output) for output in operation.expected
    }


def supports_production(
    workflow: WorkflowDefinition, stage: str | None, artifact_type: str, path: str,
) -> bool:
    if workflow.accepts_production(stage, artifact_type):
        return True
    if stage is None or not workflow.owns_production(stage) or not has_manifest_completion(stage):
        return False
    # Retained completion records were catalogued from file-kind manifest entries.
    # Their semantic type must still agree with the actual producer and filename.
    return artifact_type == semantic_output_type(
        stage, ExpectedArtifact(Path(path)),
    )


def _differences(
    recorded: ProductionProvenance, candidate: ProductionProvenance,
) -> tuple[CurrentnessReason, ...]:
    reasons = []
    for field in (
        "version", "stage", "tool", "operation", "contract", "executable",
        "source_root", "output_type",
    ):
        if getattr(recorded, field) != getattr(candidate, field):
            reasons.append(CurrentnessReason("context_changed", field))
    previous, selected = dict(recorded.settings), dict(candidate.settings)
    for key in sorted(previous.keys() | selected.keys()):
        if (
            key not in previous or key not in selected
            or json.dumps(previous[key], allow_nan=False) != json.dumps(selected[key], allow_nan=False)
        ):
            reasons.append(CurrentnessReason("setting_changed", f"settings.{key}"))
    old_inputs = {item.role: item for item in recorded.inputs}
    new_inputs = {item.role: item for item in candidate.inputs}
    for role in sorted(old_inputs.keys() | new_inputs.keys()):
        if old_inputs.get(role) != new_inputs.get(role):
            item = new_inputs.get(role) or old_inputs[role]
            reasons.append(CurrentnessReason(
                "input_changed", input_role=role, artifact_type=item.type,
                upstream_artifact=item.artifact_id,
            ))
    return tuple(reasons)


def _context(
    selection: ProductionSelection, entry: CatalogEntry, runs: dict[str, ConsumedRun],
) -> _Context:
    run_id = selection.producer_run if selection.producer_run is not None else entry.producer_run
    run = runs.get(run_id) if run_id is not None else None
    if run is None and selection.producer_run is not None and run_id != entry.producer_run:
        raise ValueError("Unknown selected production run")
    productions = () if run is None else tuple(
        item.provenance for item in run.productions if item.artifact_id == entry.id
    )
    if len(productions) > 1:
        raise ValueError("ambiguous historical production")
    if run is not None and not productions and entry.producer_run != run_id:
        raise ValueError("Selected run does not produce the selected artifact")
    recorded = None
    if productions:
        recorded = productions[0]
    elif entry.producer_run == run_id:
        recorded = entry.provenance
    output_type = recorded.output_type if recorded is not None else entry.type
    candidate = (
        production_provenance(selection.candidate, output_type)
        if selection.candidate is not None else None
    )
    successful = run.success if run is not None else (
        run_id is not None and entry.producer_run == run_id
        and entry.production_status in ("successful", "edited")
    )
    availability = Availability.PRESENT if entry.exists else Availability.MISSING
    integrity = OutputIntegrity.UNKNOWN
    if (
        entry.exists and entry.revision is not None
        and recorded is not None and recorded.output_revision is not None
    ):
        integrity = (
            OutputIntegrity.ORIGINAL if entry.revision == recorded.output_revision
            else OutputIntegrity.MODIFIED
        )
    state = Currentness.UNKNOWN
    if not entry.exists:
        reasons = (CurrentnessReason("output_missing"),)
    elif not successful:
        reasons = (CurrentnessReason("not_successful"),)
    elif recorded is None or recorded.output_revision is None:
        reasons = (CurrentnessReason("unknown_provenance"),)
    elif candidate is None:
        reasons = (CurrentnessReason("unknown_candidate"),)
    elif same_production_context(recorded, candidate):
        state, reasons = Currentness.CURRENT, ()
    else:
        state, reasons = Currentness.STALE, _differences(recorded, candidate)
    stage = recorded.stage if recorded is not None else run.stage if run is not None else entry.stage
    return _Context(selection, entry, recorded, candidate, ArtifactAssessment(
        selection.workflow, stage,
        entry.id, run_id, output_type, state, availability, integrity, reasons,
    ))


def assess_currentness(
    catalog: ArtifactCatalog, selections: tuple[ProductionSelection, ...],
) -> tuple[ArtifactAssessment, ...]:
    """Assess only supplied contexts using catalog observations, without any I/O.

    Inter-workflow links require the existing StageDependency. Internal links
    within a grouped workflow (helper prepare -> generate) use actual captured
    typed lineage, never a second hard-coded graph. Missing/unknown evidence
    cannot be promoted to definite stale by propagation.
    """
    definitions = {item.key: item for item in workflow_definitions()}
    with catalog.lock:
        entries, runs = dict(catalog.entries), dict(catalog.runs)
    contexts = []
    seen = set()
    for selection in selections:
        if selection.workflow not in definitions:
            raise ValueError("Unknown assessment workflow")
        workflow = definitions[selection.workflow]
        operation = selection.candidate
        if operation is not None:
            if not operation.expected:
                raise ValueError("Assessment requires a generation output, not playback or verification")
        context = _context(selection, entries[selection.artifact_id], runs)
        if not supports_production(
            workflow, context.assessment.stage, context.assessment.artifact_type, context.entry.path,
        ):
            raise ValueError("Selected workflow does not support historical production/output")
        if operation is not None:
            if not supports_production(
                workflow, operation.stage, context.assessment.artifact_type, context.entry.path,
            ):
                raise ValueError("Candidate production/output does not belong to selected workflow")
            if (
                context.assessment.artifact_type not in _declared_output_types(operation)
                and not has_manifest_completion(operation.stage)
            ):
                raise ValueError("Candidate does not declare the selected semantic output")
        key = (context.assessment.artifact_id, context.assessment.producer_run)
        if key in seen:
            raise ValueError("duplicate selected production")
        seen.add(key)
        contexts.append(context)
    contexts.sort(key=lambda item: (
        item.assessment.workflow, item.assessment.artifact_id,
        item.assessment.producer_run or "",
    ))

    # A link needs both current selection and historical consumption of these
    # specific output bytes. A newer unrelated run/path cannot supply it.
    links: set[tuple[int, int, str]] = set()
    for target, consumer in enumerate(contexts):
        if consumer.candidate is None or consumer.recorded is None:
            continue
        old_inputs = {item.role: item for item in consumer.recorded.inputs}
        for item in consumer.candidate.inputs:
            if old_inputs.get(item.role) != item:
                continue
            sources = []
            for source, upstream in enumerate(contexts):
                assessment = upstream.assessment
                revision = (
                    upstream.recorded.output_revision if upstream.recorded is not None
                    else upstream.entry.revision
                )
                if (
                    item.artifact_id != assessment.artifact_id
                    or item.type != assessment.artifact_type
                    or item.revision != revision
                ):
                    continue
                same_workflow = assessment.workflow == consumer.assessment.workflow
                qualified = any(
                    edge.upstream == assessment.workflow
                    and edge.input == item.role and edge.artifact_type == item.type
                    for edge in definitions[consumer.assessment.workflow].stage_dependencies
                )
                if same_workflow or qualified:
                    sources.append(source)
            if len(sources) > 1:
                raise ValueError("ambiguous selected producer for consumed artifact revision")
            if sources:
                links.add((sources[0], target, item.role))

    downstream: dict[int, set[int]] = {}
    for source, target, _ in links:
        downstream.setdefault(source, set()).add(target)
    states = [context.assessment.currentness for context in contexts]
    pending = deque(range(len(contexts)))
    while pending:
        source = pending.popleft()
        if states[source] == Currentness.CURRENT:
            continue
        for target in sorted(downstream.get(source, ())):
            if contexts[target].assessment.currentness == Currentness.UNKNOWN:
                continue
            desired = states[source]
            if states[target] != Currentness.STALE and states[target] != desired:
                states[target] = desired
                pending.append(target)
    # Each known node changes at most CURRENT -> UNKNOWN -> STALE. Immediate
    # final-source reasons are built once, so cycles cannot grow reason chains.
    results = []
    for target, context in enumerate(contexts):
        reasons = set(context.assessment.reasons)
        for source, consumer, role in sorted(links):
            if consumer != target or context.assessment.currentness == Currentness.UNKNOWN:
                continue
            if states[target] == states[source] and states[source] != Currentness.CURRENT:
                upstream = contexts[source].assessment
                reasons.add(CurrentnessReason(
                    "upstream_stale" if states[source] == Currentness.STALE else "upstream_unknown",
                    input_role=role, artifact_type=upstream.artifact_type,
                    upstream_artifact=upstream.artifact_id, upstream_workflow=upstream.workflow,
                    upstream_run=upstream.producer_run,
                ))
        ordered = tuple(sorted(reasons, key=lambda reason: (
            reason.code, reason.field or "", reason.input_role or "",
            reason.artifact_type or "", reason.upstream_artifact or "",
            reason.upstream_workflow or "", reason.upstream_run or "",
        )))
        results.append(replace(context.assessment, currentness=states[target], reasons=ordered))
    return tuple(results)


def assessed_stage_document(
    document: dict[str, object], assessments: tuple[ArtifactAssessment, ...],
) -> dict[str, object]:
    """Add selected artifact currentness; only idle Ready can project as Stale.

    All retained attempt outcomes and explicit disabled/not-configured states
    remain untouched. This never changes Execution.stage_state or the input.
    """
    selected = tuple(item for item in assessments if item.stage == document["stage"])
    stale = (
        document["state"] == StageState.READY and document["run_id"] is None
        and any(item.currentness == Currentness.STALE for item in selected)
    )
    return {
        **document, "state": StageState.STALE.value if stale else document["state"],
        "currentness": [asdict(item) for item in selected],
    }
