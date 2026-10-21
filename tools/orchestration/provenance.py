"""Immutable production-context evidence; no currentness or lifecycle policy."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from typing import TYPE_CHECKING

from .configuration import _nonsecret

if TYPE_CHECKING:
    from .process import Operation


Scalar = str | int | float | bool | None
Settings = tuple[tuple[str, Scalar], ...]
VERSION = 1


def checked_settings(settings: Settings) -> Settings:
    if not isinstance(settings, tuple) or any(
        not isinstance(pair, tuple) or len(pair) != 2
        or not isinstance(pair[0], str) or not pair[0] or "\0" in pair[0]
        or type(pair[1]) not in (str, int, float, bool, type(None))
        or isinstance(pair[1], float) and not math.isfinite(pair[1])
        or isinstance(pair[1], str) and "\0" in pair[1]
        for pair in settings
    ):
        raise ValueError("production settings require named finite JSON scalars")
    if len(dict(settings)) != len(settings):
        raise ValueError("duplicate production setting")
    _nonsecret(settings)
    return tuple(sorted(settings))


@dataclass(frozen=True)
class ProductionConfiguration:
    """Adapter-owned allowlisted settings, optionally qualified by output type."""

    contract: str
    settings: Settings = ()
    output_settings: tuple[tuple[str, Settings], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.contract, str) or not self.contract or "\0" in self.contract:
            raise ValueError("production contract is required")
        _nonsecret(self.contract)
        object.__setattr__(self, "settings", checked_settings(self.settings))
        if not isinstance(self.output_settings, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2
            or not isinstance(pair[0], str) or not pair[0]
            for pair in self.output_settings
        ):
            raise ValueError("invalid output-qualified production settings")
        if len(dict(self.output_settings)) != len(self.output_settings):
            raise ValueError("duplicate production output type")
        qualified = tuple(sorted(
            (kind, checked_settings(settings)) for kind, settings in self.output_settings
        ))
        for _, settings in qualified:
            checked_settings((*self.settings, *settings))
        object.__setattr__(self, "output_settings", qualified)

    def for_output(self, output_type: str) -> Settings:
        return checked_settings((
            *self.settings, *dict(self.output_settings).get(output_type, ()),
        ))


@dataclass(frozen=True)
class ProductionInput:
    role: str
    type: str
    artifact_id: str | None
    revision: str

    def __post_init__(self) -> None:
        if (
            not all(isinstance(value, str) and value and "\0" not in value
                    for value in (self.role, self.type))
            or not digest_value(self.revision)
            or self.artifact_id is not None and (
                not isinstance(self.artifact_id, str)
                or re.fullmatch(r"[0-9a-f]{32}", self.artifact_id) is None
            )
        ):
            raise ValueError("invalid production input evidence")


def digest_value(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class ProductionProvenance:
    version: int
    stage: str
    tool: str
    operation: str
    contract: str
    executable: str
    source_root: str
    output_type: str
    settings: Settings
    inputs: tuple[ProductionInput, ...]
    output_revision: str | None = None
    signature: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != VERSION:
            raise ValueError("unsupported production provenance version")
        if not all(
            isinstance(value, str) and value and "\0" not in value
            for value in (self.stage, self.tool, self.operation, self.contract,
                          self.executable, self.source_root, self.output_type)
        ) or self.stage != f"{self.tool}.{self.operation}":
            raise ValueError("invalid production identity")
        if (
            not isinstance(self.inputs, tuple)
            or not all(isinstance(item, ProductionInput) for item in self.inputs)
            or len({item.role for item in self.inputs}) != len(self.inputs)
        ):
            raise ValueError("invalid or duplicate production input roles")
        if self.output_revision is not None and not digest_value(self.output_revision):
            raise ValueError("invalid original production revision")
        object.__setattr__(self, "settings", checked_settings(self.settings))
        if self.tool == "yue2-native" and "lyrics" in dict(self.settings):
            raise ValueError(
                "native YuE2 inline lyrics provenance is unsupported; "
                "unreleased development records require explicit handling"
            )
        object.__setattr__(self, "inputs", tuple(sorted(self.inputs, key=lambda item: item.role)))
        _nonsecret(self.canonical)
        canonical = json.dumps(
            self.canonical, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        object.__setattr__(self, "signature", hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    @property
    def canonical(self) -> dict[str, object]:
        """Fresh inspectable context; output bytes are evidence, not a setting."""
        return {
            "version": self.version, "stage": self.stage, "tool": self.tool,
            "operation": self.operation, "contract": self.contract,
            "executable": self.executable, "source_root": self.source_root,
            "output_type": self.output_type, "settings": dict(self.settings),
            "inputs": [asdict(item) for item in self.inputs],
        }


def production_provenance(
    operation: Operation, output_type: str,
) -> ProductionProvenance | None:
    """Use captured inputs only: never read files/current configuration at completion.

    None is explicit unknown evidence for legacy/custom/non-generation operations
    or inputs not snapshotted by bind_inputs/begin before execution.
    """
    if operation.production is None or any(item.revision is None for item in operation.inputs):
        return None
    inputs = tuple(
        ProductionInput(item.role, item.type, item.artifact_id, item.revision)
        for item in operation.inputs if item.revision is not None
    )
    return ProductionProvenance(
        VERSION, operation.stage, operation.tool, operation.name,
        operation.production.contract, str(operation.command.executable),
        str(operation.command.cwd), output_type,
        operation.production.for_output(output_type), inputs,
    )


def provenance_from_document(value: object) -> ProductionProvenance | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(ProductionProvenance)}:
        raise ValueError("invalid production provenance document")
    if not isinstance(value["inputs"], list) or not isinstance(value["settings"], list):
        raise ValueError("invalid production provenance collections")
    if any(type(pair) is not list or len(pair) != 2 for pair in value["settings"]):
        raise ValueError("production settings require JSON two-element array pairs")
    evidence = ProductionProvenance(**{
        **{key: item for key, item in value.items() if key != "signature"},
        "inputs": tuple(ProductionInput(**item) for item in value["inputs"]),
        "settings": tuple(tuple(pair) for pair in value["settings"]),
    })
    if evidence.signature != value["signature"]:
        raise ValueError("production provenance signature mismatch")
    return evidence


def same_production_context(
    recorded: ProductionProvenance | None, candidate: ProductionProvenance | None,
) -> bool | None:
    """Context equality only; unknown evidence is not a match or a stale decision."""
    if recorded is None or candidate is None:
        return None
    return recorded.signature == candidate.signature
