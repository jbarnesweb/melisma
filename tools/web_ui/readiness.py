"""Pure readiness of an explicitly captured candidate, never execution admission."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from ..orchestration.adapters import FFmpegAdapter, MelismaAdapter, OpenUtauAdapter
from ..orchestration.artifact_catalog import (
    ARTIFACT_TYPES, ArtifactCatalog, CatalogEntry, ConsumedRun,
    compatible_artifact_type, semantic_output_type,
)
from ..orchestration.capabilities import (
    Capability, Readiness, SourceIdentity, State, Verification,
)
from ..orchestration.execution import RunRegistry, StageState
from ..orchestration.playback import FFplayAdapter
from ..orchestration.process import Operation, OperationInput
from ..orchestration.yue2 import InstrumentalAdapter, NativeYuE2Adapter
from .currentness import ArtifactAssessment, Currentness, supports_production
from .workflows import ARTIFACT_INPUT_TYPES, WorkflowDefinition, workflow_definitions


class CandidateReadiness(str, Enum):
    READY = "ready"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class ReasonCode(str, Enum):
    REQUIRED_INPUT_MISSING = "required_input_missing"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_INVALID = "artifact_invalid"
    ARTIFACT_TYPE_INCOMPATIBLE = "artifact_type_incompatible"
    ARTIFACT_ROLE_MISMATCH = "artifact_role_mismatch"
    ARTIFACT_IDENTITY_MISMATCH = "artifact_identity_mismatch"
    ARTIFACT_REVISION_MISMATCH = "artifact_revision_mismatch"
    ARTIFACT_STALE = "artifact_stale"
    ARTIFACT_CURRENTNESS_UNKNOWN = "artifact_currentness_unknown"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CONFIGURATION_MISSING = "configuration_missing"
    CONFIGURATION_INVALID = "configuration_invalid"
    CANDIDATE_CONTRACT_INVALID = "candidate_contract_invalid"
    SELECTED_LINEAGE_AMBIGUOUS = "selected_lineage_ambiguous"
    OUTPUT_PATH_ACTIVE = "output_path_active"
    INPUT_PATH_ACTIVE = "input_path_active"
    OUTPUT_PATH_OCCUPIED = "output_path_occupied"
    EVIDENCE_MISSING = "evidence_missing"


@dataclass(frozen=True)
class ReadinessReason:
    code: ReasonCode
    input_role: str | None = None
    artifact_id: str | None = None
    artifact_type: str | None = None
    upstream_workflow: str | None = None
    upstream_artifact: str | None = None
    source_code: str | None = None


@dataclass(frozen=True)
class ReadinessAssessment:
    workflow: str | None
    stage: str | None
    readiness: CandidateReadiness
    reasons: tuple[ReadinessReason, ...] = ()


@dataclass(frozen=True)
class CandidateEvidence:
    """Caller-supplied evidence for this exact immutable candidate.

    validated is the existing construction/validation boundary's result for
    settings, resources and path preconditions, not permission to skip selected
    input checks. None means incomplete evidence. Capabilities are selected
    required records, not an unfiltered discovery report. READY configuration
    alone proves neither capability availability nor runtime integration.
    """

    candidate: Operation
    configuration: StageState | None = None
    validated: bool | None = None
    capabilities: tuple[Capability, ...] = ()

    def __post_init__(self) -> None:
        if self.configuration not in (
            None, StageState.READY, StageState.NOT_CONFIGURED, StageState.DISABLED,
        ) or self.validated is not None and type(self.validated) is not bool:
            raise ValueError("expected candidate configuration/validation evidence")


_ADAPTERS = (
    MelismaAdapter, OpenUtauAdapter, FFmpegAdapter, NativeYuE2Adapter,
    InstrumentalAdapter, FFplayAdapter,
)
_STALE_CODES = frozenset({
    "context_changed", "setting_changed", "input_changed", "upstream_stale",
})
_UNKNOWN_CODES = frozenset({
    ReasonCode.ARTIFACT_CURRENTNESS_UNKNOWN, ReasonCode.EVIDENCE_MISSING,
})


def _artifact_id(value: str | None) -> str | None:
    return value if value is not None and re.fullmatch(r"[0-9a-f]{32}", value) else None


def _currentness_reasons(
    source: OperationInput, entry: CatalogEntry, runs: dict[str, ConsumedRun],
    assessments: tuple[ArtifactAssessment, ...], definitions: dict[str, WorkflowDefinition],
) -> tuple[ReadinessReason, ...]:
    matching = set()
    for item in assessments:
        if item.artifact_id != entry.id or item.artifact_type != entry.type:
            continue
        workflow = definitions.get(item.workflow)
        if workflow is None or not supports_production(
            workflow, item.stage, item.artifact_type, entry.path,
        ):
            continue
        run = runs.get(item.producer_run) if item.producer_run is not None else None
        productions = tuple(
            production.provenance for production in run.productions
            if production.artifact_id == entry.id
        ) if run is not None and run.success else ()
        if (
            not productions and entry.producer_run == item.producer_run
            and entry.provenance is not None
            and (run is None or run.success)
        ):
            productions = (entry.provenance,)
        if len(productions) != 1:
            continue
        recorded = productions[0]
        if (
            recorded.stage == item.stage and recorded.output_type == entry.type
            and recorded.output_revision == source.revision
        ):
            matching.add(item)
    identity = dict(
        input_role=source.role, artifact_id=_artifact_id(entry.id), artifact_type=entry.type,
    )
    if not matching:
        # Imported/manual bytes have no desired production context to regenerate.
        if entry.origin in ("imported", "edited") and entry.producer_run is None:
            return ()
        return (ReadinessReason(ReasonCode.ARTIFACT_CURRENTNESS_UNKNOWN, **identity),)
    if len({item.producer_run for item in matching}) > 1:
        return (ReadinessReason(ReasonCode.SELECTED_LINEAGE_AMBIGUOUS, **identity),)
    if len({item.currentness for item in matching}) != 1:
        return (ReadinessReason(ReasonCode.ARTIFACT_CURRENTNESS_UNKNOWN, **identity),)
    state = next(iter(matching)).currentness
    if state == Currentness.CURRENT:
        return ()
    if state == Currentness.UNKNOWN:
        return (ReadinessReason(ReasonCode.ARTIFACT_CURRENTNESS_UNKNOWN, **identity),)
    reasons = set()
    for assessment in matching:
        for reason in assessment.reasons:
            if reason.code in _STALE_CODES:
                reasons.add(ReadinessReason(
                    ReasonCode.ARTIFACT_STALE, **identity, source_code=reason.code,
                    upstream_artifact=_artifact_id(reason.upstream_artifact),
                    upstream_workflow=(
                        reason.upstream_workflow if reason.upstream_workflow in definitions else None
                    ),
                ))
    return tuple(reasons) or (ReadinessReason(ReasonCode.ARTIFACT_STALE, **identity),)


def assess_readiness(
    catalog: ArtifactCatalog, workflow: str, candidate: Operation | None, *,
    evidence: CandidateEvidence | None = None,
    currentness: tuple[ArtifactAssessment, ...] = (),
    registry: RunRegistry | None = None,
) -> ReadinessAssessment:
    """Assess supplied observations only; Blocked > Unknown > Ready.

    No path resolution, file access, discovery, input capture, currentness
    recomputation, reservation or history mutation occurs. This point-in-time
    assessment is not an admission token and does not enable deferred workflows.
    """
    definitions = {item.key: item for item in workflow_definitions()}
    definition = definitions.get(workflow)
    adapter = next((
        item for item in _ADAPTERS
        if candidate is not None and item.tool == candidate.tool
        and candidate.name in item.operations
    ), None)
    reasons: set[ReadinessReason] = set()
    stage = candidate.stage if candidate is not None and adapter is not None else None

    def add(code: ReasonCode, **identifiers: str | None) -> None:
        reasons.add(ReadinessReason(code, **identifiers))

    def result() -> ReadinessAssessment:
        state = (
            CandidateReadiness.BLOCKED if any(item.code not in _UNKNOWN_CODES for item in reasons)
            else CandidateReadiness.UNKNOWN if reasons else CandidateReadiness.READY
        )
        ordered = tuple(sorted(reasons, key=lambda item: json.dumps(asdict(item), sort_keys=True)))
        return ReadinessAssessment(workflow if definition is not None else None, stage, state, ordered)

    if definition is None:
        add(ReasonCode.CANDIDATE_CONTRACT_INVALID)
    if candidate is None:
        add(ReasonCode.EVIDENCE_MISSING)
        return result()
    playback = workflow == "play-audio" and stage == "ffplay.play" and not candidate.expected
    if adapter is None or definition is None or not (
        playback or definition.owns_production(stage) and candidate.expected
    ):
        add(ReasonCode.CANDIDATE_CONTRACT_INVALID)
    if definition is not None and not playback:
        output_types = {semantic_output_type(candidate.stage, item) for item in candidate.expected}
        if not {item.kind for item in definition.outputs} <= output_types:
            add(ReasonCode.CANDIDATE_CONTRACT_INVALID)
        for output in candidate.expected:
            type_ = semantic_output_type(candidate.stage, output)
            # Bundles can include sibling outputs owned by the same operation.
            if not any(supports_production(
                owner, candidate.stage, type_, str(output.path),
            ) for owner in definitions.values()):
                add(ReasonCode.CANDIDATE_CONTRACT_INVALID)
    if candidate.production is None and not playback:
        add(ReasonCode.EVIDENCE_MISSING)
    if evidence is None or evidence.candidate != candidate:
        add(ReasonCode.EVIDENCE_MISSING)
    else:
        if evidence.configuration is None or evidence.validated is None:
            add(ReasonCode.EVIDENCE_MISSING)
        if evidence.configuration == StageState.NOT_CONFIGURED:
            add(ReasonCode.CONFIGURATION_MISSING)
        elif evidence.configuration == StageState.DISABLED:
            add(ReasonCode.CAPABILITY_UNAVAILABLE)
        if evidence.validated is False:
            add(ReasonCode.CONFIGURATION_INVALID)
        required_tools = {candidate.tool}
        if playback or any(item.kind == "audio" for item in candidate.expected):
            required_tools.add("ffprobe")
        source = SourceIdentity(
            candidate.tool, f"{candidate.command.cwd}|{candidate.command.executable}",
            # Contract support is caller-validated, not a readiness version allowlist.
            candidate.production.contract if candidate.production is not None else "ffplay-phase1",
        )
        primary_values = {
            "executable": str(candidate.command.executable), "operation": candidate.name,
        }
        for tool in sorted(required_tools):
            # FFprobe has no selected-validator identity; assess supplied availability only.
            capabilities = tuple(
                item for item in evidence.capabilities
                if item.source.tool == tool and (tool != candidate.tool or item.source == source)
            )
            if not any(
                item.key == "executable"
                and (tool != candidate.tool or item.value == primary_values["executable"])
                for item in capabilities
            ):
                add(ReasonCode.EVIDENCE_MISSING)
            for item in capabilities:
                if item.state in (State.UNAVAILABLE, State.UNAUTHORIZED) or (
                    item.readiness in (Readiness.UNAVAILABLE, Readiness.UNAUTHORIZED)
                ):
                    add(ReasonCode.CAPABILITY_UNAVAILABLE)
                elif not (
                    item.state == State.READY and item.readiness == Readiness.READY
                    and item.verification == Verification.OBSERVED
                ):
                    add(ReasonCode.EVIDENCE_MISSING)
                elif (
                    tool == candidate.tool and item.key in primary_values
                    and item.value != primary_values[item.key]
                ):
                    # Resource filtering stays caller-owned; only these primary values are qualified.
                    add(ReasonCode.EVIDENCE_MISSING)

    registry_outputs: set[Path] = set()
    if registry is not None:
        with registry.lock:
            registry_outputs = {
                output.path for run in registry.runs.values() if not run.is_complete
                for output in run.operation.expected
            }
    with catalog.lock:
        entries, runs = dict(catalog.entries), dict(catalog.runs)
        active_paths = set(catalog.active_paths)
    for output in candidate.expected:
        if output.path in active_paths | registry_outputs:
            add(ReasonCode.OUTPUT_PATH_ACTIVE)
        if any(Path(item.path) == output.path and item.exists for item in entries.values()):
            add(ReasonCode.OUTPUT_PATH_OCCUPIED)

    required = dict(adapter.required_inputs[candidate.name]) if adapter is not None else {}
    accepted = {role: (type_,) for role, type_ in required.items()}
    accepted.update(ARTIFACT_INPUT_TYPES.get(workflow, {}))
    # Preserve actual occurrences before deduplicating evidence or collecting reasons.
    role_counts = Counter(item.role for item in candidate.inputs)
    for role in required.keys() - role_counts.keys():
        add(ReasonCode.REQUIRED_INPUT_MISSING, input_role=role)
    for role, count in role_counts.items():
        if count > 1 and (
            role in required or len({item for item in candidate.inputs if item.role == role}) > 1
        ):
            add(ReasonCode.SELECTED_LINEAGE_AMBIGUOUS, input_role=role if role in accepted else None)
    for source_input in set(candidate.inputs):
        role = source_input.role if source_input.role in accepted else None
        type_ = source_input.type if source_input.type in ARTIFACT_TYPES else None
        identity = dict(input_role=role, artifact_id=_artifact_id(source_input.artifact_id),
                        artifact_type=type_)
        if source_input.path in registry_outputs:
            add(ReasonCode.INPUT_PATH_ACTIVE, **identity)
        if role is None:
            add(ReasonCode.ARTIFACT_ROLE_MISMATCH, **identity)
        elif source_input.type not in accepted[role]:
            add(ReasonCode.ARTIFACT_TYPE_INCOMPATIBLE, **identity)
        entry = entries.get(source_input.artifact_id)
        if source_input.artifact_id is None or entry is None:
            add(ReasonCode.EVIDENCE_MISSING, **identity)
            continue
        if Path(entry.path) != source_input.path:
            add(ReasonCode.ARTIFACT_IDENTITY_MISMATCH, **identity)
        if role is not None and not compatible_artifact_type(entry, accepted[role]):
            add(ReasonCode.ARTIFACT_TYPE_INCOMPATIBLE, **identity)
        if not entry.exists:
            add(ReasonCode.ARTIFACT_MISSING, **identity)
            continue
        if entry.valid is False:
            add(ReasonCode.ARTIFACT_INVALID, **identity)
        elif entry.valid is None:
            add(ReasonCode.EVIDENCE_MISSING, **identity)
        if source_input.revision is None or entry.revision is None:
            add(ReasonCode.EVIDENCE_MISSING, **identity)
        elif source_input.revision != entry.revision:
            add(ReasonCode.ARTIFACT_REVISION_MISMATCH, **identity)
        if role is not None and entry.type in accepted[role] and (
            source_input.revision is not None and source_input.revision == entry.revision
        ):
            reasons.update(_currentness_reasons(
                source_input, entry, runs, currentness, definitions,
            ))
    return result()
