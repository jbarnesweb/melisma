"""Read-only, target-scoped run/reuse/blocked planning; never execution.

Two layers: plan_target decides from per-operation StageEvidence; plan_from_state
derives that evidence from the ArtifactCatalog, RunRegistry, currentness and
readiness models. Neither runs tools, creates attempts, or mutates any state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Mapping

from ..orchestration.artifact_catalog import ArtifactCatalog, CatalogEntry
from ..orchestration.execution import RunRegistry, StageState
from ..orchestration.process import Operation
from .currentness import (
    ArtifactAssessment, Availability, Currentness, ProductionSelection, assess_currentness,
)
from .readiness import (
    CandidateEvidence, CandidateReadiness, ReadinessReason, ReasonCode, assess_readiness,
)
from .workflows import stage_dependencies


class PlanDecision(str, Enum):
    RUN = "run"
    REUSE = "reuse"
    BLOCKED = "blocked"


class PlanReason(str, Enum):
    CURRENT_REUSABLE = "current-reusable"
    STALE = "stale"
    FAILED = "failed"
    MISSING = "missing"
    DEPENDENCY_BLOCKED = "dependency-blocked"
    READINESS_BLOCKED = "readiness-blocked"


class WorkState(str, Enum):
    """Observed state of an operation's required work. UNKNOWN is never reusable."""

    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Node:
    """One independently runnable/reusable operation inside a workflow."""

    id: str
    workflow: str
    stage: str
    edge_roles: tuple[str, ...] = ()
    internal: tuple[tuple[str, str, str], ...] = ()  # role, producer node, artifact type
    optional_roles: tuple[str, ...] = ()


NODES: dict[str, Node] = {item.id: item for item in (
    Node("generate-abc", "generate-abc", "melisma.abc"),
    Node("generate-ustx", "generate-ustx", "melisma.ustx"),
    Node("render-vocals", "render-vocals", "openutau.render", ("ustx",)),
    Node("yue2-instrumental.prepare", "generate-yue2-audio",
         "yue2-instrumental.prepare", ("abc",)),
    Node("yue2-instrumental.generate", "generate-yue2-audio",
         "yue2-instrumental.generate",
         internal=(("prepared-manifest", "yue2-instrumental.prepare", "configuration"),)),
    Node("yue2-native.generate", "generate-yue2-audio", "yue2-native.generate",
         ("abc",), optional_roles=("abc",)),
    Node("mix-audio", "mix-audio", "ffmpeg.mix", ("vocal", "instrumental")),
    Node("play-audio", "play-audio", "ffplay.play", ("audio",)),
)}
_YUE2_PRODUCERS = {
    "instrumental-audio": "yue2-instrumental.generate",
    "yue2-audio": "yue2-native.generate",
}


def _producer(workflow: str, artifact_type: str) -> str | None:
    if workflow == "generate-yue2-audio":
        return _YUE2_PRODUCERS.get(artifact_type)
    return workflow if workflow in NODES else None


def options(node: str, role: str) -> tuple[tuple[str, str], ...]:
    """Possible (producer node, artifact type) pairs for an input role."""
    item = NODES[node]
    found = {(producer, kind) for name, producer, kind in item.internal if name == role}
    if role in item.edge_roles:
        for edge in stage_dependencies(item.workflow):
            producer = _producer(edge.upstream, edge.artifact_type)
            if edge.input == role and producer is not None:
                found.add((producer, edge.artifact_type))
    return tuple(sorted(found))


def _roles(node: str) -> tuple[str, ...]:
    item = NODES[node]
    return tuple(sorted({*item.edge_roles, *(name for name, _, _ in item.internal)}))


@dataclass(frozen=True)
class InputBinding:
    """How one input role is satisfied: a producer node, an imported artifact,
    or (neither) unresolved because no unambiguous selected source exists."""

    role: str
    producer: str | None = None
    imported: bool = False


@dataclass(frozen=True)
class StageEvidence:
    """Evidence for one operation. The default is unknown, never missing-and-ready.

    work: CURRENT is the only reusable state. Failed work, including retained
    partial output, must be FAILED. readiness/readiness_reasons come from Step 5
    for this operation's own replacement candidate. inputs: selected bindings;
    None uses the declared single-producer graph.
    """

    work: WorkState = WorkState.UNKNOWN
    readiness: CandidateReadiness = CandidateReadiness.UNKNOWN
    readiness_reasons: tuple[ReadinessReason, ...] = ()
    inputs: tuple[InputBinding, ...] | None = None
    detail: str | None = None


@dataclass(frozen=True)
class PlanStage:
    stage: str
    decision: PlanDecision
    reason: PlanReason
    prerequisites: tuple[str, ...] = ()
    detail: str | None = None
    workflow: str | None = None


@dataclass(frozen=True)
class Plan:
    target: str
    stages: tuple[PlanStage, ...] = field(default_factory=tuple)

    def decision(self, stage: str) -> PlanDecision:
        return next(item.decision for item in self.stages if item.stage == stage)


# Input-attributed blockers that an earlier run in the same plan replaces.
_REPLACEABLE = frozenset({
    ReasonCode.ARTIFACT_STALE, ReasonCode.ARTIFACT_MISSING, ReasonCode.ARTIFACT_INVALID,
    ReasonCode.ARTIFACT_CURRENTNESS_UNKNOWN, ReasonCode.EVIDENCE_MISSING,
})
_OWN = {
    WorkState.STALE: PlanReason.STALE, WorkState.FAILED: PlanReason.FAILED,
    WorkState.MISSING: PlanReason.MISSING,
}


def _bindings(node: str, evidence: Mapping[str, StageEvidence]) -> tuple[InputBinding, ...]:
    item = evidence.get(node)
    chosen = {b.role: b for b in (item.inputs or ())} if item is not None else {}
    for role in _roles(node):
        if role in chosen or role in NODES[node].optional_roles:
            continue
        found = options(node, role)
        chosen[role] = InputBinding(role, found[0][0] if len(found) == 1 else None)
    return tuple(chosen[role] for role in sorted(chosen))


def plan_target(target: str, evidence: Mapping[str, StageEvidence]) -> Plan:
    """Plan the target and its selected prerequisites; earlier runs satisfy later ones."""
    if target not in NODES:
        raise ValueError(f"Unknown plannable stage: {target}")
    decided: dict[str, PlanStage] = {}
    binds: dict[str, tuple[InputBinding, ...]] = {}
    active: set[str] = set()

    def resolve(node: str) -> PlanStage:
        if node in decided:
            return decided[node]
        if node in active:
            raise ValueError(f"Cyclic stage dependency: {node}")
        active.add(node)
        item = evidence.get(node, StageEvidence())
        binds[node] = bindings = _bindings(node, evidence)
        producers = tuple(sorted({b.producer for b in bindings if b.producer}))
        for producer in producers:
            resolve(producer)

        def outcome(decision: PlanDecision, reason: PlanReason) -> PlanStage:
            return PlanStage(
                node, decision, reason, producers, item.detail, NODES[node].workflow,
            )

        if item.work == WorkState.CURRENT:
            result = outcome(PlanDecision.REUSE, PlanReason.CURRENT_REUSABLE)
        else:
            replaced = {
                b.role for b in bindings
                if b.producer and decided[b.producer].decision == PlanDecision.RUN
            }
            blocked = {
                b.role for b in bindings
                if (b.producer is None and not b.imported)
                or (b.producer and decided[b.producer].decision == PlanDecision.BLOCKED)
            }
            remaining = [
                r for r in item.readiness_reasons
                if not (r.code in _REPLACEABLE and r.input_role in replaced)
            ]
            own = [r for r in remaining if r.input_role not in blocked]
            if own or (
                item.readiness != CandidateReadiness.READY and not item.readiness_reasons
            ):
                result = outcome(PlanDecision.BLOCKED, PlanReason.READINESS_BLOCKED)
            elif blocked:
                result = outcome(PlanDecision.BLOCKED, PlanReason.DEPENDENCY_BLOCKED)
            elif item.work == WorkState.UNKNOWN:
                result = outcome(PlanDecision.BLOCKED, PlanReason.READINESS_BLOCKED)
            else:
                result = outcome(PlanDecision.RUN, _OWN[item.work])
        active.discard(node)
        decided[node] = result
        return result

    resolve(target)
    depth: dict[str, int] = {}

    def level(node: str) -> int:
        if node not in depth:
            depth[node] = 1 + max((level(p) for p in decided[node].prerequisites), default=-1)
        return depth[node]

    return Plan(target, tuple(decided[n] for n in sorted(decided, key=lambda n: (level(n), n))))


@dataclass(frozen=True)
class StageRequest:
    """Desired replacement candidate (with bound inputs/configuration) and its
    Step 5 evidence for one operation. No candidate means unknown evidence."""

    candidate: Operation | None
    evidence: CandidateEvidence | None = None


def _imported(entry: CatalogEntry) -> bool:
    return entry.producer_run is None and entry.origin in ("imported", "edited")


def _input_bindings(
    node: str, candidate: Operation, entries: Mapping[str, CatalogEntry],
) -> tuple[InputBinding, ...]:
    """Selected artifact lineage controls producer choice; no ranking is used."""
    result = []
    for role in (*_roles(node), *NODES[node].optional_roles):
        selected = [item for item in candidate.inputs if item.role == role]
        if not selected:
            continue
        entry = entries.get(selected[0].artifact_id) if len(set(selected)) == 1 else None
        if entry is None:
            result.append(InputBinding(role))
        elif _imported(entry):
            result.append(InputBinding(role, imported=True))
        else:
            matches = {
                producer for producer, kind in options(node, role)
                if entry.type == kind and entry.stage is not None
                and (entry.stage == NODES[producer].stage
                     or NODES[producer].workflow in ("generate-abc", "generate-ustx")
                     and entry.stage.startswith("melisma."))
            }
            result.append(InputBinding(role, matches.pop() if len(matches) == 1 else None))
    return tuple(result)


def _attempt(
    stage: str, catalog: ArtifactCatalog, registry: RunRegistry | None,
) -> tuple[str, str] | None:
    """Latest retained attempt for the stage as (outcome, run id)."""
    with catalog.lock:
        history = [(r.id, r.success) for r in catalog.runs.values() if r.stage == stage]
        known = {run_id for run_id, _ in history}
    latest = None if not history else (
        "success" if history[-1][1] else "failed", history[-1][0],
    )
    if registry is not None:
        with registry.lock:
            live = [r for r in registry.runs.values() if r.operation.stage == stage]
        # Registry runs the catalog has not recorded are the newest evidence.
        newer = [r for r in live if r.id not in known]
        if newer:
            state = newer[-1].stage_state
            outcome = (
                "active" if state == StageState.RUNNING
                else "success" if state == StageState.SUCCESS else "failed"
            )
            return outcome, newer[-1].id
    return latest


def _assess(
    catalog: ArtifactCatalog, selections: dict[str, tuple[ProductionSelection, ...]],
) -> tuple[dict[str, tuple[ArtifactAssessment, ...]], set[str]]:
    """Joint currentness; nodes whose selections cannot be assessed stay unknown."""
    usable = dict(selections)
    unassessable: set[str] = set()
    while usable:
        try:
            flat = tuple(s for items in usable.values() for s in items)
            assessed = assess_currentness(catalog, flat)
        except ValueError:
            bad = set()
            for node, items in usable.items():
                try:
                    assess_currentness(catalog, items)
                except (ValueError, KeyError):
                    bad.add(node)
            if not bad:
                bad = set(usable)
            unassessable |= bad
            usable = {k: v for k, v in usable.items() if k not in bad}
            continue
        keys = {(s.artifact_id, s.producer_run): node for node, items in usable.items() for s in items}
        grouped: dict[str, list[ArtifactAssessment]] = {node: [] for node in usable}
        for item in assessed:
            grouped[keys[(item.artifact_id, item.producer_run)]].append(item)
        return {k: tuple(v) for k, v in grouped.items()}, unassessable
    return {}, unassessable


def _fold(items: tuple[ArtifactAssessment, ...]) -> WorkState:
    if any(item.availability == Availability.MISSING for item in items):
        return WorkState.MISSING
    if any(item.currentness == Currentness.STALE for item in items):
        return WorkState.STALE
    if all(item.currentness == Currentness.CURRENT for item in items):
        return WorkState.CURRENT
    return WorkState.UNKNOWN


def plan_from_state(
    target: str, *, catalog: ArtifactCatalog, requests: Mapping[str, StageRequest],
    registry: RunRegistry | None = None,
) -> Plan:
    """Derive evidence from catalog/registry/currentness/readiness, then plan.

    requests maps operation ids (see NODES) to desired replacement candidates.
    Without a request, candidate or readiness evidence an operation is unknown
    and conservatively blocked. A failed latest attempt (recorded run or
    registry state) is FAILED even when partial output is retained.
    """
    if target not in NODES:
        raise ValueError(f"Unknown plannable stage: {target}")
    unknown = set(requests) - set(NODES)
    if unknown:
        raise ValueError(f"Unknown plannable stage: {sorted(unknown)[0]}")
    with catalog.lock:
        entries = dict(catalog.entries)
    attempts: dict[str, tuple[str, str] | None] = {}
    selections: dict[str, tuple[ProductionSelection, ...]] = {}
    for node, request in sorted(requests.items()):
        if request.candidate is None:
            continue
        attempts[node] = attempt = _attempt(NODES[node].stage, catalog, registry)
        if attempt is not None and attempt[0] == "success":
            outputs = [e for e in entries.values() if e.producer_run == attempt[1]]
            if outputs:
                selections[node] = tuple(
                    ProductionSelection(NODES[node].workflow, e.id, request.candidate, attempt[1])
                    for e in sorted(outputs, key=lambda e: e.id)
                )
    assessments, unassessable = _assess(catalog, selections)
    everything = tuple(item for items in assessments.values() for item in items)
    evidence: dict[str, StageEvidence] = {}
    for node, request in sorted(requests.items()):
        candidate = request.candidate
        if candidate is None:
            continue
        attempt, detail = attempts.get(node), None
        if attempt is None:
            work = WorkState.MISSING
        elif attempt[0] == "failed":
            work = WorkState.FAILED
        elif attempt[0] == "active":
            work, detail = WorkState.UNKNOWN, "active-run"
        elif node in unassessable:
            work, detail = WorkState.UNKNOWN, "unassessable-currentness"
        elif node not in assessments:
            work = WorkState.MISSING
        else:
            work = _fold(assessments[node])
            if work == WorkState.UNKNOWN:
                detail = "unknown-currentness"
        readiness = assess_readiness(
            catalog, NODES[node].workflow, candidate, evidence=request.evidence,
            currentness=everything, registry=registry,
        )
        evidence[node] = StageEvidence(
            work, readiness.readiness, readiness.reasons,
            _input_bindings(node, candidate, entries), detail,
        )
    return plan_target(target, evidence)
