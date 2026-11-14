#!/usr/bin/env python3
"""Tiny web server for the benchmark history.

  python3 bench/server.py [--port 8377]

Open the printed address (http://127.0.0.1:8377/ unless that port is taken) to see the time each solver iteration takes on the
benchmark suite. The page can also build and time the current working tree,
which appends a new iteration to bench/history.json.
Standard library only; listens on localhost.
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import run as bench

HERE = Path(__file__).resolve().parent
state = {"busy": None, "last_error": None}
lock = threading.Lock()


def read_history():
    with bench.History() as h:
        return json.loads(json.dumps(h))


def run_job(label: str, note: str):
    try:
        state["busy"] = "Building…"
        bench.build()

        def log(msg, **_):
            state["busy"] = msg

        bench.record("current", label, note, None, timeout=3600, log=log)
        state["last_error"] = None
    except Exception as e:  # shown on the page
        state["last_error"] = f"Benchmark failed: {e}"
    finally:
        state["busy"] = None


class Handler(BaseHTTPRequestHandler):
    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/history":
            payload = {"history": read_history(), "busy": state["busy"], "last_error": state["last_error"]}
            self.send(200, json.dumps(payload).encode(), "application/json")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            return self.send(404, b"not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            label = str(body.get("label", "")).strip()[:80]
            note = str(body.get("note", "")).strip()[:400]
        except ValueError:
            return self.send(400, b"bad json", "text/plain")
        if not label:
            return self.send(400, b"label required", "text/plain")
        with lock:
            if state["busy"]:
                return self.send(409, b"a benchmark is already running", "text/plain")
            state["busy"] = "Starting…"
        threading.Thread(target=run_job, args=(label, note), daemon=True).start()
        self.send(202, b"started", "text/plain")

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8377)
    a = ap.parse_args()
    srv = None
    for port in range(a.port, a.port + 20):  # skip ports something else is using
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        raise SystemExit(f"no free port in {a.port}..{a.port + 19}")
    print(f"benchmark history: http://127.0.0.1:{srv.server_address[1]}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
