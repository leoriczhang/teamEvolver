"""Backward-compatible import for the full native DreamCycle runtime.

The historical simplified implementation remains below only for persisted
import compatibility. ``NativeDreamCycleSupervisor`` is rebound at EOF to the
complete Scheduler/ReAct implementation.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any
import httpx
from ..llm import AsyncLLMClient
from .dreamcycle import collect_personal_source_keys, collect_personal_source_users, parse_openviking_key

logger = logging.getLogger(__name__)
DEFAULT_EXTRACT_PROMPT = """You maintain team memory. Extract durable, reusable facts from personal memories. Remove secrets, transient details, duplicates, and person-specific content that should not be shared. Return concise Markdown bullets with source labels and confidence."""
DEFAULT_CONSOLIDATE_PROMPT = """You curate team memory. Merge candidate memories into the existing team memory. Preserve useful established rules, resolve conflicts conservatively, remove duplicates, and return only the final Markdown team-memory document."""


def _result(response: httpx.Response) -> Any:
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(str(data.get("error") or "OpenViking error"))
    return data.get("result") if isinstance(data, dict) and "result" in data else data


class NativeDreamCycleSupervisor:
    """Run memory extraction and consolidation inside teamEvolver."""
    def __init__(self, config: Any):
        self.config = config
        self._stop = threading.Event()
        self._daemon_thread: threading.Thread | None = None
        self._run_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_run_at = ""
        self._last_result: dict[str, Any] = {}
        self._last_error = ""

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "dreamcycle_enabled", False))

    def _missing(self) -> list[str]:
        values = {
            "sharing.viking_endpoint": getattr(self.config, "sharing_viking_endpoint", ""),
            "sharing.viking_team_api_key": getattr(self.config, "sharing_viking_team_api_key", "") or getattr(self.config, "sharing_viking_api_key", ""),
            "dreamcycle.llm_api_key": getattr(self.config, "dreamcycle_llm_api_key", "") or getattr(self.config, "llm_api_key", ""),
            "dreamcycle.llm_model": getattr(self.config, "dreamcycle_llm_model", "") or getattr(self.config, "llm_model_id", ""),
        }
        return [key for key, value in values.items() if not str(value or "").strip()]

    def start(self) -> dict[str, Any]:
        if not self._enabled() or not bool(getattr(self.config, "dreamcycle_auto_start", False)):
            return self.status()
        if self._missing():
            return self.status()
        if self._daemon_thread and self._daemon_thread.is_alive():
            return self.status()
        self._stop.clear()
        self._daemon_thread = threading.Thread(target=self._daemon_loop, name="teamEvolver-memory-evolution", daemon=True)
        self._daemon_thread.start()
        return self.status()

    def _daemon_loop(self) -> None:
        interval = max(60, int(getattr(self.config, "dreamcycle_interval_seconds", 86400) or 86400))
        while not self._stop.is_set():
            if not (self._run_thread and self._run_thread.is_alive()):
                self._run_cycle()
            self._stop.wait(interval)

    def trigger(self) -> dict[str, Any]:
        if not self._enabled():
            return {"status": "disabled", **self.status()}
        missing = self._missing()
        if missing:
            return {"status": "not_configured", "missing": missing, **self.status()}
        if self._run_thread and self._run_thread.is_alive():
            return {"status": "already_running", **self.status()}
        self._run_thread = threading.Thread(target=self._run_cycle, name="teamEvolver-memory-evolution-once", daemon=True)
        self._run_thread.start()
        return {"status": "started", **self.status()}

    def status(self) -> dict[str, Any]:
        missing = self._missing()
        running = bool(self._run_thread and self._run_thread.is_alive())
        return {"engine": "teamEvolver-native", "enabled": self._enabled(), "configured": not missing,
                "missing": missing, "running": running, "pid": None,
                "daemon_running": bool(self._daemon_thread and self._daemon_thread.is_alive()),
                "daemon_pid": None, "log_file": "", "daemon_log_file": "",
                "last_run_at": self._last_run_at, "last_result": self._last_result,
                "last_error": self._last_error}

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._daemon_thread, self._run_thread):
            if thread and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=10)

    def _headers(self, api_key: str, user: str) -> dict[str, str]:
        return {"Content-Type": "application/json", "X-API-Key": api_key,
                "Authorization": f"Bearer {api_key}",
                "X-OpenViking-Account": str(getattr(self.config, "sharing_viking_account", "") or "default"),
                "X-OpenViking-User": user,
                "X-OpenViking-Agent": "teamEvolver-memory-evolution"}

    def _sources(self) -> list[tuple[str, str]]:
        keys = collect_personal_source_keys(self.config)
        users = collect_personal_source_users(self.config)
        registry_path = Path(str(getattr(self.config, "users_registry_path", "") or "~/.teamEvolver/users.json")).expanduser()
        pairs: list[tuple[str, str]] = []
        try:
            for record in json.loads(registry_path.read_text("utf-8")).get("users") or []:
                personal = record.get("personal_space") if isinstance(record, dict) else None
                if isinstance(personal, dict):
                    user = str(personal.get("viking_user") or record.get("id") or "").strip()
                    key = str(personal.get("viking_api_key") or "").strip()
                    if user and key: pairs.append((user, key))
        except (OSError, ValueError, TypeError):
            pass
        for key in keys:
            _account, encoded_user = parse_openviking_key(key)
            if encoded_user:
                pairs.append((encoded_user, key))
        fallback_key = str(getattr(self.config, "sharing_viking_personal_api_key", "") or "").strip()
        for user in users:
            if user and fallback_key: pairs.append((user, fallback_key))
        return list(dict.fromkeys(pairs))

    def _read_memories(self, client: httpx.Client, user: str, key: str, max_items: int, max_chars: int) -> list[dict[str, str]]:
        root = f"viking://user/{user}/memories"
        try:
            tree = _result(client.get("/api/v1/fs/tree", headers=self._headers(key, user), params={"uri": root, "level_limit": 16, "node_limit": max_items * 4}))
        except Exception:
            return []
        entries = tree if isinstance(tree, list) else (tree.get("entries") if isinstance(tree, dict) else []) or []
        output: list[dict[str, str]] = []; used = 0
        for item in entries:
            if not isinstance(item, dict) or item.get("isDir") or item.get("is_dir"): continue
            uri = str(item.get("uri") or item.get("path") or "")
            if not uri.startswith(root + "/"): continue
            try:
                value = _result(client.get("/api/v1/content/read", headers=self._headers(key, user), params={"uri": uri, "offset": 0, "limit": -1, "raw": "true"}))
            except Exception: continue
            text = str(value.get("content") or value.get("text") or "") if isinstance(value, dict) else str(value or "")
            remaining = max_chars - used
            if remaining <= 0: break
            text = text[:remaining]; used += len(text)
            output.append({"source": user, "name": uri.rsplit("/", 1)[-1], "content": text})
            if len(output) >= max_items: break
        return output

    async def _evolve(self, sources: list[dict[str, str]], existing: str) -> str:
        llm = AsyncLLMClient(api_key=str(getattr(self.config, "dreamcycle_llm_api_key", "") or getattr(self.config, "llm_api_key", "")), base_url=str(getattr(self.config, "dreamcycle_llm_base_url", "") or getattr(self.config, "llm_api_base", "")), model=str(getattr(self.config, "dreamcycle_llm_model", "") or getattr(self.config, "llm_model_id", "")), max_tokens=16384, temperature=0.2)
        extract_prompt = str(getattr(self.config, "dreamcycle_extract_prompt", "") or DEFAULT_EXTRACT_PROMPT)
        consolidate_prompt = str(getattr(self.config, "dreamcycle_consolidate_prompt", "") or DEFAULT_CONSOLIDATE_PROMPT)
        candidates = await llm.chat([{"role": "system", "content": extract_prompt}, {"role": "user", "content": json.dumps(sources, ensure_ascii=False)}], trace_name="teamEvolver.dreamcycle.extract")
        return await llm.chat([{"role": "system", "content": consolidate_prompt}, {"role": "user", "content": f"## Existing team memory\n{existing}\n\n## Candidates\n{candidates}"}], trace_name="teamEvolver.dreamcycle.consolidate")

    def _run_cycle(self) -> None:
        if not self._lock.acquire(blocking=False): return
        try:
            endpoint = str(getattr(self.config, "sharing_viking_endpoint", "") or "").rstrip("/")
            team_key = str(getattr(self.config, "sharing_viking_team_api_key", "") or getattr(self.config, "sharing_viking_api_key", ""))
            _account, encoded_user = parse_openviking_key(team_key)
            team_user = encoded_user or str(getattr(self.config, "sharing_viking_user", "") or "default")
            max_items = max(1, int(getattr(self.config, "dreamcycle_max_source_items", 100) or 100))
            max_chars = max(1000, int(getattr(self.config, "dreamcycle_max_source_chars", 120000) or 120000))
            with httpx.Client(base_url=endpoint, timeout=30) as client:
                sources: list[dict[str, str]] = []
                for user, key in self._sources():
                    sources.extend(self._read_memories(client, user, key, max_items - len(sources), max_chars - sum(len(x["content"]) for x in sources)))
                    if len(sources) >= max_items: break
                if not sources:
                    self._last_run_at = datetime.now(timezone.utc).isoformat()
                    self._last_result = {"source_items": 0, "status": "no_personal_memory"}
                    self._last_error = ""
                    return
                target = f"viking://user/{team_user}/memories/teamEvolver/dreamcycle.md"
                try:
                    _result(client.post("/api/v1/fs/mkdir", headers=self._headers(team_key, team_user), json={"uri": f"viking://user/{team_user}/memories/teamEvolver", "description": "teamEvolver native memory evolution"}))
                except Exception:
                    pass
                existing = ""
                try:
                    value = _result(client.get("/api/v1/content/read", headers=self._headers(team_key, team_user), params={"uri": target, "offset": 0, "limit": -1, "raw": "true"}))
                    existing = str(value.get("content") or value.get("text") or "") if isinstance(value, dict) else str(value or "")
                except Exception: pass
                evolved = asyncio.run(self._evolve(sources, existing))
                if not evolved.strip():
                    raise RuntimeError("native memory evolution returned empty content")
                payload = {"uri": target, "content": evolved, "mode": "replace"}
                try: _result(client.post("/api/v1/content/write", headers=self._headers(team_key, team_user), json=payload))
                except Exception:
                    payload["mode"] = "create"; _result(client.post("/api/v1/content/write", headers=self._headers(team_key, team_user), json=payload))
            self._last_run_at = datetime.now(timezone.utc).isoformat()
            self._last_result = {"source_items": len(sources), "output_chars": len(evolved), "target": "team_memory:teamEvolver/dreamcycle.md"}
            self._last_error = ""
        except Exception as exc:
            self._last_error = str(exc)
            logger.exception("[DreamCycle] native memory evolution failed")
        finally:
            self._lock.release()


# Existing integrations importing the old class name now receive the complete
# five-job Scheduler/ReAct implementation.
from .dreamcycle_runtime import FullDreamCycleSupervisor

NativeDreamCycleSupervisor = FullDreamCycleSupervisor
