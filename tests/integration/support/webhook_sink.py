"""Minimal in-memory webhook sink for integration tests.

The notification-worker POSTs its notification payload to :data:`WEBHOOK_URL`
(typically ``http://webhook-sink:9000/hook``). This stdlib-only server records
every body so tests can assert what was delivered and, crucially, how many
times.

Endpoints
---------
``POST /hook``
    Store the JSON body and return ``{"status": "stored", "count": N}``.
``GET /events``
    Return the stored events as a JSON list (oldest first).
``DELETE /events``
    Clear the store and return ``{"status": "cleared", "count": 0}``.
``GET /health``
    Liveness probe used by the compose healthcheck.

Environment
-----------
``PORT`` (default ``9000``) host port, ``HOST`` (default ``0.0.0.0``),
``EVENTS_JSONL`` (optional) path to append each stored event as JSONL.

Run standalone with ``python webhook_sink.py`` (or ``python -m support`` from
``tests/integration``).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

__all__ = ["EventStore", "Handler", "main"]

DEFAULT_PORT = 9000
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - local test sink, bound inside a container
_MAX_BODY_BYTES = 1_000_000


class EventStore:
    """Thread-safe in-memory store of received webhook events."""

    def __init__(self, jsonl_path: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._jsonl_path = str(jsonl_path) if jsonl_path else None

    def add(self, event: dict[str, Any]) -> int:
        """Append ``event`` and return the new count."""
        with self._lock:
            self._events.append(event)
            count = len(self._events)
        if self._jsonl_path is not None:
            with Path(self._jsonl_path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
        return count

    def list(self) -> list[dict[str, Any]]:
        """Return a shallow copy of the stored events (oldest first)."""
        with self._lock:
            return list(self._events)

    def clear(self) -> None:
        """Remove every stored event."""
        with self._lock:
            self._events.clear()


def _default_jsonl_path() -> str | None:
    """Return the optional JSONL path for this sink process."""
    configured = os.environ.get("EVENTS_JSONL")
    if configured:
        return configured
    return os.path.join(tempfile.gettempdir(), "events.jsonl")


class Handler(BaseHTTPRequestHandler):
    """HTTP request handler backed by a module-level :class:`EventStore`."""

    store: EventStore
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Silence per-request logging; tests only inspect stored events."""
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Any:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/hook":
            self._send_json(404, {"status": "not_found", "path": self.path})
            return
        event = self._read_json_body()
        if not isinstance(event, dict):
            self._send_json(400, {"status": "invalid_body"})
            return
        count = self.store.add(event)
        self._send_json(200, {"status": "stored", "count": count})

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/events":
            self._send_json(200, self.store.list())
            return
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"status": "not_found", "path": self.path})

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] != "/events":
            self._send_json(404, {"status": "not_found", "path": self.path})
            return
        self.store.clear()
        self._send_json(200, {"status": "cleared", "count": 0})


def build_server(port: int | None = None, host: str | None = None) -> ThreadingHTTPServer:
    """Build (but do not start) the sink server."""
    resolved_port = port if port is not None else int(os.environ.get("PORT", DEFAULT_PORT))
    resolved_host = host or os.environ.get("HOST", DEFAULT_HOST)
    Handler.store = EventStore(_default_jsonl_path())
    return ThreadingHTTPServer((resolved_host, resolved_port), Handler)


def main() -> None:
    """Run the webhook sink until interrupted."""
    server = build_server()
    print(f"webhook-sink listening on {server.server_address}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
