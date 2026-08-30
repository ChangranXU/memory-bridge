"""Stdlib HTTP front exposing a ``MemoryEndpoint`` over the standardized routes.

Routes (the ``/v1/memories/`` path family, Leaderboard-style synchronous
semantics):

    GET    /health                  -> 200 {"status": "ok"}   (unauthenticated)
    POST   /v1/memories/            -> AddRequest    -> 200 AddResponse
    POST   /v1/memories/search/     -> SearchRequest -> 200 SearchResponse
    PUT    /v1/memories/{id}        -> UpdateRequest -> 200 UpdateResponse
    DELETE /v1/memories/{id}        -> 200 DeleteResponse

Errors are ``{"detail": {"reason": ...}}`` with standard status codes
(400 invalid request body, 404 unknown id or route, 500 integration failure).
No authentication: intended for local or trusted-network use — put an
authenticating reverse proxy in front before exposing it further.

The server is deliberately single-threaded (the contract is synchronous, and
integrations may hold thread-affine resources such as sqlite connections):
handlers run on the serving thread. Construct the endpoint on that same
thread, or use ``serve_in_thread`` which does both on one dedicated daemon
thread.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from pydantic import BaseModel, ValidationError

from shared_bridge.endpoint import (
    AddRequest,
    MemoryEndpoint,
    MemoryEndpointError,
    SearchRequest,
    UpdateRequest,
)

logger = logging.getLogger("shared_bridge.serve")


def make_handler(endpoint: MemoryEndpoint) -> type[BaseHTTPRequestHandler]:
    """Build a request-handler class bound to ``endpoint``."""

    class MemoryEndpointHandler(BaseHTTPRequestHandler):
        server_version = "MemoryEndpointHTTP/1.0"

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            logger.debug("%s - %s", self.address_string(), format % args)

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, reason: str) -> None:
            self._send(status, {"detail": {"reason": reason}})

        def _body(self, model: type[BaseModel]) -> BaseModel:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0:
                    # read(-1) means "read to EOF": a held-open socket would
                    # block the single serving thread forever.
                    raise MemoryEndpointError(400, "negative Content-Length")
                raw = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError) as e:
                raise MemoryEndpointError(400, f"invalid JSON body: {e}") from e
            try:
                return model.model_validate(raw)
            except ValidationError as e:
                raise MemoryEndpointError(400, f"invalid request: {e}") from e

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            try:
                if method == "GET" and path == "/health":
                    self._send(200, {"status": "ok"})
                elif method == "POST" and path.rstrip("/") == "/v1/memories":
                    self._send(200, endpoint.add(self._body(AddRequest)).model_dump())
                elif method == "POST" and path.rstrip("/") == "/v1/memories/search":
                    self._send(200, endpoint.search(self._body(SearchRequest)).model_dump())
                elif path.startswith("/v1/memories/"):
                    # Percent-decode: the id segment is URL-encoded on the wire
                    # (an encoded slash decodes to "/" and misses the route).
                    memory_id = unquote(path[len("/v1/memories/") :]).strip("/")
                    if not memory_id or "/" in memory_id:
                        self._error(404, f"unknown route: {method} {path}")
                    elif method == "PUT":
                        self._send(200, endpoint.update(memory_id, self._body(UpdateRequest)).model_dump())
                    elif method == "DELETE":
                        self._send(200, endpoint.delete(memory_id).model_dump())
                    else:
                        self._error(404, f"unknown route: {method} {path}")
                else:
                    self._error(404, f"unknown route: {method} {path}")
            except MemoryEndpointError as e:
                self._error(e.status_code, e.reason)
            except Exception:
                logger.exception("endpoint %s %s failed", method, path)
                self._error(500, "internal endpoint error")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

    return MemoryEndpointHandler


def make_server(endpoint: MemoryEndpoint, host: str = "127.0.0.1", port: int = 8080) -> HTTPServer:
    """Build (but do not start) an HTTP server for ``endpoint``.

    Single-threaded on purpose: handlers run on the serving thread, so a
    thread-affine endpoint (e.g. sqlite-backed) is always used from the
    thread that serves — construct such endpoints on that thread.
    """
    return HTTPServer((host, port), make_handler(endpoint))


def serve_in_thread(endpoint_factory, host: str = "127.0.0.1", port: int = 8080) -> HTTPServer:
    """Start serving on a daemon thread and return the server.

    ``endpoint_factory()`` is called on the serving thread, so thread-affine
    stores (sqlite) are both created and used on that one thread. A startup
    failure (e.g. the port is already bound) re-raises here immediately
    instead of surfacing as a timeout with no cause.
    """
    ready: threading.Event = threading.Event()
    holder: dict = {}

    def _run():
        try:
            server = make_server(endpoint_factory(), host, port)
        except BaseException as e:
            holder["error"] = e
            ready.set()
            return
        holder["server"] = server
        ready.set()
        try:
            server.serve_forever()
        finally:
            server.server_close()

    threading.Thread(target=_run, daemon=True).start()
    if not ready.wait(timeout=5):
        raise RuntimeError("endpoint server thread failed to start")
    if "error" in holder:
        raise holder["error"]
    return holder["server"]
