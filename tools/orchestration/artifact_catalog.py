"""Local artifact history around, not in place of, execution validation records."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .artifacts import existing_input, inspect_artifact, new_output
from .process import (
    ArtifactRecord, ExpectedArtifact, Operation, OperationInput, OperationResult,
    ProcessState, utc_now,
)
from .provenance import (
    ProductionProvenance, production_provenance, provenance_from_document,
    same_production_context,
)


SCHEMA_VERSION = 1
TEXT_LIMIT = 64 * 1024
ARTIFACT_TYPES = (
    "midi", "abc", "ustx", "lyrics", "ir", "audio", "vocal-audio",
    "yue2-audio", "instrumental-audio", "mixed-audio",
    "configuration", "diagnostic", "text", "file",
)
IMPORT_TYPES = ("midi", "abc", "ustx", "lyrics", "audio", "configuration", "diagnostic", "text")
TEXT_TYPES = ("abc", "ustx", "lyrics", "ir", "configuration", "diagnostic", "text")
EDITABLE_TYPES = ("abc", "ustx", "lyrics", "text")


class CatalogError(RuntimeError):
    pass


def default_history_path() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return root / "melisma" / "artifacts.json"


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_new_file(path: Path, contents: bytes) -> None:
    """Publish complete private content without replacing even a racing symlink."""
    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ValueError(f"refusing occupied destination: {path}") from error
        published = True
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                outcome = f"File published to {path}" if published else f"Destination {path} not published"
                raise CatalogError(f"{outcome}; private temporary cleanup failed: {error}") from error
    try:
        fsync_directory(path.parent)
    except OSError as error:
        raise CatalogError(
            f"File published to {path}, but containing-directory fsync failed: {error}; "
            "durability is unconfirmed and the file was not rolled back"
        ) from error


def input_document(item: OperationInput) -> dict[str, object]:
    return {**asdict(item), "path": str(item.path)}


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate history field")
        value[key] = item
    return value


def valid_source(source: ArtifactSource) -> bool:
    return (
        all(isinstance(value, str) and value and "\0" not in value
            for value in asdict(source).values())
        and Path(source.path).is_absolute()
        and re.fullmatch(r"[0-9a-f]{32}", source.artifact_id) is not None
        and re.fullmatch(r"[0-9a-f]{64}", source.revision) is not None
    )


@dataclass(frozen=True)
class ArtifactSource:
    role: str
    path: str
    artifact_id: str
    revision: str


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    path: str
    type: str
    kind: str
    allow_empty: bool
    origin: str
    session_id: str
    created_at: str
    checked_at: str
    revision: str | None
    exists: bool
    size: int | None
    valid: bool | None
    error: str | None
    production_status: str
    producer_run: str | None = None
    stage: str | None = None
    sources: tuple[ArtifactSource, ...] = ()
    derived_from: str | None = None
    backup_path: str | None = None
    edited_at: str | None = None
    provenance: ProductionProvenance | None = None


@dataclass(frozen=True)
class ArtifactProduction:
    artifact_id: str
    provenance: ProductionProvenance

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", self.artifact_id) is None
            or not isinstance(self.provenance, ProductionProvenance)
            or self.provenance.output_revision is None
        ):
            raise ValueError("invalid historical artifact production")

    @classmethod
    def from_document(cls, value: object) -> ArtifactProduction:
        if not isinstance(value, dict) or set(value) != {"artifact_id", "provenance"}:
            raise ValueError("invalid historical artifact production document")
        evidence = provenance_from_document(value["provenance"])
        if evidence is None:
            raise ValueError("historical artifact production requires evidence")
        return cls(value["artifact_id"], evidence)


@dataclass(frozen=True)
class ConsumedRun:
    id: str
    session_id: str
    stage: str
    success: bool
    sources: tuple[ArtifactSource, ...]
    productions: tuple[ArtifactProduction, ...] = ()


def unique_paths(entries: dict[str, CatalogEntry]) -> dict[Path, CatalogEntry]:
    paths: dict[Path, CatalogEntry] = {}
    for entry in entries.values():
        path = Path(entry.path).resolve()
        if path in paths:
            raise CatalogError(
                f"Duplicate canonical artifact path {path}: {paths[path].id} and {entry.id}; "
                "history must be corrected explicitly, not merged"
            )
        paths[path] = entry
    return paths


def semantic_type(operation: Operation, artifact: ArtifactRecord) -> str:
    return semantic_output_type(operation.stage, artifact.expected)


def semantic_output_type(stage: str, artifact: ExpectedArtifact) -> str:
    """Classify a declared output without inspecting its contents or existence."""
    kind, path = artifact.kind, artifact.path
    if kind == "audio" or path.suffix.lower() in (".wav", ".flac", ".mp3", ".ogg"):
        return {
            "openutau.render": "vocal-audio", "ffmpeg.mix": "mixed-audio",
            "yue2-native.generate": "yue2-audio",
            "yue2-instrumental.generate": "instrumental-audio",
        }.get(stage, "audio")
    if kind in ("abc", "ustx", "ir"):
        return kind
    if path.name == "lyrics.txt":
        return "lyrics"
    if path.name in ("result.json", "summary.json", "repair-report.json", "score-check.json"):
        return "diagnostic"
    return {
        ".abc": "abc", ".ustx": "ustx", ".json": "configuration",
        ".jsonl": "ir", ".txt": "text",
    }.get(path.suffix.lower(), "file")


def compatible_artifact_type(entry: CatalogEntry, accepted_types: tuple[str, ...]) -> bool:
    """Semantic selection eligibility only, without observing or changing files."""
    return entry.type in accepted_types and (
        entry.type != "instrumental-audio"
        or entry.stage is not None and semantic_output_type(
            entry.stage, ExpectedArtifact(Path(entry.path), entry.kind),
        ) == entry.type
    )


class ArtifactCatalog:
    """One server's locked catalog; persisted atomically, with media left in place."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path.absolute() if path is not None else None
        self.session_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.entries: dict[str, CatalogEntry] = {}
        self.runs: dict[str, ConsumedRun] = {}
        self.active_paths: dict[Path, int] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        try:
            with self.path.open(encoding="utf-8") as stream:
                data = json.load(stream, object_pairs_hook=unique_object)
        except FileNotFoundError:
            if self.path.is_symlink():
                raise CatalogError(f"Artifact history is a dangling symlink: {self.path}")
            return
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            raise CatalogError(f"Cannot read artifact history {self.path}: {error}") from error
        try:
            if not isinstance(data, dict) or set(data) != {"schema_version", "artifacts", "runs"}:
                raise ValueError("invalid history document")
            if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
                raise ValueError(f"unsupported artifact history version: {data['schema_version']!r}")
            if not isinstance(data["artifacts"], list) or not isinstance(data["runs"], list):
                raise ValueError("history artifacts and runs must be arrays")
            for item in data["artifacts"]:
                entry = CatalogEntry(**{
                    **item, "sources": tuple(ArtifactSource(**source) for source in item["sources"]),
                    "provenance": provenance_from_document(item.get("provenance")),
                })
                if (
                    entry.type not in ARTIFACT_TYPES or not Path(entry.path).is_absolute()
                    or entry.kind not in ("file", "audio", "abc", "ustx", "ir", "json")
                    or entry.id in self.entries or not entry.id
                    or entry.origin not in ("imported", "generated", "edited")
                    or entry.production_status not in ("imported", "successful", "partial", "missing", "edited")
                    or type(entry.allow_empty) is not bool or type(entry.exists) is not bool
                    or (entry.valid is not None and type(entry.valid) is not bool)
                    or (entry.size is not None and (type(entry.size) is not int or entry.size < 0))
                    or not all(isinstance(value, str) and value and "\0" not in value for value in (
                        entry.id, entry.path, entry.session_id, entry.created_at, entry.checked_at,
                    ))
                    or re.fullmatch(r"[0-9a-f]{32}", entry.id) is None
                    or (entry.revision is not None and (
                        not isinstance(entry.revision, str)
                        or re.fullmatch(r"[0-9a-f]{64}", entry.revision) is None
                    ))
                    or not all(value is None or isinstance(value, str) for value in (
                        entry.error, entry.producer_run, entry.stage, entry.derived_from,
                        entry.backup_path, entry.edited_at,
                    ))
                    or not all(valid_source(source) for source in entry.sources)
                    or entry.provenance is not None and (
                        entry.origin == "imported" or entry.producer_run is None
                        or entry.provenance.stage != entry.stage
                        or entry.provenance.output_type != entry.type
                        or entry.provenance.output_revision is None
                        or entry.production_status not in ("successful", "edited")
                    )
                ):
                    raise ValueError("invalid artifact record")
                self.entries[entry.id] = entry
            unique_paths(self.entries)
            for item in data["runs"]:
                run = ConsumedRun(**{
                    **item, "sources": tuple(ArtifactSource(**source) for source in item["sources"]),
                    "productions": tuple(
                        ArtifactProduction.from_document(production)
                        for production in item.get("productions", [])
                    ),
                })
                if (
                    run.id in self.runs or type(run.success) is not bool
                    or not all(isinstance(value, str) and value for value in (
                        run.id, run.session_id, run.stage,
                    ))
                    or not all(valid_source(source) for source in run.sources)
                    or any(
                        not run.success or production.artifact_id not in self.entries
                        or production.provenance.stage != run.stage
                        for production in run.productions
                    )
                ):
                    raise ValueError("invalid consumer record")
                self.runs[run.id] = run
            if any(
                source.artifact_id not in self.entries
                for sources in (
                    *[entry.sources for entry in self.entries.values()],
                    *[run.sources for run in self.runs.values()],
                ) for source in sources
            ):
                raise ValueError("history references an unknown source artifact")
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise CatalogError(f"Invalid artifact history {self.path}: {error}") from error

    def _commit(
        self, entries: dict[str, CatalogEntry], runs: dict[str, ConsumedRun],
    ) -> None:
        unique_paths(entries)
        if self.path is not None:
            temporary: Path | None = None
            published = False
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                contents = json.dumps({
                    "schema_version": SCHEMA_VERSION,
                    "artifacts": [asdict(entry) for entry in entries.values()],
                    "runs": [asdict(run) for run in runs.values()],
                }, indent=2, allow_nan=False) + "\n"
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                published = True
                self.entries, self.runs = entries, runs
                fsync_directory(self.path.parent)
            except OSError as error:
                if published:
                    raise CatalogError(
                        f"Artifact history published to {self.path}, but containing-directory fsync "
                        f"failed: {error}; durability is unconfirmed and history was not rolled back"
                    ) from error
                raise CatalogError(f"Cannot save artifact history {self.path}: {error}") from error
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        self.entries, self.runs = entries, runs

    def _at_path(
        self, path: Path, entries: dict[str, CatalogEntry] | None = None,
    ) -> CatalogEntry | None:
        entries = self.entries if entries is None else entries
        return unique_paths(entries).get(path.resolve())

    def get(self, artifact_id: str) -> CatalogEntry:
        with self.lock:
            return self.entries[artifact_id]

    def stage_artifacts(self, stage: str) -> tuple[CatalogEntry, ...]:
        """All tracked outputs for a producer stage, including partial/missing ones."""
        with self.lock:
            return tuple(entry for entry in self.entries.values() if entry.stage == stage)

    def _entry(
        self, path: Path, type_: str, *, kind: str = "file", allow_empty: bool = False,
        origin: str = "imported", status: str = "imported",
    ) -> CatalogEntry:
        checked = inspect_artifact(ExpectedArtifact(path, kind, allow_empty))
        revision = fingerprint(path) if checked.exists else None
        now = utc_now()
        return CatalogEntry(
            uuid.uuid4().hex, str(path), type_, kind, allow_empty,
            origin, self.session_id, now, now, revision, checked.exists, checked.size,
            None if kind == "audio" and checked.valid else checked.valid,
            checked.error, status,
        )

    def register_input(self, path: Path, type_: str) -> CatalogEntry:
        if type_ not in IMPORT_TYPES:
            raise ValueError("Unsupported imported artifact type")
        if not path.is_absolute():
            raise ValueError("Artifact registration requires an absolute local path")
        path = existing_input(path, path.parent)
        with self.lock:
            if path in self.active_paths:
                raise ValueError("Artifact is in use by an active run")
            previous = self._at_path(path)
            if previous is not None:
                if previous.type != type_:
                    raise ValueError(f"Path is already registered as {previous.type}")
                return self._refresh(previous)
            entry = self._entry(path, type_, kind="audio" if type_ == "audio" else "file")
            self._commit({**self.entries, entry.id: entry}, self.runs)
            return entry

    def bind_inputs(self, operation: Operation) -> Operation:
        """Read-only preview snapshot; importing is a separate deliberate action."""
        with self.lock:
            inputs = []
            for source in operation.inputs:
                path = existing_input(source.path, operation.command.cwd)
                known = self._at_path(path)
                inputs.append(replace(
                    source, path=path, artifact_id=known.id if known else None,
                    revision=fingerprint(path),
                ))
            return replace(operation, inputs=tuple(inputs))

    def begin(self, operation: Operation) -> Operation:
        with self.lock:
            if any(item.path in self.active_paths for item in operation.expected):
                raise ValueError("Output is in use by an active run")
            entries = dict(self.entries)
            inputs = []
            for source in operation.inputs:
                path = existing_input(source.path, operation.command.cwd)
                revision = fingerprint(path)
                if source.revision is not None and source.revision != revision:
                    raise ValueError(f"Input changed since preview: {source.role}; preview again")
                previous = self._at_path(path, entries)
                if previous is None:
                    type_ = source.type
                    if type_ not in IMPORT_TYPES:
                        type_ = "audio" if type_.endswith("audio") else "file"
                    previous = self._entry(path, type_, kind="audio" if type_ == "audio" else "file")
                else:
                    previous = self._observed(previous)
                entries[previous.id] = previous
                inputs.append(replace(source, artifact_id=previous.id, revision=revision))
            self._commit(entries, self.runs)
            for path in (*[source.path for source in inputs],
                         *[item.path for item in operation.expected]):
                self.active_paths[path] = self.active_paths.get(path, 0) + 1
            return replace(operation, inputs=tuple(inputs))

    def end(self, operation: Operation) -> None:
        with self.lock:
            for path in (*[source.path for source in operation.inputs],
                         *[item.path for item in operation.expected]):
                count = self.active_paths.get(path, 0)
                if count > 1:
                    self.active_paths[path] = count - 1
                else:
                    self.active_paths.pop(path, None)

    def register_result(self, result: OperationResult, run_id: str) -> None:
        """Also callable by synchronous adapter clients after begin()."""
        with self.lock:
            sources = tuple(
                ArtifactSource(item.role, str(item.path), item.artifact_id, item.revision)
                for item in result.operation.inputs
                if item.artifact_id is not None and item.revision is not None
            )
            entries = dict(self.entries)
            run = ConsumedRun(
                run_id, self.session_id,
                result.operation.stage, result.success, sources,
            )
            # A pre-execution rejection may inspect someone else's existing output.
            produced = result.process.state not in (
                ProcessState.REJECTED, ProcessState.START_FAILED, ProcessState.CANCELLED,
            ) and result.process.pid is not None and result.process.publication_error is None
            productions = []
            if produced:
                records: dict[Path, ArtifactRecord] = {}
                for record in result.artifacts:
                    path = record.expected.path.resolve()
                    previous = records.get(path)
                    if previous is not None:
                        record = replace(
                            record,
                            expected=previous.expected if previous.expected.kind != "file" else record.expected,
                            valid=previous.valid and record.valid,
                            error="; ".join(dict.fromkeys(
                                error for error in (previous.error, record.error) if error
                            )) or None,
                        )
                    records[path] = record
                for path, record in records.items():
                    previous = self._at_path(path)
                    entry = self._entry(
                        path, semantic_type(result.operation, record),
                        kind=record.expected.kind, allow_empty=record.expected.allow_empty,
                        origin="generated",
                        status="successful" if result.success else "partial" if record.exists else "missing",
                    )
                    entry = replace(
                        entry, valid=record.valid and entry.valid is not False,
                        error=record.error or entry.error, producer_run=run_id,
                        stage=run.stage, sources=sources,
                        id=previous.id if previous else entry.id,
                        created_at=previous.created_at if previous else entry.created_at,
                    )
                    if result.success and entry.revision is not None:
                        evidence = production_provenance(result.operation, entry.type)
                        if evidence is not None:
                            historical = replace(
                                evidence, output_revision=entry.revision,
                            )
                            entry = replace(entry, provenance=historical)
                            productions.append(ArtifactProduction(entry.id, historical))
                    entries[entry.id] = entry
            self._commit(entries, {
                **self.runs, run.id: replace(run, productions=tuple(productions)),
            })

    def register_diagnostics(self, run_id: str, document: dict[str, object]) -> None:
        if self.path is None:
            return
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None:
            raise ValueError("Diagnostic run ID must be a safe filename")
        with self.lock:
            directory = self.path.parent / f"{self.path.stem}-files" / self.session_id
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = directory / f"{run_id}.json"
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(document, indent=2, allow_nan=False) + "\n")
            except OSError as error:
                raise CatalogError(f"Cannot save run diagnostics: {error}") from error
            entry = self._entry(path, "diagnostic", kind="json", origin="generated", status="successful")
            entry = replace(entry, producer_run=run_id, stage="execution.diagnostics")
            self._commit({**self.entries, entry.id: entry}, self.runs)

    def _observed(self, entry: CatalogEntry, *, validate: bool = True) -> CatalogEntry:
        path = Path(entry.path)
        try:
            check = inspect_artifact(ExpectedArtifact(
                path, entry.kind if validate else "file", entry.allow_empty,
            ))
            revision = fingerprint(path) if check.exists else None
            unchanged = revision is not None and revision == entry.revision
            valid = entry.valid if unchanged else check.valid
            if check.valid and not unchanged and (entry.kind == "audio" or not validate):
                valid = None
            return replace(
                entry, checked_at=utc_now(), revision=revision, exists=check.exists,
                size=check.size,
                valid=valid,
                error=entry.error if unchanged else check.error,
            )
        except OSError as error:
            return replace(entry, checked_at=utc_now(), valid=False, error=str(error))

    def _refresh(self, entry: CatalogEntry) -> CatalogEntry:
        updated = self._observed(entry)
        self._commit({**self.entries, entry.id: updated}, self.runs)
        return updated

    def refresh(self) -> None:
        with self.lock:
            self._commit({
                key: self._observed(entry) for key, entry in self.entries.items()
            }, self.runs)

    def document(self) -> dict[str, object]:
        with self.lock:
            consumers = []
            for run in self.runs.values():
                older = self.changed_inputs(run.id)
                consumers.append({**asdict(run), "stale": bool(older), "changed_inputs": older})
            return {
                "schema_version": SCHEMA_VERSION, "session_id": self.session_id,
                "history_path": str(self.path) if self.path else None,
                "artifacts": [asdict(entry) for entry in reversed(tuple(self.entries.values()))],
                "consumers": consumers,
            }

    def changed_inputs(self, run_id: str) -> list[str]:
        with self.lock:
            run = self.runs.get(run_id)
            return [] if run is None else [
                source.artifact_id for source in run.sources
                if source.artifact_id in self.entries
                and self.entries[source.artifact_id].revision != source.revision
            ]

    def compatible(self, accepted_types: tuple[str, ...]) -> list[dict[str, object]]:
        with self.lock:
            return [
                asdict(entry) for entry in reversed(tuple(self.entries.values()))
                if compatible_artifact_type(entry, accepted_types)
                and entry.exists and entry.valid is not False
                and entry.production_status in ("imported", "successful", "edited")
                and Path(entry.path) not in self.active_paths
            ]

    def read_text(self, artifact_id: str) -> dict[str, object]:
        with self.lock:
            entry = self._observed(self.get(artifact_id), validate=False)
            if entry.type not in TEXT_TYPES:
                raise ValueError("Artifact is not a supported text format")
            path = Path(entry.path)
            with path.open("rb") as stream:
                data = stream.read(TEXT_LIMIT + 1)
            truncated = len(data) > TEXT_LIMIT
            data = data[:TEXT_LIMIT]
            if b"\0" in data:
                raise ValueError("Binary data cannot be inspected as text")
            # A preview can end inside a UTF-8 character; never make it editable.
            from codecs import getincrementaldecoder
            text = getincrementaldecoder("utf-8")().decode(data, final=not truncated)
            return {
                "artifact": asdict(entry), "text": text, "truncated": truncated,
                "limit": TEXT_LIMIT,
                "editable": not truncated and entry.type in EDITABLE_TYPES
                and path not in self.active_paths and not path.is_symlink(),
            }

    def save_text(
        self, artifact_id: str, revision: str, text: str, destination: Path, *,
        replace_existing: bool = False,
    ) -> CatalogEntry:
        if (
            not isinstance(text, str) or "\0" in text
            or len(text.encode("utf-8")) > TEXT_LIMIT
            or not destination.is_absolute()
        ):
            raise ValueError("Save requires bounded UTF-8 text and an absolute destination")
        with self.lock:
            view = self.read_text(artifact_id)
            entry = self._observed(self.get(artifact_id))
            if not view["editable"] or entry.revision != revision:
                raise ValueError("Artifact changed, is in use, read-only, or truncated; reopen it")
            original = Path(entry.path)
            target = destination.parent.resolve() / destination.name
            if target in self.active_paths:
                raise ValueError("Destination is in use by an active run")
            if replace_existing:
                if target != original or original.is_symlink():
                    raise ValueError("Replacement is only allowed for this original regular file")
                backup = original.with_name(f"{original.name}.backup-{uuid.uuid4().hex}")
                mode = stat.S_IMODE(original.stat().st_mode) & 0o777
                data = original.read_bytes()
                if hashlib.sha256(data).hexdigest() != revision:
                    raise ValueError("Artifact changed; reopen it")
                try:
                    publish_new_file(backup, data)
                except (OSError, ValueError, CatalogError) as error:
                    raise CatalogError(
                        f"Original {original} was not replaced; backup preparation failed: {error}"
                    ) from error
                temporary: Path | None = None
                published = False
                try:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(text.encode("utf-8"))
                        stream.flush()
                        os.fchmod(stream.fileno(), mode)
                        os.fsync(stream.fileno())
                    if original.is_symlink() or fingerprint(original) != revision:
                        raise ValueError("Artifact changed during save; original not replaced")
                    os.replace(temporary, target)
                    published = True
                    fsync_directory(target.parent)
                except OSError as error:
                    if published:
                        raise CatalogError(
                            f"Text replaced at {target}, backup preserved at {backup}, but "
                            f"containing-directory fsync failed before history persistence: {error}; "
                            "durability is unconfirmed and the replacement was not rolled back"
                        ) from error
                    raise
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                edited = self._observed(replace(
                    entry, origin="edited", production_status="edited",
                    backup_path=str(backup), edited_at=utc_now(),
                ))
            else:
                known = self._at_path(target)
                if known is not None:
                    raise ValueError(
                        f"Save As destination is already registered as artifact {known.id}: {target}; "
                        "choose a new path even if its backing file is missing"
                    )
                target = new_output(target, target.parent)
                publish_new_file(target, text.encode("utf-8"))
                try:
                    edited = self._entry(target, entry.type, kind=entry.kind, origin="edited", status="edited")
                except OSError as error:
                    raise CatalogError(
                        f"Text saved to {target}, but artifact inspection failed before history "
                        f"persistence: {error}; the file was not rolled back"
                    ) from error
                edited = replace(
                    edited, derived_from=entry.id,
                    sources=(ArtifactSource("edited-from", entry.path, entry.id, revision),),
                )
            try:
                self._commit({**self.entries, edited.id: edited}, self.runs)
            except CatalogError as error:
                raise CatalogError(f"Text saved to {target}, but history update failed: {error}") from error
            return edited
