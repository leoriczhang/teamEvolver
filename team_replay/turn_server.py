"""Standalone replay-turn HTTP endpoint for customer Agents.

Usage:
    python scripts/replay_turn_server.py --port 8010 --api-key <secret>

This is the Agent-side half of teamEvolver's server-driven True Replay
(``replay.branch.v1`` capability with ``orchestration: server_driven``).
teamEvolver calls this endpoint once per interaction turn and owns the
checklist judge, progressive disclosure, turn counting, and metric
aggregation. This script only executes ONE turn per call and reports the
single-turn trace and usage.

To onboard YOUR agents, edit ``AGENT_HANDLERS`` below — one handler per
runtime_type (the key must match the ``runtime_type`` registered with
teamEvolver). Register one agent per handler:

    POST /internal/agents/register
    "capabilities": {"replay.branch.v1": {
        "transport": "http", "orchestration": "server_driven",
        "endpoint": "http://<this-host>:8010/turn/<runtime_type>",
        "auth_profile": "<profile>"}}
    # export TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY=<secret> on the
    # teamEvolver side; pass the same secret here via --api-key.

Per-turn contract (teamevolver.replay-turn-request.v1 / -result.v1):

  request : {request_id, turn_num, branch, prompt,
             limits.turn_timeout_seconds}
            + turn 1 only: context_snapshot, skill, materials, tool_policy
  response: {status: succeeded|failed|unsupported, final_response,
             messages, metrics: {tool_call_count, total_tokens, ...},
             artifacts?, error?}

Notes for handlers:
  * ``request_id`` is a session handle: turn 2+ resumes the same replay
    session. This script keeps the per-session history for you and passes
    it to the handler as ``req["history"]`` so even stateless agents can
    continue a task; if your agent is stateful, key its own session store
    by ``request_id`` instead.
  * ``messages`` is the ONLY evidence the server-side checklist judge can
    see — return the full turn trace (assistant/tool messages).
  * ``metrics.tool_call_count`` and ``metrics.total_tokens`` are REQUIRED
    on success (fail-closed): a turn without them is invalid, never
    silently zero-filled.
  * Raise ``ReplayUnsupportedError`` when the turn hits an external side
    effect that cannot be deterministically replayed (fail-closed, never
    fall back to a live call).
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from .protocol import (  # noqa: E402
    REPLAY_TURN_RESULT_SCHEMA_V1,
    AgentProtocolError,
    normalize_replay_turn_request,
    normalize_replay_turn_result,
)

DEFAULT_PORT = 8010
DEFAULT_SESSION_TTL_SECONDS = 3600

REPLAY_UNSUPPORTED_ERROR_CODE = "REPLAY_EXTERNAL_TOOL_UNSUPPORTED"


class ReplayUnsupportedError(RuntimeError):
    """Raised by a handler when the turn cannot be deterministically replayed."""


# ---------------------------------------------------------------------------
# Customer adaptation area — implement one handler per Agent runtime.
# ---------------------------------------------------------------------------


def example_handler(req: dict[str, Any]) -> dict[str, Any]:
    """Minimal reference handler. Replace with your real agent invocation.

    ``req`` fields:
      request_id, turn_num, branch, prompt, limits,
      history: [{turn_num, prompt, response, messages}] (prior turns),
      turn-1 only: context_snapshot, skill, materials, tool_policy

    Must return:
      {"final_response": str, "messages": [...],
       "metrics": {"tool_call_count": int, "total_tokens": int, ...},
       "artifacts": [...]}  # artifacts optional
    """
    # >>> Replace this body with your agent call, e.g.:
    # result = my_agent.run(
    #     task=req["prompt"],
    #     prior_turns=req["history"],          # or key your session by req["request_id"]
    #     skill_bundle=(req.get("skill") or {}).get("content"),
    #     frozen_context=req.get("context_snapshot"),
    #     materials=req.get("materials") or [],
    # )
    # return {
    #     "final_response": result["answer"],
    #     "messages": result["trace"],
    #     "metrics": {
    #         "tool_call_count": result["tool_calls"],
    #         "total_tokens": result["input_tokens"] + result["output_tokens"],
    #     },
    #     "artifacts": result.get("files", []),
    # }
    return {
        "final_response": f"[example:{req['branch']}] {req['prompt'][:120]}",
        "messages": [{"role": "assistant", "content": req["prompt"]}],
        "metrics": {"tool_call_count": 1, "total_tokens": 42},
        "artifacts": [],
    }


# key = runtime_type registered with teamEvolver; value = handler function.
AGENT_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "example": example_handler,
    # "openclaw": openclaw_handler,
    # "agent-b": agent_b_handler,
}


# ---------------------------------------------------------------------------
# Protocol plumbing (pure logic — no HTTP).
# ---------------------------------------------------------------------------


def _failed_turn(
    request: dict[str, Any],
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
        "protocol_version": "1.0",
        "request_id": str(request.get("request_id") or ""),
        "turn_num": request.get("turn_num") or 0,
        "branch": str(request.get("branch") or ""),
        "status": "failed",
        "final_response": "",
        "messages": [],
        "artifacts": [],
        "metrics": {},
        "error": {"code": code, "message": message, "retryable": False},
    }


def handle_turn_request(
    payload: dict[str, Any],
    handler: Callable[[dict[str, Any]], dict[str, Any]],
    sessions: dict[str, dict[str, Any]],
    *,
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
) -> dict[str, Any]:
    """Validate one turn request, run the handler, wrap the protocol result.

    ``sessions`` maps request_id -> {"history": [...], "updated_at": float}
    so consecutive turns resume the same replay session. Pure and
    thread-unsafe by design: the HTTP layer serializes per session."""
    now = time.time()
    for key in [k for k, v in sessions.items() if now - v["updated_at"] > session_ttl_seconds]:
        del sessions[key]

    try:
        request = normalize_replay_turn_request(payload)
    except AgentProtocolError as exc:
        return _failed_turn(payload or {}, code="INVALID_RESPONSE", message=str(exc))

    request_id = request["request_id"]
    turn_num = request["turn_num"]
    session = sessions.get(request_id)
    if session is None:
        if turn_num != 1:
            return _failed_turn(
                request,
                code="INVALID_RESPONSE",
                message=(
                    f"unknown replay session {request_id!r}: expired or turn 1 was never executed"
                ),
            )
        session = {"history": [], "updated_at": now}
        sessions[request_id] = session
    session["updated_at"] = now

    handler_request = {
        **request,
        "history": [dict(item) for item in session["history"]],
    }
    try:
        raw = handler(handler_request)
        if raw is None or not isinstance(raw, dict):
            raise ValueError("handler must return an object")
        result = normalize_replay_turn_result(
            {
                "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
                "protocol_version": "1.0",
                "request_id": request_id,
                "turn_num": turn_num,
                "branch": request["branch"],
                "status": "succeeded",
                "final_response": str(raw.get("final_response") or ""),
                "messages": list(raw.get("messages") or []),
                "artifacts": list(raw.get("artifacts") or []),
                "metrics": dict(raw.get("metrics") or {}),
            },
            expected_request_id=request_id,
            expected_turn_num=turn_num,
        )
    except ReplayUnsupportedError as exc:
        result = _failed_turn(
            request,
            code=REPLAY_UNSUPPORTED_ERROR_CODE,
            message=str(exc),
        )
        result["status"] = "unsupported"
    except AgentProtocolError as exc:
        # Fail-closed: missing tool_call_count/total_tokens invalidates the turn.
        result = _failed_turn(request, code="INVALID_RESPONSE", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - surface handler failure to the caller
        result = _failed_turn(
            request, code="EXECUTION_FAILED", message=f"{type(exc).__name__}: {exc}"
        )

    if result["status"] == "succeeded":
        session["history"].append(
            {
                "turn_num": turn_num,
                "prompt": request["prompt"],
                "response": result.get("final_response") or "",
                "messages": list(result.get("messages") or []),
            }
        )
    return result


# ---------------------------------------------------------------------------
# HTTP layer.
# ---------------------------------------------------------------------------


class TurnRequestHandler(BaseHTTPRequestHandler):
    api_key: str = ""
    handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    lock: threading.Lock = threading.Lock()

    def log_message(self, fmt: str, *args: Any) -> None:  # terse stdout logs
        sys.stderr.write("[replay-turn] %s\n" % (fmt % args))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.api_key:
            return True
        header = self.headers.get("Authorization") or ""
        expected = f"Bearer {self.api_key}"
        return hmac.compare_digest(header, expected)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            self._send_json(200, {"ok": True, "agents": sorted(self.handlers)})
            return
        self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if not self.path.startswith("/turn/"):
            self._send_json(404, {"error": "expected POST /turn/<runtime_type>"})
            return
        runtime_type = self.path[len("/turn/"):].strip("/").lower()
        handler = self.handlers.get(runtime_type)
        if handler is None:
            self._send_json(
                404,
                {"error": f"no handler registered for runtime_type {runtime_type!r}"},
            )
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        with self.lock:
            result = handle_turn_request(
                payload,
                handler,
                self.sessions,
                session_ttl_seconds=self.session_ttl_seconds,
            )
        status = str(result.get("status"))
        sys.stderr.write(
            f"[replay-turn] {runtime_type} turn={result.get('turn_num')} "
            f"status={status}\n"
        )
        self._send_json(200, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--api-key",
        default=os.environ.get("REPLAY_TURN_API_KEY", ""),
        help="Bearer token required on every turn (env: REPLAY_TURN_API_KEY)",
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=DEFAULT_SESSION_TTL_SECONDS,
        help="seconds an idle replay session stays resumable",
    )
    args = parser.parse_args()

    TurnRequestHandler.api_key = args.api_key
    TurnRequestHandler.handlers = dict(AGENT_HANDLERS)
    TurnRequestHandler.session_ttl_seconds = args.session_ttl
    server = ThreadingHTTPServer((args.host, args.port), TurnRequestHandler)
    print(
        f"replay-turn server on http://{args.host}:{args.port}  "
        f"agents={sorted(AGENT_HANDLERS)}  auth={'on' if args.api_key else 'OFF'}"
    )
    print("routes: POST /turn/<runtime_type>   GET /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
