"""Results viewer: a stdlib HTTP server exposing result files as JSON plus a static UI.

GET /api/runs            index of runs in the results directory
GET /api/runs/<run_id>   full result.json for one run
GET /                    the viewer (pagereuse/web/)
"""

from __future__ import annotations

import json
import re
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WEB_DIR = Path(__file__).parent / "web"
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")


def index_runs(results_dir: Path) -> list[dict]:
    runs = []
    for path in sorted(Path(results_dir).glob("*/result.json")):
        if not _RUN_ID.match(path.parent.name):
            continue
        r = json.loads(path.read_text())
        runs.append(
            {
                "run_id": r["run_id"],
                "started_at": r["started_at"],
                "label": r["config"].get("label") or r["config"]["table"],
                "table": r["config"]["table"],
                "fillfactor": r["config"]["fillfactor"],
                "rows": r["config"]["rows"],
                "steps": [c["name"] for c in r["checkpoints"]],
                "main_pages": [c["relation"]["exact"]["main_pages"] for c in r["checkpoints"]],
                "summary": r["summary"],
            }
        )
    runs.sort(key=lambda r: r["started_at"], reverse=True)
    return runs


def load_run(results_dir: Path, run_id: str) -> dict | None:
    if not _RUN_ID.match(run_id):
        return None
    path = Path(results_dir) / run_id / "result.json"
    return json.loads(path.read_text()) if path.is_file() else None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, results_dir: Path, **kwargs):
        self.results_dir = results_dir
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/runs":
            return self._json(index_runs(self.results_dir))
        if path.startswith("/api/runs/"):
            run = load_run(self.results_dir, path[len("/api/runs/") :])
            if run is None:
                return self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return self._json(run)
        super().do_GET()

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args) -> None:  # quiet by default
        pass


def make_server(results_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), partial(Handler, results_dir=Path(results_dir)))


def serve(results_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    with make_server(results_dir, host, port) as httpd:
        print(f"PageReuse Lab viewer: http://{host}:{httpd.server_address[1]}/  (results: {results_dir})")
        httpd.serve_forever()
