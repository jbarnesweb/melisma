"""Loopback workflow execution, read-only capability refresh and unsaved config."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from ..orchestration.capabilities import (
    Capability, DiscoveryReport, Provenance, Readiness, State, Verification,
)
from ..orchestration.artifact_catalog import (
    ArtifactCatalog, CatalogError, IMPORT_TYPES, default_history_path,
)
from ..orchestration.configuration import (
    Configuration, ConfigurationError, configuration_from_value,
)
from ..orchestration.discovery import DiscoveryService, DiscoverySource
from ..orchestration.execution import RegistryFull
from .configuration import configuration_document
from .execution import ExecutionAPI
from .workflows import accepted_artifact_types, workflow_document


STATIC = Path(__file__).parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
RUN_ROUTE = re.compile(r"/api/runs/([0-9a-f]{32})(/stop)?\Z")
ARTIFACT_ROUTE = re.compile(r"/api/artifacts/([0-9a-f]{32})/(text|save)\Z")

Refresh = Callable[[tuple[DiscoverySource, ...]], tuple[DiscoveryReport, ...]]


def refresh_capabilities(sources: tuple[DiscoverySource, ...]) -> tuple[DiscoveryReport, ...]:
    """Deliberate read-only boundary; no executable probes, even help/version."""
    return DiscoveryService(probe=None).refresh(sources)


def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class WorkflowHandler(BaseHTTPRequestHandler):
    server: WorkflowServer

    def do_GET(self) -> None:
        self._read()

    def do_HEAD(self) -> None:
        self._read(head=True)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        # Never log submitted bodies, URLs, configuration values, or exception text.
        self.log_message("HTTP response %s", code)

    def _read(self, *, head: bool = False) -> None:
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            self._respond(403, b'{"error":"Exact loopback Host required"}', head=head)
            return
        if self.path == "/api/workflows":
            body = self.server.workflow_json
            content_type = "application/json; charset=utf-8"
        elif self.path == "/api/ui":
            body = json.dumps({
                "csrf": self.server.csrf,
                "configuration": self.server.configuration_preview,
                "refresh": self.server.refresh_status,
            }).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        elif self.path == "/api/runs":
            self._respond(
                200, json.dumps({"runs": self.server.execution.registry.list()}).encode(),
                head=head,
            )
            return
        elif self.path == "/api/artifacts":
            self._respond(
                200, json.dumps({
                    **self.server.catalog.document(), "import_types": IMPORT_TYPES,
                }).encode(), head=head,
            )
            return
        elif (artifact_route := ARTIFACT_ROUTE.fullmatch(self.path)) and artifact_route[2] == "text":
            try:
                value = self.server.catalog.read_text(artifact_route[1])
            except KeyError:
                self._respond(404, b'{"error":"Unknown artifact"}', head=head)
                return
            except (OSError, ValueError, UnicodeError) as error:
                self._respond(422, json.dumps({"error": str(error)}).encode(), head=head)
                return
            self._respond(200, json.dumps(value).encode(), head=head)
            return
        elif (match := RUN_ROUTE.fullmatch(self.path)) and match[2] is None:
            try:
                value = self.server.execution.registry.get(match[1]).snapshot()
            except KeyError:
                self._respond(404, b'{"error":"Run not retained in this session"}', head=head)
                return
            self._respond(200, json.dumps(value).encode(), head=head)
            return
        elif self.path in STATIC_ROUTES:
            filename, content_type = STATIC_ROUTES[self.path]
            try:
                body = (STATIC / filename).read_bytes()
            except OSError:
                self.log_error("Unable to read static asset: %s", filename)
                self.send_error(500, "Unable to read static asset")
                return
        else:
            self.send_error(404, "Unknown route")
            return
        self._respond(200, body, content_type, head=head)

    def _respond(
        self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8",
        *, head: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._respond(status, json.dumps(value, allow_nan=False).encode("utf-8"))

    def do_POST(self) -> None:
        stop_route = RUN_ROUTE.fullmatch(self.path)
        artifact_route = ARTIFACT_ROUTE.fullmatch(self.path)
        if self.path not in (
            "/api/capabilities/refresh", "/api/configuration-preview",
            "/api/operations/preview", "/api/runs",
            "/api/artifacts/register", "/api/artifacts/refresh", "/api/artifacts/compatible",
        ) and not (stop_route and stop_route[2] == "/stop") and not (
            artifact_route and artifact_route[2] == "save"
        ):
            self._reject_write()
            return
        self.close_connection = True
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if (
            self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
            or self.headers.get("Origin", origin) != origin
            or self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none")
            or not secrets.compare_digest(
                self.headers.get("X-UI-CSRF", "").encode("utf-8"), self.server.csrf.encode("ascii"),
            )
        ):
            self._json(403, {"error": "Same-origin UI authorization required"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding") is not None
                or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                or self.headers.get("Content-Type") != "application/json"
            ):
                raise ValueError("invalid framing")
            length = int(lengths[0])
            limit = 2 if self.path.endswith("/refresh") else 524288 if artifact_route else 131072
            if not 0 < length <= limit:
                raise ValueError("invalid length")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            draft = json.loads(raw, object_pairs_hook=unique_object)
            if not isinstance(draft, dict):
                raise ValueError("object required")
        except (ValueError, UnicodeError, OSError, RecursionError):
            self._json(400, {"error": "Malformed JSON request"})
            return
        if self.path.startswith("/api/artifacts/"):
            try:
                result = self._artifact_action(draft, artifact_route)
            except KeyError:
                self._json(404, {"error": "Unknown artifact"})
                return
            except CatalogError as error:
                self._json(500, {"error": str(error), "category": "artifact-history"})
                return
            except (OSError, ValueError, UnicodeError) as error:
                self._json(422, {"error": str(error), "category": "artifact-validation"})
                return
            self._json(200, result)
            return
        if self.path in ("/api/operations/preview", "/api/runs") or stop_route:
            try:
                if self.path == "/api/operations/preview":
                    result = self.server.execution.preview(draft)
                    status = 200
                elif self.path == "/api/runs":
                    result = self.server.execution.start(draft)
                    status = 202
                else:
                    if draft != {}:
                        raise ValueError("Stop accepts only an empty object; PID termination is prohibited")
                    assert stop_route is not None
                    result = self.server.execution.registry.get(stop_route[1]).stop()
                    status = 200
            except RegistryFull as error:
                self._json(409, {"error": str(error), "category": "admission"})
                return
            except CatalogError as error:
                self._json(500, {"error": str(error), "category": "artifact-history"})
                return
            except KeyError:
                self._json(404, {"error": "Run or preview not retained in this session"})
                return
            except (ValueError, OSError) as error:
                self._json(422, {"error": str(error), "category": "validation"})
                return
            self._json(status, result)
            return
        if self.path == "/api/configuration-preview":
            try:
                # Validate but do not echo potentially sensitive user-entered strings.
                configuration_from_value(draft)
            except (ConfigurationError, ValueError, TypeError, RecursionError):
                self._json(422, {
                    "saved": False, "valid": False,
                    "reason": "Invalid or prohibited configuration; check Issue #4 model constraints. Nothing saved.",
                })
                return
            self._json(200, {
                "saved": False, "valid": True,
                "reason": "Issue #4 model validation passed. Unsaved; active configuration unchanged. Readiness unknown.",
            })
            return
        if draft != {}:
            self._json(400, {"error": "Refresh accepts only an empty object"})
            return
        if not self.server.refresh_lock.acquire(blocking=False):
            self._json(409, {"error": "Capability refresh already in progress"})
            return
        try:
            document = self.server.refresh()
            self._json(200, document)
        finally:
            self.server.refresh_lock.release()

    def _artifact_action(
        self, draft: dict[str, object], route: re.Match[str] | None,
    ) -> object:
        catalog = self.server.catalog
        if self.path == "/api/artifacts/refresh":
            if draft:
                raise ValueError("Refresh accepts only an empty object")
            catalog.refresh()
            return catalog.document()
        if self.path == "/api/artifacts/compatible":
            if set(draft) != {"workflow", "field"} or not all(
                isinstance(value, str) for value in draft.values()
            ):
                raise ValueError("Expected workflow and field strings")
            accepted = accepted_artifact_types(draft["workflow"], draft["field"])
            catalog.refresh()
            return {"artifacts": catalog.compatible(accepted), "accepted_types": accepted,
                    "readiness": "unknown"}
        if self.path == "/api/artifacts/register":
            if set(draft) != {"path", "type"} or not all(
                isinstance(value, str) and "\0" not in value for value in draft.values()
            ):
                raise ValueError("Expected local path and artifact type strings")
            return asdict(catalog.register_input(Path(draft["path"]), draft["type"]))
        if route is None or set(draft) != {"revision", "text", "destination", "replace"}:
            raise ValueError("Expected revision, text, destination and explicit replace flag")
        if type(draft["replace"]) is not bool or not all(
            isinstance(draft[key], str) and "\0" not in draft[key]
            for key in ("revision", "text", "destination")
        ):
            raise ValueError("Invalid text save fields")
        return asdict(catalog.save_text(
            route[1], draft["revision"], draft["text"], Path(draft["destination"]),
            replace_existing=draft["replace"],
        ))

    def _reject_write(self) -> None:
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    do_PUT = _reject_write
    do_PATCH = _reject_write
    do_DELETE = _reject_write


class WorkflowServer(ThreadingHTTPServer):
    def __init__(
        self, port: int = 8765, *,
        configuration: Configuration | None = None,
        reports: tuple[DiscoveryReport, ...] = (),
        refresh: Refresh = refresh_capabilities,
        artifact_history: Path | None = None,
    ) -> None:
        self.configuration = configuration or Configuration()
        self.configuration_preview = configuration_document(self.configuration)
        self.catalog = ArtifactCatalog(artifact_history or default_history_path())
        self.execution = ExecutionAPI(self.configuration, self.catalog)
        self.refresh_boundary = refresh
        self.refresh_lock = threading.Lock()
        self.csrf = secrets.token_hex(32)
        self.reports = reports
        self.refresh_status = {
            "state": State.UNKNOWN, "readiness": Readiness.UNKNOWN,
            "reason": "Not refreshed. Startup performs no discovery.",
        }
        self.workflow_json = json.dumps(
            workflow_document(configuration, reports=reports),
        ).encode("utf-8")
        super().__init__(("127.0.0.1", port), WorkflowHandler)

    def server_close(self) -> None:
        self.execution.registry.close()
        super().server_close()

    def refresh(self) -> dict[str, object]:
        sources = self.configuration.sources
        try:
            reports = self.refresh_boundary(sources)
            if (
                not isinstance(reports, tuple)
                or len(reports) != len(sources)
                or {report.source for report in reports} != {source.identity for source in sources}
                or any(item.source != report.source for report in reports for item in report.capabilities)
            ):
                raise ValueError("refresh returned unconfigured or incomplete sources")
            failed = any(
                item.state == State.ERROR for report in reports for item in report.capabilities
            )
            status = {
                "state": State.ERROR if failed else State.SUPPORTED,
                "readiness": Readiness.UNKNOWN,
                "reason": "Refresh returned explicit capability errors; inspect reports."
                if failed else "Read-only refresh complete; resource readiness is separate."
                if sources else "No configured discovery sources; no paths scanned.",
            }
        except (OSError, ValueError, TypeError, RuntimeError, AttributeError) as error:
            reason = f"Read-only capability refresh failed ({type(error).__name__}); previous evidence replaced, readiness unknown."
            reports = tuple(
                DiscoveryReport(source.identity, (Capability(
                    source.identity, "discovery", None, Provenance.SOURCE,
                    Verification.UNVERIFIED, State.ERROR, reason=reason,
                ),)) for source in sources
            )
            status = {"state": State.ERROR, "readiness": Readiness.UNKNOWN, "reason": reason}
        document = workflow_document(self.configuration, reports=reports)
        document["refresh"] = status
        snapshot = json.dumps(document, allow_nan=False).encode("utf-8")
        self.reports, self.refresh_status, self.workflow_json = reports, status, snapshot
        return document


def create_server(
    port: int = 8765, *,
    configuration: Configuration | None = None,
    reports: tuple[DiscoveryReport, ...] = (),
    refresh: Refresh | None = None,
    artifact_history: Path | None = None,
) -> WorkflowServer:
    """No host override: every server created here binds to IPv4 loopback."""
    kwargs = {} if refresh is None else {"refresh": refresh}
    if artifact_history is not None:
        kwargs["artifact_history"] = artifact_history
    return WorkflowServer(port, configuration=configuration, reports=reports, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--artifact-history", type=Path, default=default_history_path())
    args = parser.parse_args()
    with create_server(args.port, artifact_history=args.artifact_history) as server:
        print(
            f"Local workflow UI: http://127.0.0.1:{server.server_port}/",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
