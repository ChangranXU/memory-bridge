"""Test-support capture server shared by every test suite in the bundle.

A real local HTTP stand-in for the recorder's annotate endpoint, used by the
shared-bridge suite and both integration suites. It lives in the package
rather than in a tests/ helper module because pytest's prepend import mode
puts only a suite's own tests/ dir on sys.path — a tests/-local helper would
need a cross-root path hack from the sibling suites. Stdlib only.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEST_TRAJECTORY_ID = "t3st-bearer-trajectory-id"


class CaptureServer:
    """Threaded stdlib HTTP server that records annotation POSTs.

    responder(path, events) -> (status, body) can be replaced per test; the
    default answers every post with a 202 carrying role_call_cursor. Events
    arrive exactly as the bridge sent them (annotation_ids included)."""

    def __init__(self):
        self.requests: list[dict] = []  # {"path", "events"}
        self.cursor = 0
        self.responder = None

        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.loads(body)
                server.requests.append({"path": self.path, "events": payload.get("events", [])})
                if server.responder is not None:
                    status, response = server.responder(self.path, payload.get("events", []))
                else:
                    response = {
                        "recorded": len(payload.get("events", [])),
                        "duplicates": 0,
                        "role_call_cursor": server.cursor,
                    }
                    status = 202
                # A bytes response goes out verbatim (malformed-body tests).
                data = response if isinstance(response, bytes) else json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def lane_url(self, lane: str) -> str:
        """A trajectory-scoped model base URL for one role lane."""
        return f"{self.url}/{lane}/trajectories/{TEST_TRAJECTORY_ID}/v1"

    def annotate_url(self, lane: str) -> str:
        return f"{self.url}/{lane}/trajectories/{TEST_TRAJECTORY_ID}/annotate"

    def events(self, event_type: str | None = None) -> list[dict]:
        seen = [event for request in self.requests for event in request["events"]]
        return [e for e in seen if event_type is None or e["type"] == event_type]

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
