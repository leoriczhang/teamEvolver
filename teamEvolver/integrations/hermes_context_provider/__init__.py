"""Hermes MemoryProvider backed by teamEvolver Context Workspace."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

try:
    from teamEvolver.integrations.hermes_delivery import HermesDeliverySpool
except ImportError:
    try:
        from .hermes_delivery import HermesDeliverySpool
    except ImportError:
        from hermes_delivery import HermesDeliverySpool


def _hermes_home() -> Path:
    configured = str(os.environ.get("HERMES_HOME") or "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _load_feed() -> dict[str, Any]:
    override = str(os.environ.get("TEAMEVOLVER_FEED_CONFIG") or "").strip()
    path = (
        Path(override).expanduser()
        if override
        else _hermes_home()
        / "skills"
        / "teamEvolver-feed"
        / "feed.json"
    )
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class TeamEvolverMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._config: dict[str, Any] = {}
        self._session_id = ""
        self._context_session_id = ""
        self._sequence = 0
        self._used_context_refs: set[str] = set()
        self._sync_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return "team_evolver"

    def is_available(self) -> bool:
        config = _load_feed()
        return bool(
            str(config.get("base_url") or "").strip()
            and str(config.get("workspace_token") or "").strip()
            and str(config.get("external_subject") or "").strip()
            and str(config.get("integration_id") or "").strip()
        )

    def initialize(self, session_id: str, **kwargs) -> None:
        del kwargs
        self._config = _load_feed()
        self._start_session(str(session_id or ""))

    def system_prompt_block(self) -> str:
        return (
            "teamEvolver Context Workspace is active. Relevant personal/team "
            "Memory and Skill context is prefetched per turn. Use the "
            "team_evolver_context_read tool only when a returned context_ref "
            "needs full expansion. Personal Memory writes require "
            "team_evolver_memory_remember; team Memory and team Skills are read-only."
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        base_url = str(self._config.get("base_url") or "").rstrip("/")
        if query:
            path += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            base_url + path,
            data=(
                json.dumps(body, ensure_ascii=False).encode("utf-8")
                if body is not None
                else None
            ),
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": (
                    "Bearer "
                    + str(self._config.get("workspace_token") or "")
                ),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8") or "{}")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            raise RuntimeError(f"teamEvolver Context request failed: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("teamEvolver Context response is not an object")
        return value

    def _identity(self) -> dict[str, str]:
        return {
            "integration_id": str(self._config.get("integration_id") or ""),
            "external_subject": str(
                self._config.get("external_subject")
                or self._config.get("user_alias")
                or ""
            ),
        }

    def _delivery_spool(self) -> HermesDeliverySpool:
        configured = str(self._config.get("spool_dir") or "").strip()
        return HermesDeliverySpool(
            (
                Path(configured).expanduser()
                if configured
                else _hermes_home() / "teamEvolver-feed-spool"
            ),
            integration_id=str(
                self._config.get("integration_id") or "hermes:local"
            ),
        )

    def _delivery_sender(self, delivery: dict[str, Any]) -> dict[str, Any]:
        paths = {
            "context.start": "/internal/agents/context/sessions/start",
            "context.append": "/internal/agents/context/sessions/append",
            "context.commit": "/internal/agents/context/sessions/commit",
        }
        kind = str(delivery.get("kind") or "")
        if kind not in paths:
            raise ValueError(f"unsupported Context delivery kind: {kind}")
        return self._request(
            "POST",
            paths[kind],
            body=dict(delivery.get("payload") or {}),
            timeout=30.0 if kind == "context.commit" else 15.0,
        )

    def _durable_context_request(
        self,
        *,
        kind: str,
        aggregate_id: str,
        sequence: int,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        if not str(self._config.get("integration_id") or ""):
            paths = {
                "context.start": "/internal/agents/context/sessions/start",
                "context.append": "/internal/agents/context/sessions/append",
                "context.commit": "/internal/agents/context/sessions/commit",
            }
            return self._request(
                "POST",
                paths[kind],
                body=body,
                timeout=30.0 if kind == "context.commit" else 15.0,
            )
        spool = self._delivery_spool()
        delivery = spool.enqueue(
            kind=kind,
            aggregate_id=aggregate_id,
            sequence=sequence,
            payload=body,
        )
        result = spool.deliver(
            str(delivery["delivery_id"]),
            self._delivery_sender,
            force=True,
        )
        if result.get("status") != "acked":
            raise RuntimeError(
                str(result.get("last_error") or "Context delivery is pending")
            )
        return dict(result.get("ack") or {})

    def _start_session(self, session_id: str) -> None:
        self._session_id = session_id
        self._context_session_id = ""
        self._sequence = 0
        with self._lock:
            self._used_context_refs.clear()
        if not session_id or not self.is_available():
            return
        try:
            result = self._durable_context_request(
                kind="context.start",
                aggregate_id=session_id,
                sequence=1,
                body={
                    **self._identity(),
                    "external_session_id": session_id,
                },
            )
            self._context_session_id = str(
                result.get("context_session_id") or ""
            )
        except Exception:
            self._context_session_id = ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if session_id and session_id != self._session_id:
            self._start_session(session_id)
        try:
            result = self._request(
                "POST",
                "/internal/agents/context/resolve",
                body={
                    **self._identity(),
                    "context_session_id": self._context_session_id,
                    "query": str(query or ""),
                    "scopes": [
                        "personal_memory",
                        "team_memory",
                        "personal_skills",
                        "team_skills",
                    ],
                    "max_items": 12,
                    "max_chars": 16_000,
                },
                timeout=10.0,
            )
        except Exception:
            return ""
        items = [
            item
            for item in result.get("items") or []
            if isinstance(item, dict) and item.get("selected", True)
        ]
        self._record_usage(result, items)
        lines = ["<team_evolver_context>"]
        for item in items:
            label = str(
                item.get("qualified_skill_id")
                or item.get("title")
                or "context"
            )
            content = str(item.get("l1") or item.get("l0") or "").strip()
            if not content:
                continue
            lines.append(
                f"[{item.get('scope')}] {label} "
                f"(context_ref={item.get('context_ref')}):\n{content}"
            )
        lines.append("</team_evolver_context>")
        return "\n\n".join(lines) if len(lines) > 2 else ""

    def _record_usage(
        self,
        envelope: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> None:
        path = (
            _hermes_home()
            / "teamEvolver-context-usage"
            / f"{self._session_id}.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "context_snapshot_id": str(envelope.get("snapshot_id") or ""),
            "memory_refs": [
                {
                    "context_ref": item.get("context_ref"),
                    "operation": "injected",
                }
                for item in items
                if item.get("kind") == "memory"
            ],
            "skill_refs": [
                {
                    "context_ref": item.get("context_ref"),
                    "operation": (
                        "selected" if item.get("selected", True) else "retrieved"
                    ),
                }
                for item in items
                if item.get("kind") == "skill"
            ],
            "feedback": {},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(path, 0o600)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        del messages
        if session_id and session_id != self._session_id:
            self._start_session(session_id)
        if not self._context_session_id:
            return

        def sync() -> None:
            deliveries: list[dict[str, Any]] = []
            for role, content in (
                ("user", user_content),
                ("assistant", assistant_content),
            ):
                if not str(content or "").strip():
                    continue
                with self._lock:
                    self._sequence += 1
                    sequence = self._sequence
                deliveries.append(
                    self._delivery_spool().enqueue(
                        kind="context.append",
                        aggregate_id=self._context_session_id,
                        sequence=sequence,
                        payload={
                            "context_session_id": self._context_session_id,
                            "event_id": (
                                f"{self._session_id}:{sequence}:{role}"
                            ),
                            "sequence": sequence,
                            "role": role,
                            "content": str(content),
                        },
                    )
                )
            if deliveries:
                self._delivery_spool().flush(
                    self._delivery_sender,
                    limit=max(20, len(deliveries)),
                )

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2.0)
        self._sync_thread = threading.Thread(target=sync, daemon=True)
        self._sync_thread.start()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        del messages
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        if not self._context_session_id:
            return
        try:
            self._durable_context_request(
                kind="context.commit",
                aggregate_id=self._context_session_id,
                sequence=9_000,
                body={
                    "context_session_id": self._context_session_id,
                    "used_context_refs": sorted(self._used_context_refs),
                },
            )
            with self._lock:
                self._used_context_refs.clear()
        except Exception:
            pass

    def on_session_switch(
        self,
        new_session_id: str,
        **kwargs,
    ) -> None:
        del kwargs
        self.on_session_end([])
        self._start_session(str(new_session_id or ""))

    def shutdown(self) -> None:
        self.on_session_end([])

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "team_evolver_context_read",
                "description": "Expand an opaque teamEvolver context_ref.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "context_ref": {"type": "string"},
                        "level": {
                            "type": "string",
                            "enum": ["l0", "l1", "l2", "full"],
                            "default": "l1",
                        },
                    },
                    "required": ["context_ref"],
                },
            },
            {
                "name": "team_evolver_memory_remember",
                "description": "Write a personal Memory through teamEvolver.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "category": {"type": "string", "default": "agent"},
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "team_evolver_memory_forget",
                "description": "Forget a personal Memory using its context_ref.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "context_ref": {"type": "string"}
                    },
                    "required": ["context_ref"],
                },
            },
        ]

    def handle_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        **kwargs,
    ) -> str:
        del kwargs
        if tool_name == "team_evolver_context_read":
            context_ref = str(args.get("context_ref") or "")
            result = self._request(
                "POST",
                "/internal/agents/context/read",
                body={
                    "context_ref": context_ref,
                    "level": str(args.get("level") or "l1"),
                },
            )
            if context_ref:
                with self._lock:
                    self._used_context_refs.add(context_ref)
        elif tool_name == "team_evolver_memory_remember":
            result = self._request(
                "POST",
                "/internal/agents/context/remember",
                body={
                    **self._identity(),
                    "context_session_id": self._context_session_id,
                    "content": str(args.get("content") or ""),
                    "category": str(args.get("category") or "agent"),
                },
            )
        elif tool_name == "team_evolver_memory_forget":
            result = self._request(
                "POST",
                "/internal/agents/context/forget",
                body={"context_ref": str(args.get("context_ref") or "")},
            )
        else:
            result = {"error": f"unsupported tool: {tool_name}"}
        return json.dumps(result, ensure_ascii=False)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(TeamEvolverMemoryProvider())
