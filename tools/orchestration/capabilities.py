"""Small immutable discovery/default records; no configuration persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class State(str, Enum):
    SUPPORTED = "supported"
    CONFIGURED = "configured"
    CANDIDATE = "candidate"
    READY = "ready"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"
    UNRESOLVED = "unresolved"


class Readiness(str, Enum):
    READY = "ready"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    NOT_APPLICABLE = "not-applicable"


class Verification(str, Enum):
    UNVERIFIED = "unverified"
    SOURCE = "source"
    METADATA = "metadata"
    OBSERVED = "observed"
    RECORDED = "recorded"


class Provenance(str, Enum):
    SOURCE = "source"
    TOOL_DEFAULT = "tool-default"
    ENVIRONMENT = "environment"
    FILESYSTEM = "filesystem"
    CONFIGURED = "configured"
    REFERENCE = "reference"
    PREVIOUS = "previous-selection"
    PROJECT = "project-default"
    MANUAL = "manual"


class SourceKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True)
class SourceIdentity:
    tool: str
    location: str
    contract: str
    kind: SourceKind = SourceKind.LOCAL


@dataclass(frozen=True)
class Capability:
    source: SourceIdentity
    key: str
    value: str | None
    provenance: Provenance
    verification: Verification
    state: State
    readiness: Readiness = Readiness.UNKNOWN
    reason: str | None = None
    default: bool = False
    evidence: tuple[tuple[str, str], ...] = ()
    # Retains the evidence's origin when a previous selection is revalidated.
    origin: Provenance | None = None

    def __post_init__(self) -> None:
        for value, kind in (
            (self.provenance, Provenance), (self.verification, Verification),
            (self.state, State), (self.readiness, Readiness),
        ):
            if not isinstance(value, kind):
                raise ValueError(f"expected constrained {kind.__name__}")
        if self.state == State.READY or self.readiness == Readiness.READY:
            if (
                self.state != State.READY or self.readiness != Readiness.READY
                or self.verification != Verification.OBSERVED
            ):
                raise ValueError("ready requires current observed evidence")
        if self.state in (
            State.UNKNOWN, State.UNAVAILABLE, State.UNAUTHORIZED, State.ERROR,
            State.UNRESOLVED,
        ) and not self.reason:
            raise ValueError("unknown and failure states require a reason")
        if self.readiness == Readiness.UNKNOWN and not self.reason:
            object.__setattr__(self, "reason", "readiness not validated")


@dataclass(frozen=True)
class DiscoveryReport:
    source: SourceIdentity
    capabilities: tuple[Capability, ...]

    def values(self, key: str) -> tuple[Capability, ...]:
        return tuple(item for item in self.capabilities if item.key == key)

    @property
    def issues(self) -> tuple[Capability, ...]:
        return tuple(item for item in self.capabilities if item.reason)


REJECTED = frozenset((
    State.UNAVAILABLE, State.UNAUTHORIZED, State.ERROR, State.UNRESOLVED,
))


def selection_match(
    report: DiscoveryReport, selection: Capability,
) -> Capability | None:
    """Match current evidence, not a configured/reference echo of the selection."""
    if selection.source != report.source:
        return None
    return next((
        item for item in report.values(selection.key)
        if item.value == selection.value and item.value is not None
        and item.state not in REJECTED | {State.UNKNOWN}
        and item.readiness not in (Readiness.UNAVAILABLE, Readiness.UNAUTHORIZED)
        and item.verification in (
            Verification.SOURCE, Verification.METADATA, Verification.OBSERVED,
        )
        and item.provenance not in (
            Provenance.CONFIGURED, Provenance.REFERENCE, Provenance.PREVIOUS,
            Provenance.PROJECT, Provenance.MANUAL,
        )
    ), None)


def revalidate_selection(
    report: DiscoveryReport, selection: Capability,
) -> Capability:
    """Keep the saved value/origin even when it is no longer a valid selection."""
    match = selection_match(report, selection)
    if match is not None:
        return replace(
            match, provenance=selection.provenance,
            origin=selection.origin or selection.provenance,
        )
    failures = tuple(
        item for item in report.capabilities
        if item.state in REJECTED | {State.UNKNOWN}
        and (item.key == selection.key and item.value in (None, selection.value)
             or selection.key == "singer" and item.key == "installed-singers"
             or item.key in ("discovery", "source", "package", "contract"))
    )
    failure = next(
        (item for item in failures if item.state == State.UNAUTHORIZED),
        next(iter(failures), None),
    )
    finite_inventory = selection.key in (
        "renderer", "cot", "backend", "quantization", "stage", "composer",
        "overlap-policy", "format", "operation", "device",
    ) and any(
        item for item in report.values(selection.key)
        if item.provenance == Provenance.SOURCE
        and item.verification in (Verification.SOURCE, Verification.METADATA)
        and item.state not in REJECTED | {State.UNKNOWN}
    )
    enumerated_singers = selection.key == "singer" and any(
        item.state == State.CANDIDATE for item in report.values("installed-singers")
    )
    state = State.UNKNOWN
    readiness = Readiness.UNKNOWN
    reason = "saved selection has no current validating evidence"
    if selection.source != report.source:
        reason = "saved source identity does not match the current source"
    elif failure is not None:
        state, readiness = failure.state, failure.readiness
        reason = failure.reason or reason
    elif finite_inventory or enumerated_singers:
        state, readiness = State.UNAVAILABLE, Readiness.UNAVAILABLE
        reason = "saved value is absent from the current capability inventory"
    return replace(
        selection, state=state, readiness=readiness,
        verification=Verification.UNVERIFIED, reason=reason, evidence=(),
        origin=selection.origin or selection.provenance,
    )


def resolve_default(
    report: DiscoveryReport, key: str, *,
    previous: Capability | None = None,
    project: Capability | None = None,
    manual: Capability | None = None,
) -> Capability:
    """Selection validity is not render readiness; uncertainty is preserved."""
    current = tuple(
        item for item in report.values(key)
        if item.value is not None and item.state not in REJECTED
        and item.readiness not in (Readiness.UNAVAILABLE, Readiness.UNAUTHORIZED)
    )
    for provenance in (Provenance.TOOL_DEFAULT, Provenance.ENVIRONMENT, Provenance.FILESYSTEM):
        match = next((
            item for item in current if item.provenance == provenance
            and (item.default or provenance != Provenance.TOOL_DEFAULT)
        ), None)
        if match is not None:
            return match
    if previous is not None and previous.source == report.source and previous.key == key:
        match = selection_match(report, previous)
        if match is not None:
            return replace(
                match, provenance=Provenance.PREVIOUS,
                origin=match.origin or match.provenance, default=True,
            )
    for item, provenance in ((project, Provenance.PROJECT), (manual, Provenance.MANUAL)):
        if item is not None:
            if item.key != key or item.source != report.source or item.provenance != provenance:
                raise ValueError("fallback must identify its actual source/key/provenance")
            if (
                item.state not in REJECTED and item.value is not None
                and item.readiness not in (Readiness.UNAVAILABLE, Readiness.UNAUTHORIZED)
            ):
                # A preset/manual string is not evidence of installation or readiness.
                return replace(
                    item, state=State.CONFIGURED, readiness=Readiness.UNKNOWN,
                    verification=Verification.UNVERIFIED, default=True,
                )
    return Capability(
        report.source, key, None, Provenance.MANUAL, Verification.UNVERIFIED,
        State.UNKNOWN, reason="no valid default; manual entry required",
    )
