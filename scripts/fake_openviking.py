"""Fault-injectable OpenViking Compile stand-in for multi-process acceptance.

Run as a separate process; serves the subset of the OpenViking HTTP API that
TeamEvolver's Compile client and Team Miner use, keeps every submission keyed
by ``Idempotency-Key`` and exposes ``/_ctl/*`` endpoints to inject faults:

- ``POST /_ctl/mode {"mode": "ok" | "unavailable" | "drop_submit_response"}``
  ``unavailable`` answers 503 for every API call; ``drop_submit_response``
  records the next Compile submission and closes the connection unanswered
  (outcome unknown to the caller).
- ``POST /_ctl/finish {"task_id": ..., "status": "completed" | "failed"}``
- ``GET /_ctl/state`` returns submissions, task states and request counters.

    python scripts/fake_openviking.py --port 56190
"""

from __future__ import annotations

import argparse
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

LOCK = threading.Lock()
STATE = {"mode": "ok", "submissions": {}, "tasks": {}, "requests": {}, "unavailable_hits": 0}
WIKI = {"index.md": "# Index\n\nSee [Topic](topic.md).\n", "topic.md": "# Topic\n\nBack to [Index](index.md).\n"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - quiet access log
        return

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}") if length else {}

    def _route(self, method):
        url = urlparse(self.path)
        path, query = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        body = self._body() if method == "POST" else {}
        if path.startswith("/_ctl/"):
            return self._control(path, body)
        with LOCK:
            key = f"{method} {path.split('/')[3] if path.count('/') >= 3 else path}"
            STATE["requests"][key] = STATE["requests"].get(key, 0) + 1
            mode = STATE["mode"]
            if mode == "unavailable":
                STATE["unavailable_hits"] += 1
                return self._send(503, {"detail": "injected outage"})
        if method == "POST" and path == "/api/v1/compile":
            idem = self.headers.get("Idempotency-Key", "")
            with LOCK:
                task_id = STATE["submissions"].get(idem, {}).get("task_id")
                if task_id is None:
                    task_id = "ov_" + uuid.uuid4().hex[:16]
                    STATE["submissions"][idem] = {"task_id": task_id, "body": body}
                    STATE["tasks"][task_id] = "running"
                drop = STATE["mode"] == "drop_submit_response"
                if drop:
                    STATE["mode"] = "ok"
            if drop:
                self.close_connection = True
                self.connection.shutdown(2)
                return None
            return self._send(202, {"task_id": task_id, "status": "running"})
        if method == "GET" and path.startswith("/api/v1/compile/submissions/"):
            with LOCK:
                submission = STATE["submissions"].get(unquote(path.rsplit("/", 1)[1]))
                if submission is None:
                    return self._send(404, {"detail": "unknown submission"})
                task_id = submission["task_id"]
                return self._send(200, {"task_id": task_id, "status": STATE["tasks"][task_id]})
        if path.startswith("/api/v1/tasks/"):
            parts = path.split("/")
            task_id = parts[4]
            with LOCK:
                if task_id not in STATE["tasks"]:
                    return self._send(404, {"detail": "unknown task"})
                if method == "POST" and path.endswith("/cancel"):
                    STATE["tasks"][task_id] = "cancelled"
                return self._send(200, {"task_id": task_id, "status": STATE["tasks"][task_id]})
        if method == "GET" and path == "/api/v1/tasks":
            with LOCK:
                return self._send(200, {"tasks": [{"task_id": t, "status": s} for t, s in STATE["tasks"].items()]})
        if method == "GET" and path == "/api/v1/compile/capabilities":
            return self._send(200, {"compile": True})
        if method == "GET" and path == "/api/v1/fs/tree":
            root = query["uri"]
            return self._send(200, [{"uri": f"{root}/{name}", "isDir": False} for name in WIKI])
        if method == "GET" and path == "/api/v1/content/read":
            return self._send(200, {"status": "ok", "result": WIKI[query["uri"].rsplit("/", 1)[1]]})
        return self._send(404, {"detail": "not served by fake OpenViking"})

    def _control(self, path, body):
        with LOCK:
            if path == "/_ctl/mode":
                STATE["mode"] = body["mode"]
            elif path == "/_ctl/finish":
                STATE["tasks"][body["task_id"]] = body.get("status", "completed")
            elif path != "/_ctl/state":
                return self._send(404, {"detail": "unknown control"})
            return self._send(200, json.loads(json.dumps(STATE)))

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
