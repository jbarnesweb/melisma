"""Issue #4 model projection and non-persisting editor preview."""

from dataclasses import asdict

from ..orchestration.capabilities import Provenance
from ..orchestration.configuration import (
    Configuration, configuration_structure, configuration_value,
)
from .workflows import FieldDefinition


def configuration_document(configuration: Configuration) -> dict[str, object]:
    def project(structure: dict[str, object], key: str) -> dict[str, object]:
        kind = structure["kind"]
        result = dict(structure)
        result["field"] = asdict(FieldDefinition(
            key, key.replace("_", " ").capitalize(),
            "default" not in structure and not structure.get("nullable", False),
            kind,
            "Configured selection only; state: configured; readiness: unknown. "
            "No availability, authorization expansion, remote health, or runtime validation."
            + (
                f" Value must be greater than {structure['exclusive_minimum']}."
                if "exclusive_minimum" in structure else ""
            )
            + (
                " Complete numeric range is not established; unspecified bounds are not constrained."
                if kind == "number" and not {"minimum", "maximum"} <= structure.keys() else ""
            ),
            provenance=(Provenance.CONFIGURED, Provenance.MANUAL),
            minimum=structure.get("minimum"), maximum=structure.get("maximum"),
            exclusive_minimum=structure.get("exclusive_minimum"),
            step=structure.get("step"),
        ))
        if kind == "object":
            result["properties"] = {
                name: project(child, name) for name, child in structure["properties"].items()
            }
        if kind == "array":
            result["items"] = [
                project(child, f"{key} item") for child in structure["items"]
            ]
        return result

    return {
        "schema": project(configuration_structure(), "configuration"),
        "value": configuration_value(configuration),
        "saved": False,
        "reason": (
            "Unsaved editor/preview. Validation uses Issue #4's pure model boundary; "
            "it does not scan paths, contact targets, or change active configuration. "
            "Persistence integration must explicitly call Issue #4 save_configuration "
            "with an operator-selected destination; no UI save endpoint is provided."
        ),
    }
