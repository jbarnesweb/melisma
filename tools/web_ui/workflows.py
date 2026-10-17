"""Declarative forms and execution boundaries, not runtime-readiness claims."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path

from ..orchestration.adapters import MelismaAdapter
from ..orchestration.capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, State, Verification,
)
from ..orchestration.configuration import Configuration, SavedSelection
from ..orchestration.discovery import CONTRACTS
from ..orchestration.process import ExpectedArtifact


@dataclass(frozen=True)
class FieldDefinition:
    key: str
    label: str
    required: bool
    kind: str
    help: str
    default: str | bool | int | float = ""
    provenance: tuple[Provenance, ...] = (Provenance.MANUAL,)
    configured: tuple[SavedSelection, ...] = ()
    verification: Verification = Verification.UNVERIFIED
    capabilities: tuple[Capability, ...] = ()
    minimum: int | float | None = None
    maximum: int | float | None = None
    exclusive_minimum: int | float | None = None
    step: int | float | str | None = None
    accept: str = ""
    selection: str | None = None
    path_limitation: str | None = None
    dependency: DependencyDefinition | None = None


@dataclass(frozen=True)
class DependencyBinding:
    parent_index: int
    choices: tuple[int, ...]
    state: State
    reason: str


@dataclass(frozen=True)
class DependencyDefinition:
    parent: str
    bindings: tuple[DependencyBinding, ...]
    state: State = State.UNKNOWN
    reason: str = (
        "Dependency unknown/unverified without a source-qualified reported parent. "
        "All reported choices retained; manual entry remains possible."
    )


PATH_LIMITATION = (
    "Browser selection supplies a filename, not a reusable absolute host path. "
    "Contents are not read or uploaded; compatibility is not validated. "
    "Execution requires the separate explicitly entered absolute local path below."
)


@dataclass(frozen=True)
class ArtifactView:
    key: str
    label: str
    state: State
    reason: str


@dataclass(frozen=True)
class OutputDefinition:
    field: str
    kind: str
    inspectable: bool = True
    views: tuple[ArtifactView, ...] = ()

    def expected_artifact(self, path: Path) -> ExpectedArtifact:
        """Describe a future output without opening or validating its path."""
        return ExpectedArtifact(path, self.kind)


@dataclass(frozen=True)
class WorkflowDefinition:
    key: str
    label: str
    state: State
    available: bool
    reason: str
    fields: tuple[FieldDefinition, ...] = ()
    outputs: tuple[OutputDefinition, ...] = ()
    operation: str | None = None
    reports: tuple[DiscoveryReport, ...] = ()
    readiness: Readiness = Readiness.UNKNOWN
    readiness_reason: str = (
        "Workflow runtime readiness is not evaluated by this UI. "
        "Individual capability records are separate evidence, not workflow validation."
    )


WORKFLOW_TOOLS = {
    "generate-midi": ("melisma",),
    "generate-abc": ("melisma",),
    "generate-ustx": ("melisma", "openutau"),
    "render-vocals": ("openutau", "ffprobe"),
    "generate-yue2-audio": ("yue2-native", "yue2-instrumental", "ffprobe"),
    "mix-audio": ("ffmpeg", "ffprobe"),
    "play-audio": ("ffplay", "ffprobe"),
    "full-pipeline": tuple(CONTRACTS),
}


MIDI = FieldDefinition(
    "midi", "Existing MIDI input", True, "input-path",
    "An existing compatible MIDI file: format 0, one track, positive PPQN, "
    "monophonic non-overlapping notes. This UI does not read or validate the file.",
    accept=".mid,.midi", selection="existing-file", path_limitation=PATH_LIMITATION,
)
LYRICS = FieldDefinition(
    "lyrics", "Melisma lyric input", True, "input-path",
    "Select an existing Melisma lyric file, not free-form song lyrics. "
    "This UI does not read or validate the file.",
    accept=".txt", selection="existing-file", path_limitation=PATH_LIMITATION,
)


def _evidenced(item: Capability) -> bool:
    return (
        item.verification in (Verification.SOURCE, Verification.METADATA, Verification.OBSERVED)
        and item.state in (State.SUPPORTED, State.CANDIDATE, State.CONFIGURED, State.READY)
    )


def singer_dependency(
    parents: tuple[Capability, ...], choices: tuple[Capability, ...], key: str,
) -> DependencyDefinition:
    """Only Issue #3's source-qualified singer metadata defines these relations."""
    bindings = []
    for index, parent in enumerate(parents):
        retained = []
        unknown = False
        parent_evidence = dict(parent.evidence)
        for choice_index, choice in enumerate(choices):
            evidence = dict(choice.evidence)
            relation = None
            if (
                parent.source == choice.source and _evidenced(parent) and _evidenced(choice)
                and parent.provenance == Provenance.FILESYSTEM
            ):
                if key == "renderer" and choice.provenance == Provenance.SOURCE:
                    singer_type = parent_evidence.get("singer_type")
                    renderer_type = evidence.get("singer_type")
                    if singer_type and singer_type != "unknown" and renderer_type:
                        relation = singer_type.casefold() == renderer_type.casefold()
                elif key == "phonemizer" and choice.provenance == Provenance.CONFIGURED:
                    path = parent_evidence.get("path")
                    if path and evidence.get("singer") is not None and evidence.get("file"):
                        relation = (
                            evidence["singer"] == parent.value
                            and Path(evidence["file"]).parent == Path(path)
                        )
            if relation is not False:
                retained.append(choice_index)
            unknown |= relation is None
        state = State.UNKNOWN if unknown or not choices else State.SUPPORTED
        bindings.append(DependencyBinding(
            index, tuple(retained), state,
            "Dependency unknown/unverified for some choices; those choices are retained."
            if state == State.UNKNOWN else
            "Filtered by explicit same-source singer metadata; not runtime compatibility or readiness.",
        ))
    return DependencyDefinition("singer", tuple(bindings))


def workflow_definitions(
    configuration: Configuration | None = None,
    *,
    reports: tuple[DiscoveryReport, ...] = (),
) -> tuple[WorkflowDefinition, ...]:
    """Project supplied records unchanged; never load, discover, or save."""
    if not all(kind in MelismaAdapter.operations for kind in ("abc", "ustx")):
        raise ValueError("workflow forms require Melisma ABC and USTX adapter contracts")
    configured = tuple(
        SavedSelection(source.identity, key, value)
        for source in (() if configuration is None else configuration.sources)
        if source.tool == "openutau"
        for key, value in source.configured
    )
    metadata = tuple(
        FieldDefinition(
            key, key.capitalize(), False, "text",
            "Optional configured/manual text; not evidence of installation "
            "or renderability. An empty value leaves Melisma metadata empty.",
            provenance=(Provenance.CONFIGURED, Provenance.MANUAL),
            configured=tuple(item for item in configured if item.key == key),
            capabilities=tuple(
                capability for report in reports if report.source.tool == "openutau"
                for capability in report.values(key)
            ),
        )
        for key in ("singer", "phonemizer", "renderer")
    )
    metadata = (
        metadata[0],
        *(replace(
            field, dependency=singer_dependency(
                metadata[0].capabilities, field.capabilities, field.key,
            ),
        ) for field in metadata[1:]),
    )
    forms = tuple(
        WorkflowDefinition(
            f"generate-{kind}", f"Generate {kind.upper()}", State.SUPPORTED,
            True, "ABC/USTX preview and execution require an explicitly configured "
            "local Melisma executable and entered absolute paths. Not audio readiness.",
            (
                MIDI, LYRICS,
                FieldDefinition(
                    f"{kind}_output", f"{kind.upper()} new absolute output path", True,
                    "output-path", "Explicit local destination; parent must exist. "
                    "The adapter creates the artifact exclusively after successful execution.",
                    accept=f".{kind}", selection="output-name",
                    path_limitation=(
                        "Enter an absolute local path; this is not a save dialog. "
                        "Preview does not write; Start authorizes creation without overwrite."
                    ),
                ),
                *(metadata if kind == "ustx" else ()),
            ),
            (OutputDefinition(
                f"{kind}_output", kind,
                views=(
                    ArtifactView(
                        "raw-text", "Raw ABC text", State.UNAVAILABLE,
                        "Raw artifact browsing is deferred to Issue #7; current run "
                        "artifact existence, size and validity are reported in the console.",
                    ),
                    ArtifactView(
                        "score", "Rendered musical score", State.UNAVAILABLE,
                        "Future score view; ABC rendering is not implemented "
                        "and no rendering dependency is included.",
                    ),
                ) if kind == "abc" else (),
            ),),
            operation=kind,
        )
        for kind in ("abc", "ustx")
    )
    workflows = (
        WorkflowDefinition(
            "generate-midi", "Generate MIDI", State.UNRESOLVED, False,
            "MIDI generation is unresolved/deferred. Current Melisma consumes "
            "an existing compatible MIDI file; it does not generate MIDI.",
        ),
        *forms,
        WorkflowDefinition(
            "render-vocals", "Render Vocals", State.UNAVAILABLE, False,
            "Issue #9: requires OpenUtau audio workflow integration and resource "
            "validation; the adapter alone does not establish runtime readiness.",
        ),
        WorkflowDefinition(
            "generate-yue2-audio", "Generate YuE2 Audio", State.UNAVAILABLE, False,
            "Requires separate native song and pinned instrumental target/"
            "contract selection, readiness validation, and job execution "
            "integration; this slice implements none of these.",
        ),
        WorkflowDefinition(
            "mix-audio", "Mix Audio", State.UNAVAILABLE, False,
            "Issue #9: requires FFmpeg audio workflow integration and audio input/"
            "output validation; execution is not enabled here.",
        ),
        WorkflowDefinition(
            "play-audio", "Play Audio", State.UNAVAILABLE, False,
            "Issue #9: requires UI integration with the FFplay adapter's owned playback "
            "lifecycle and audio validation.",
        ),
        WorkflowDefinition(
            "full-pipeline", "Full Pipeline", State.UNAVAILABLE, False,
            "Requires workflow graph/job coordination, validated resources and "
            "targets, and the generation/render/mix stage integrations. "
            "The existing experimental pipeline is not wired to this UI.",
        ),
    )
    return tuple(
        replace(
            workflow,
            reports=tuple(
                report for report in reports
                if report.source.tool in WORKFLOW_TOOLS[workflow.key]
            ),
        )
        for workflow in workflows
    )


def workflow_document(
    configuration: Configuration | None = None,
    *,
    reports: tuple[DiscoveryReport, ...] = (),
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "execution_enabled": configuration is not None and len([
            source for source in configuration.sources if source.tool == "melisma"
        ]) == 1 and any(
            source.tool == "melisma" and source.executable is not None
            and source.kind.value == "local"
            and source.contract in (None, "melisma-phase1")
            for source in configuration.sources
        ),
        "workflows": [
            asdict(item) for item in workflow_definitions(configuration, reports=reports)
        ],
    }
