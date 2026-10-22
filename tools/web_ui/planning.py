"""Read-only, target-scoped run/reuse/blocked planning; never execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from .currentness import ArtifactAssessment, Availability, Currentness
from .readiness import CandidateReadiness
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
    """Existing outcome of a stage's required work, as observed by the caller."""

    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True)
class StageEvidence:
    """Supplied observations for one stage.

    work: only CURRENT successful work is reusable; failed work, including
    retained partial output, must be reported FAILED regardless of artifacts.
    readiness: Step 5 readiness of this stage's own replacement candidate.
    A stage without evidence is treated as MISSING work with READY readiness.
    """

    work: WorkState = WorkState.MISSING
    readiness: CandidateReadiness = CandidateReadiness.READY


@dataclass(frozen=True)
class PlanStage:
    stage: str
    decision: PlanDecision
    reason: PlanReason
    prerequisites: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    target: str
    stages: tuple[PlanStage, ...] = field(default_factory=tuple)

    def decision(self, stage: str) -> PlanDecision:
        return next(item.decision for item in self.stages if item.stage == stage)


def work_state(
    assessments: tuple[ArtifactAssessment, ...], *, failed: bool = False,
) -> WorkState:
    """Fold Step 3/4 assessments into WorkState without new currentness rules.

    Unknown currentness is not proof of currentness, so it is never reusable.
    """
    if failed:
        return WorkState.FAILED
    if not assessments or any(item.availability == Availability.MISSING for item in assessments):
        return WorkState.MISSING
    if all(item.currentness == Currentness.CURRENT for item in assessments):
        return WorkState.CURRENT
    return WorkState.STALE


_RANK = {PlanDecision.REUSE: 0, PlanDecision.RUN: 1, PlanDecision.BLOCKED: 2}
_OWN = {
    WorkState.STALE: PlanReason.STALE,
    WorkState.FAILED: PlanReason.FAILED,
    WorkState.MISSING: PlanReason.MISSING,
}


def plan_target(target: str, evidence: Mapping[str, StageEvidence]) -> Plan:
    """Plan the target and its relevant prerequisites from supplied evidence.

    Earlier runnable stages in the plan satisfy later prerequisites. Distinct
    inputs are all required; alternative producers for one input are resolved
    to a single deterministic source (reuse, then run, then blocked, then name),
    considering only stages with supplied evidence. Pure: no I/O or mutation.
    """
    stage_dependencies(target)
    decided: dict[str, PlanStage] = {}
    active: set[str] = set()

    def sources(stage: str) -> tuple[str, ...]:
        roles: dict[str, set[str]] = {}
        for edge in stage_dependencies(stage):
            roles.setdefault(edge.input, set()).add(edge.upstream)
        chosen, unsatisfiable = [], False
        for role in sorted(roles):
            options = sorted(
                item for item in roles[role]
                if len(roles[role]) == 1 or item in evidence
            )
            if not options:
                unsatisfiable = True
                continue
            ranked = sorted(
                (resolve(item) for item in options),
                key=lambda item: (_RANK[item.decision], item.stage),
            )
            chosen.append(ranked[0].stage)
        if unsatisfiable:
            chosen.append("")
        return tuple(sorted(set(chosen)))

    def resolve(stage: str) -> PlanStage:
        if stage in decided:
            return decided[stage]
        if stage in active:
            raise ValueError(f"Cyclic stage dependency: {stage}")
        active.add(stage)
        item = evidence.get(stage, StageEvidence())
        upstream = sources(stage)
        prerequisites = tuple(name for name in upstream if name)
        if item.work == WorkState.CURRENT:
            result = PlanStage(
                stage, PlanDecision.REUSE, PlanReason.CURRENT_REUSABLE, prerequisites,
            )
        elif item.readiness != CandidateReadiness.READY:
            result = PlanStage(
                stage, PlanDecision.BLOCKED, PlanReason.READINESS_BLOCKED, prerequisites,
            )
        elif "" in upstream or any(
            decided[name].decision == PlanDecision.BLOCKED for name in prerequisites
        ):
            result = PlanStage(
                stage, PlanDecision.BLOCKED, PlanReason.DEPENDENCY_BLOCKED, prerequisites,
            )
        else:
            result = PlanStage(stage, PlanDecision.RUN, _OWN[item.work], prerequisites)
        active.discard(stage)
        decided[stage] = result
        return result

    resolve(target)
    # Alternatives that lost selection were evaluated but are not part of this plan.
    relevant: set[str] = set()
    pending = [target]
    while pending:
        name = pending.pop()
        if name not in relevant:
            relevant.add(name)
            pending.extend(decided[name].prerequisites)
    depth: dict[str, int] = {}

    def level(name: str) -> int:
        if name not in depth:
            depth[name] = 1 + max(
                (level(item) for item in decided[name].prerequisites), default=-1,
            )
        return depth[name]

    ordered = sorted(relevant, key=lambda name: (level(name), name))
    return Plan(target, tuple(decided[name] for name in ordered))
