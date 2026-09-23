"""DEAP replay transport: every branch owns a disposable remote workspace."""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit

import httpx

from ._util import normalize_artifact_rel_path
from .adapters import resolve_replay_api_key
from .artifacts import decode_bundle_payload, skill_treatment_members
from .hooks import RUNTIME_METRICS
from .protocol import REPLAY_TURN_RESULT_SCHEMA_V1


class DeapReplayAdapter:
    def __init__(self, endpoint, *, employee_no="", auth_profile="", client=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("DEAP endpoint must be an HTTP(S) URL")
        self.endpoint = endpoint.rstrip("/")
        suffix = "/deapAgent/blocking/message"
        if self.endpoint.endswith(suffix):
            self.endpoint = self.endpoint[: -len(suffix)]
        self.workspace = "te-replay-" + uuid.uuid4().hex
        self.session_id = "te-session-" + uuid.uuid4().hex
        self._initialized = False
        self._touched = False
        self._owned_client = client is None
        self._client = client or httpx.Client(timeout=30)
        self._headers = {"x-sf-employeeNo": str(employee_no), "X-Session-ID": self.session_id}
        key = resolve_replay_api_key(auth_profile)
        if key:
            self._headers["Authorization"] = f"Bearer {key}"

    def _post(self, path, body, timeout=30):
        self._touched = True
        response = self._client.post(
            self.endpoint + path,
            json=body,
            headers=self._headers,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def _update(self, name, content):
        result = self._post(
            "/skillopt/update",
            {
                "workspace": self.workspace,
                "fileName": name,
                "content": content,
            },
        )
        if not result.get("success"):
            raise RuntimeError("DEAP rejected workspace update; check hot-reload configuration")

    def _prepare(self, skill):
        for member in skill_treatment_members(skill):
            self._prepare_one(member)

    def _prepare_one(self, skill):
        name = str(skill.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("invalid DEAP skill name")
        bundle = skill.get("bundle")
        files = decode_bundle_payload(bundle) if isinstance(bundle, dict) else {"SKILL.md": skill.get("content", "")}
        texts = {
            normalize_artifact_rel_path(path): content.decode("utf-8") if isinstance(content, bytes) else str(content)
            for path, content in files.items()
        }
        if not texts.get("SKILL.md"):
            raise ValueError("DEAP replay requires a nonempty SKILL.md")
        # The first update initializes the isolated overlay from the original.
        self._update(name + "/SKILL.md", texts["SKILL.md"])
        if isinstance(bundle, dict):
            result = self._post(
                "/skillopt/delete",
                {
                    "workspace": self.workspace,
                    "fileName": "skills/" + name,
                },
            )
            if not result.get("success"):
                raise RuntimeError("DEAP could not reset the isolated skill bundle")
        for path, content in texts.items():
            self._update(name + "/" + path, content)

    def call_turn(self, request):
        result = {
            "schema_version": REPLAY_TURN_RESULT_SCHEMA_V1,
            "protocol_version": "1.0",
            "request_id": request["request_id"],
            "turn_num": request["turn_num"],
            "branch": request.get("branch", ""),
            "runtime": {"type": "deap"},
            "messages": [],
            "artifacts": [],
            "metrics": {},
            "metrics_incomplete": True,
        }
        try:
            if not self._initialized:
                self._prepare(request.get("skill"))
                self._initialized = True
            body = self._post(
                "/deapAgent/blocking/message",
                {
                    "query": str(request.get("prompt") or ""),
                    "conversationId": self.session_id,
                    "workspace": self.workspace,
                    "responseMode": "blocking",
                },
                timeout=max(1, int((request.get("limits") or {}).get("turn_timeout_seconds", 300))),
            )
            if not isinstance(body.get("answer"), str):
                raise ValueError("DEAP response missing answer")
            replay = body.get("replay") if isinstance(body.get("replay"), dict) else {}
            raw_metrics = (
                replay.get("metrics")
                if isinstance(replay.get("metrics"), dict)
                else {}
            )
            metrics = {
                key: value
                for key, value in raw_metrics.items()
                if key in RUNTIME_METRICS
            }
            messages = (
                [dict(item) for item in replay.get("messages") or [] if isinstance(item, dict)]
                if isinstance(replay.get("messages"), list)
                else []
            )
            artifacts = (
                list(replay.get("artifacts") or [])
                if isinstance(replay.get("artifacts"), list)
                else []
            )
            missing = [
                key
                for key in ("tool_call_count", "total_tokens")
                if key not in metrics
            ]
            result.update(
                status="succeeded",
                final_response=body["answer"],
                messages=messages,
                artifacts=artifacts,
                metrics=metrics,
                metrics_incomplete=bool(missing),
                metrics_incomplete_reason=(
                    "missing required metrics: " + ", ".join(missing)
                    if missing
                    else ""
                ),
                trace_id=str(body.get("traceId") or replay.get("trace_id") or ""),
            )
        except Exception as exc:
            result.update(
                status="failed",
                final_response="",
                error={"code": "DEAP_REPLAY_FAILED", "message": str(exc), "retryable": False},
            )
        return result

    def close(self):
        try:
            if self._touched:
                result = self._post("/skillopt/delete", {"workspace": self.workspace})
                if not result.get("success") and "\u4e0d\u5b58\u5728" not in str(result.get("message")):
                    raise RuntimeError("DEAP replay workspace cleanup failed: " + self.workspace)
        finally:
            if self._owned_client:
                self._client.close()
