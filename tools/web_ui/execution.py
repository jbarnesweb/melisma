"""Trusted ABC/USTX construction; browser data never supplies commands."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from pathlib import Path

from ..orchestration.adapters import MelismaAdapter
from ..orchestration.artifact_catalog import ArtifactCatalog
from ..orchestration.capabilities import SourceKind
from ..orchestration.configuration import Configuration
from ..orchestration.execution import RunRegistry, operation_document
from ..orchestration.process import Operation


class ExecutionAPI:
    def __init__(
        self, configuration: Configuration, catalog: ArtifactCatalog | None = None,
    ) -> None:
        self.configuration = configuration
        self.catalog = catalog or ArtifactCatalog()
        self.registry = RunRegistry(catalog=self.catalog)
        self.lock = threading.Lock()
        self.previews: OrderedDict[str, tuple[MelismaAdapter, Operation]] = OrderedDict()

    def construct(self, draft: dict[str, object]) -> tuple[MelismaAdapter, Operation]:
        if set(draft) != {"workflow", "inputs"}:
            raise ValueError("Expected only workflow and inputs; raw commands are prohibited")
        workflow = draft["workflow"]
        if workflow not in ("generate-abc", "generate-ustx"):
            raise ValueError("Only Generate ABC and Generate USTX are executable; audio/pipeline integration is deferred")
        mode = "abc" if workflow == "generate-abc" else "ustx"
        inputs = draft["inputs"]
        allowed = {"midi", "lyrics", f"{mode}_output"}
        if mode == "ustx":
            allowed |= {"singer", "phonemizer", "renderer"}
        if not isinstance(inputs, dict) or set(inputs) - allowed:
            raise ValueError("Unknown operation input")
        text_inputs: dict[str, str] = {}
        for key in sorted(allowed):
            value = inputs.get(key, "")
            if not isinstance(value, str) or "\0" in value or len(value) > 4096:
                raise ValueError(f"{key} must be NUL-free text of at most 4096 characters")
            text_inputs[key] = value
        paths: dict[str, Path] = {}
        for key in ("midi", "lyrics", f"{mode}_output"):
            value = text_inputs[key]
            if not value or not Path(value).is_absolute():
                raise ValueError(f"{key} requires an explicitly entered absolute local path")
            paths[key] = Path(value)
        sources = [source for source in self.configuration.sources if source.tool == "melisma"]
        source = sources[0] if len(sources) == 1 else None
        if (source is None or source.kind != SourceKind.LOCAL
            or source.executable is None or source.contract not in (None, "melisma-phase1")):
            raise ValueError("Execution requires exactly one configured local melisma-phase1 source with an explicit executable; unsaved editor changes do not apply")
        adapter = MelismaAdapter(source.executable, source.root)
        metadata = {
            key: text_inputs[key] for key in ("singer", "phonemizer", "renderer")
            if text_inputs.get(key)
        }
        operation = adapter.build(
            mode, paths["midi"], paths["lyrics"], paths[f"{mode}_output"],
            publish_stdout=True, **metadata,
        )
        return adapter, operation

    def preview(self, draft: dict[str, object]) -> dict[str, object]:
        adapter, operation = self.construct(draft)
        operation = self.catalog.bind_inputs(operation)
        token = uuid.uuid4().hex
        with self.lock:
            if len(self.previews) >= 32:
                self.previews.popitem(last=False)
            self.previews[token] = adapter, operation
        return {
            "preview_id": token, **operation_document(operation),
            "publication": "Melisma writes stdout. Complete raw bytes are spooled independently of the bounded console, then exclusively published after successful exit and complete capture; existing files are never overwritten.",
            "readiness": "unknown",
        }

    def start(self, draft: dict[str, object]) -> dict[str, object]:
        if set(draft) != {"preview_id"} or not isinstance(draft["preview_id"], str):
            raise ValueError("Start accepts only a preview_id; preview a validated operation first")
        with self.lock:
            token = draft["preview_id"]
            if token not in self.previews:
                raise KeyError("Preview expired or already submitted; preview again")
            adapter, operation = self.previews[token]
            # The registry rechecks explicit provenance and consumed revisions.
            snapshot = self.registry.start(adapter, operation)
            del self.previews[token]
            return snapshot
