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
        match = next((
            item for item in current if item.value == previous.value
            and item.state != State.UNKNOWN
            and item.verification in (
                Verification.SOURCE, Verification.METADATA, Verification.OBSERVED,
            )
            and item.provenance not in (
                Provenance.CONFIGURED, Provenance.REFERENCE, Provenance.PREVIOUS,
                Provenance.PROJECT, Provenance.MANUAL,
            )
        ), None)
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
