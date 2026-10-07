"""HTTP service for the ground-calibration spreadsheet.

Endpoints
---------
GET  /                 single-page UI
GET  /health           liveness probe -> {"status":"ok", ...}
GET  /api/table        current table state (cells, edges, generations)
POST /api/table        reset/bulk load {"cells": [...]}
POST /api/candidates   submit a one-cell candidate, receive the verdict

All requests receive JSON responses; the table is guarded by a single lock.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict

from .model import EvalError, MAX_CELLS, Table

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def cell_to_dict(cell) -> Dict[str, Any]:
    return {
        "index": cell.idx,
        "type": cell.kind.value,
        "raw": cell.raw,
        "value": cell.value,
        "error": cell.error,
        "generation": cell.generation,
        "deps": sorted(cell.deps),
    }


def table_to_dict(table: Table) -> Dict[str, Any]:
    reverse: Dict[int, list] = {i: [] for i in range(table.size)}
    for cell in table.cells:
        for dep in cell.deps:
            reverse[dep].append(cell.idx)
    return {
        "size": table.size,
        "generation": table.generation,
        "cells": [cell_to_dict(c) for c in table.cells],
        "reverse": {str(k): sorted(v) for k, v in reverse.items() if v},
    }


def verdict_to_dict(result) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "ok": result.ok,
        "generation": result.generation,
    }
    if result.ok:
        aff = result.affected
        body["affected"] = {
            "generation": aff.generation,
            "recomputed": aff.recomputed,
            "cache_hits": aff.cache_hits,
            "deps_added": {str(k): v for k, v in aff.deps_added.items()},
            "deps_removed": {str(k): v for k, v in aff.deps_removed.items()},
            "active_deps": {str(k): v for k, v in aff.active_deps.items()},
        }
    else:
        fail = result.failure
        body["failure"] = {
            "reason": fail.reason.value,
            "detail": fail.detail,
            "point": fail.point,
            "chain": fail.chain,
            "chain_labels": [f"c{i}" for i in fail.chain],
        }
    return body


class _State:
    def __init__(self, size: int = MAX_CELLS):
        self.lock = threading.Lock()
        self.table = Table(size)


def make_handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CalibrationSheet/1.0"

        def log_message(self, fmt, *args):  # quiet by default
            if os.environ.get("APP_HTTP_LOG"):
                super().log_message(fmt, *args)

        # -- helpers ------------------------------------------------------ #

        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise EvalError(f"invalid JSON body: {exc}")
            if not isinstance(payload, dict):
                raise EvalError("JSON body must be an object")
            return payload

        def _send_file(self, path: str, content_type: str) -> None:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # -- routes ------------------------------------------------------- #

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/health":
                self._send_json(200, {
                    "status": "ok",
                    "generation": state.table.generation,
                    "size": state.table.size,
                })
                return
            if path == "/api/table":
                with state.lock:
                    self._send_json(200, table_to_dict(state.table))
                return
            if path in ("/", "/index.html"):
                self._send_file(os.path.join(WEB_DIR, "index.html"),
                                "text/html; charset=utf-8")
                return
            if path == "/app.js":
                self._send_file(os.path.join(WEB_DIR, "app.js"),
                                "application/javascript; charset=utf-8")
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                payload = self._read_json()
                if path == "/api/table":
                    entries = payload.get("cells", [])
                    if not isinstance(entries, list):
                        raise EvalError("'cells' must be a list")
                    size = int(payload.get("size", state.table.size))
                    if not 1 <= size <= MAX_CELLS:
                        raise EvalError(f"size must be 1..{MAX_CELLS}")
                    with state.lock:
                        if size != state.table.size:
                            state.table = Table(size)
                        state.table.reset(entries)
                        self._send_json(200, table_to_dict(state.table))
                    return
                if path == "/api/candidates":
                    idx = int(payload["index"])
                    kind = payload.get("type", "formula")
                    with state.lock:
                        result = state.table.submit_candidate(
                            idx,
                            kind,
                            value=payload.get("value"),
                            raw=payload.get("raw"),
                        )
                        self._send_json(200 if result.ok else 422,
                                        verdict_to_dict(result))
                    return
                self._send_json(404, {"ok": False, "error": "not found"})
            except (EvalError, ValueError, KeyError, TypeError) as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})

    return Handler


def serve(host: str = "0.0.0.0", port: int = 8080,
          size: int = MAX_CELLS) -> ThreadingHTTPServer:
    state = _State(size)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.state = state
    return httpd


def main() -> None:
    host = os.environ.get("APP_HOST", "0.0.0.0")
    port = int(os.environ.get("APP_PORT", "8080"))
    size = int(os.environ.get("TABLE_SIZE", str(MAX_CELLS)))
    httpd = serve(host, port, size)
    print(f"calibration sheet listening on http://{host}:{port} (size={size})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
