"""HTTP 服务：页面、健康检查、状态查询、单格候选提交。"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Table
from .formula import cell_index

INDEX_HTML = Path(__file__).parent / "static" / "index.html"


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(table: Table):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeriveTable/1.0"
        protocol_version = "HTTP/1.1"

        # 静默访问日志
        def log_message(self, *args):  # noqa: N802
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            elif path == "/health":
                self._json(200, table.health())
            elif path == "/api/state":
                self._json(200, table.snapshot())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/reset":
                table.reset()
                self._json(200, table.snapshot())
                return
            if path == "/api/candidate":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    cell, raw = payload["cell"], payload["input"]
                    if not isinstance(cell, str) or not isinstance(raw, str):
                        raise ValueError
                except Exception:
                    self._json(400, {"error": '请求体须为 JSON：{"cell": "A1", "input": "..."}'})
                    return
                if cell_index(cell) < 0:
                    self._json(400, {"error": f"无效单元格：{cell}（应为 A1..H16）"})
                    return
                report = table.adjudicate(cell, raw)
                code = 200 if report["status"] == "published" else 409
                self._json(code, report)
                return
            self._json(404, {"error": "not found"})

    return Handler


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    table = Table()
    httpd = _HTTPServer(("0.0.0.0", port), make_handler(table))
    print(f"推导表服务已启动：http://0.0.0.0:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
