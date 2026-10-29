"""/healthz minimal (stdlib). `python -m commute_alerts.health --check` pour le healthcheck Docker."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .service import HealthState

LOOP_STALE = timedelta(minutes=12)  # la boucle se réveille au moins toutes les 5 min


def evaluate(h: HealthState, now: datetime) -> tuple[bool, dict[str, Any]]:
    body = h.as_dict()
    ok = (
        bool(h.last_loop_at)
        and now - datetime.fromisoformat(h.last_loop_at) < LOOP_STALE
        and bool(h.gtfs_imported_at)
    )
    body["status"] = "ok" if ok else "degraded"
    return ok, body


def serve(state: HealthState, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.rstrip("/") not in ("/healthz", ""):
                self.send_error(404)
                return
            ok, body = evaluate(state, datetime.now().astimezone())
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:  # silence
            pass

    # 0.0.0.0 : requis dans le conteneur ; le port n'est pas publié par compose.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104
    threading.Thread(target=server.serve_forever, name="healthz", daemon=True).start()
    return server


def check(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return 0 if r.status == 200 else 1
    except Exception:
        return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("HEALTH_PORT", "8080")))
    args = ap.parse_args()
    sys.exit(check(args.port) if args.check else 0)


if __name__ == "__main__":
    main()
