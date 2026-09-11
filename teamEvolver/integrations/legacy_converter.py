"""Compatibility boundary for trusted skill-opt converter source.

Import translation is module-local, never a process-wide sys.modules shim.
This is NOT a Python sandbox: only service administrators may install code.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import re
import threading
from collections import defaultdict
from types import SimpleNamespace

from . import legacy_parser
from .langfuse_client import LangfuseClient, LangfuseError
from .langfuse_convert import convert_langfuse_session

HOOKS = {"query_meta", "fetch_detail", "filter_meta", "extract_fields", "convert", "dedup"}
MODULES = {
    "re",
    "json",
    "datetime",
    "collections",
    "typing",
    "hashlib",
    "math",
    "itertools",
    "functools",
    "decimal",
    "statistics",
    "base64",
}
CORE_EXPORTS = {
    "core.langfuse_client": {"langfuse_to_template", "parse_llm_output", "parse_tool_output", "format_timestamp"},
    "core.source_default": {
        "default_query_meta",
        "default_fetch_detail",
        "default_filter_meta",
        "default_extract_fields",
        "default_dedup",
        "default_convert",
        "_session_kind",
        "_extract_first_text",
    },
}


def inspect_converter(source: str) -> dict:
    report = {"sha256": hashlib.sha256(source.encode()).hexdigest(), "hooks": [], "issues": []}
    if len(source.encode()) > 256 * 1024:
        report["issues"].append("converter exceeds 256 KiB")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        report["issues"].append(f"syntax error at line {exc.lineno}: {exc.msg}")
        report["status"] = "blocked"
        return report
    report["hooks"] = sorted({node.name for node in tree.body if isinstance(node, ast.FunctionDef)} & HOOKS)
    if not report["hooks"]:
        report["issues"].append("no supported converter hooks")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in MODULES:
                    report["issues"].append(f"port import {alias.name} before migration")
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in CORE_EXPORTS:
                unknown = {alias.name for alias in node.names} - CORE_EXPORTS[module]
                if unknown:
                    report["issues"].append(f"unsupported {module} symbols: {', '.join(sorted(unknown))}")
            elif module == "core" and all(alias.name in {"source_default", "langfuse_client"} for alias in node.names):
                pass
            elif node.level or module.split(".")[0] not in MODULES:
                report["issues"].append(f"port import {module} before migration")
    report["status"] = "blocked" if report["issues"] else "compatible"
    return report


def compile_converter(source: str):
    report = inspect_converter(source)
    if report["issues"]:
        raise ValueError("; ".join(report["issues"]))
    parser = SimpleNamespace(**{name: getattr(legacy_parser, name) for name in CORE_EXPORTS["core.langfuse_client"]})
    defaults = SimpleNamespace(
        **{
            name: getattr(legacy_parser, name)
            for name in CORE_EXPORTS["core.source_default"]
            if hasattr(legacy_parser, name)
        }
    )
    defaults.default_query_meta = lambda ctx: ctx.source.default_query_meta(ctx)
    defaults.default_fetch_detail = lambda tid, ctx: ctx.source.default_fetch_detail(tid, ctx)

    def import_compat(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "core.langfuse_client":
            return parser
        if name == "core.source_default":
            return defaults
        if name == "core":
            return SimpleNamespace(langfuse_client=parser, source_default=defaults)
        if level or name.split(".")[0] not in MODULES:
            raise ImportError(f"unsupported converter dependency: {name}")
        return builtins.__import__(name, globals, locals, fromlist, level)

    namespace = {
        "__name__": "te_converter_" + report["sha256"],
        "__builtins__": {**vars(builtins), "__import__": import_compat},
    }
    exec(compile(source, "<customer-converter>", "exec"), namespace)
    return SimpleNamespace(**{hook: namespace.get(hook, getattr(defaults, "default_" + hook, None)) for hook in HOOKS})


def _text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if value is not None else ""


def conversion_to_turn(conv: dict, trace: dict, base: dict) -> dict:
    if not isinstance(conv, dict) or not isinstance(conv.get("conversions"), list):
        raise ValueError("convert must return an object containing conversions[]")
    messages, calls, results, prompts, responses, skills = [], [], [], [], [], []
    observations = {str(obs.get("id")): obs for obs in trace.get("observations") or []}
    for index, entry in enumerate(conv["conversions"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("content"), list):
            raise ValueError("invalid conversion entry")
        role = entry.get("role")
        if role not in {"user", "gpt", "assistant", "tool_result", "system"}:
            raise ValueError(f"unsupported conversion role: {role}")
        text = "\n".join(
            _text(item.get("value"))
            for item in entry["content"]
            if isinstance(item, dict) and item.get("type") == "text"
        )
        obs_id = str(entry.get("observationId") or f"{trace.get('id', '')}-{index}")
        entry_calls = []
        for item in entry["content"]:
            if not isinstance(item, dict):
                raise ValueError("conversion content entries must be objects")
            if item.get("type") == "toolCall":
                used_ids = {call["id"] for call in calls}
                call_id = str(entry.get("observationId") or "")
                if not call_id:
                    call_id = next(
                        (
                            oid
                            for oid, obs in observations.items()
                            if oid not in used_ids
                            and str(obs.get("name") or "").removeprefix("tool:").strip() == item.get("name")
                        ),
                        "",
                    )
                if not call_id or call_id in used_ids:
                    call_id = f"{obs_id}-{len(entry_calls)}"
                call = {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": str(item.get("name") or ""),
                        "arguments": _text(item.get("arguments") or {}),
                    },
                }
                calls.append(call)
                entry_calls.append(call)
        if role == "user":
            prompts.append(text)
        elif role in {"gpt", "assistant"} and text:
            responses.append(text)
        message = {"role": "assistant" if role == "gpt" else "tool" if role == "tool_result" else role, "content": text}
        if entry_calls:
            message["tool_calls"] = entry_calls
        if role == "tool_result":
            observation = observations.get(obs_id, {})
            name = str(observation.get("name") or "").removeprefix("tool:").strip()
            if not any(call["id"] == obs_id for call in calls):
                calls.append(
                    {
                        "id": obs_id,
                        "type": "function",
                        "function": {"name": name, "arguments": _text(observation.get("input") or {})},
                    }
                )
            message["tool_call_id"] = obs_id
            results.append(
                {
                    "tool_call_id": obs_id,
                    "observation_id": obs_id,
                    "tool_name": name,
                    "content": text,
                    "has_error": observation.get("level") == "ERROR"
                    or bool(
                        (observation.get("output") or {}).get("isError")
                        if isinstance(observation.get("output"), dict)
                        else False
                    ),
                }
            )
        messages.append(message)
    for call in calls:
        arguments = call["function"]["arguments"]
        skills.extend(re.findall(r"skills/([A-Za-z0-9_.-]+)/", arguments))
        if call["function"]["name"] in {"read_skill", "execute_skill"}:
            try:
                data = json.loads(arguments)
                name = data.get("skill_name") or data.get("skillName") or data.get("name")
                if name:
                    skills.append(str(name))
            except (ValueError, AttributeError):
                pass
    return {
        **base,
        "prompt_text": "\n".join(prompts),
        "response_text": "\n".join(responses),
        "messages": messages,
        "tool_calls": calls,
        "tool_results": results,
        "used_skills": list(dict.fromkeys(skills)),
        "read_skills": list(dict.fromkeys(skills)),
        "legacy_conversions": conv["conversions"],
        "legacy_scores": conv.get("scores") or [],
        "metrics": {**base.get("metrics", {}), "tool_call_count": len(calls)},
    }


class LegacyConverterSource:
    source_type = "skillopt"

    def __init__(self, config):
        self.config = config
        self.code = config.datasource_legacy_converter_code
        self.hooks = compile_converter(self.code)
        self.meta = {}
        self.sessions = {}
        self.ctx = None
        self._local = threading.local()
        self._clients = []
        self._client_lock = threading.Lock()

    def _client(self):
        client = getattr(self._local, "client", None)
        if client is None:
            client = LangfuseClient.from_config(self.config)
            self._local.client = client
            with self._client_lock:
                self._clients.append(client)
        return client

    def close(self):
        for client in self._clients:
            client.close()
        self._clients.clear()

    def health(self):
        return self._client().health()

    def list_session_ids(self, filters, *, max_sessions):
        options = self.config.datasource_legacy_options or {}
        names = [name.strip() for name in str(filters.get("trace_name") or "").split(",") if name.strip()]
        source = self

        def group_sessions(rows):
            grouped = defaultdict(list)
            for tid, meta in sorted(rows.items(), key=lambda item: str(item[1].get("timestamp") or "")):
                grouped[str(meta.get("sessionId") or "__nosession__/" + tid)].append(tid)
            return dict(grouped)

        def existing_sessions():
            from ..session_store import SessionStore
            from ..tenants.registry import current_tenant_id

            rows = SessionStore.from_config(source.config, current_tenant_id()).load_index_rows()
            return {str(row["session_id"]) for row in rows if row.get("session_id")}

        self.ctx = SimpleNamespace(
            source=self,
            adapter=self.hooks,
            project=self.config.datasource_legacy_project,
            config=options,
            filters=filters,
            names=names,
            trace_name=filters.get("trace_name", ""),
            date_from=legacy_parser._parse_any_ts(filters.get("from_timestamp")),
            date_to=legacy_parser._parse_any_ts(filters.get("to_timestamp")),
            limit=max_sessions,
            all_sessions=bool(options.get("all_sessions", False)),
            cron_dedup=bool(options.get("cron_dedup", False)),
            doris_project_id="",
            retries=3,
            run_dir=None,
            out_dir=None,
            only_tid=options.get("only_tid"),
            lf_cfg={
                "host": self.config.langfuse_host,
                "public_key": self.config.langfuse_public_key,
                "secret_key": self.config.langfuse_secret_key,
            },
            langfuse=self._client,
            group_sessions=group_sessions,
            load_existing_sessions=existing_sessions,
        )
        rows = self.hooks.query_meta(self.ctx)
        if not isinstance(rows, dict):
            raise LangfuseError("query_meta must return a trace-id mapping")
        max_traces = min(50000, max(1, int(options.get("max_traces", 10000))))
        if len(rows) > max_traces:
            raise LangfuseError("converter trace limit exceeded; narrow the time window")
        rows = {
            str(tid): dict(meta)
            for tid, meta in rows.items()
            if (not names or not meta.get("name") or meta["name"] in names)
            and (not self.ctx.only_tid or str(tid) == self.ctx.only_tid)
        }
        original = set(rows)
        for hook in (self.hooks.filter_meta, self.hooks.dedup):
            rows = hook(rows, self.ctx)
            if not isinstance(rows, dict) or not set(rows).issubset(original):
                raise LangfuseError("filter_meta/dedup must return a subset of the selected traces")
        self.meta = rows
        self.sessions = group_sessions(rows)
        return list(self.sessions)[:max_sessions]

    def default_query_meta(self, ctx):
        cap = min(50000, max(1, int(ctx.config.get("max_traces", 10000))))
        rows = {}
        for name in ctx.names or [""]:
            traces = self._client().iter_traces(
                name=name,
                session_id=ctx.filters.get("session_id", ""),
                from_timestamp=ctx.filters.get("from_timestamp", ""),
                to_timestamp=ctx.filters.get("to_timestamp", ""),
                user_id=ctx.filters.get("user_id", ""),
                tags=ctx.filters.get("tags"),
                environment=ctx.filters.get("environment"),
                metadata=ctx.filters.get("metadata"),
                release=ctx.filters.get("release", ""),
                version=ctx.filters.get("version", ""),
                max_items=cap + 1,
            )
            for trace in traces:
                meta = {key: trace.get(key) for key in ("sessionId", "timestamp", "name", "userId")}
                meta.update(self.hooks.extract_fields(trace.get("input"), meta, ctx))
                rows[str(trace["id"])] = meta
                if len(rows) > cap:
                    raise LangfuseError("trace scan exceeds max_traces; narrow the time window")
        return rows

    def default_fetch_detail(self, tid, ctx):
        trace = self._client().get_trace(tid)
        return {"trace": trace, "observations": trace.get("observations") or []}

    def fetch_session(self, session_id):
        traces = []
        for tid in self.sessions[session_id]:
            raw = self.hooks.fetch_detail(tid, self.ctx)
            if not isinstance(raw, dict) or not isinstance(raw.get("trace"), dict):
                raise LangfuseError(f"fetch_detail did not return trace data: {tid}")
            trace = {**raw["trace"], "observations": raw.get("observations") or []}
            if self.meta[tid].get("emp_id"):
                trace["userId"] = self.meta[tid]["emp_id"]
            traces.append(trace)
        return {"id": session_id}, traces

    def convert_session(self, session, traces):
        def mapper(trace, observations, turn_num, base):
            conv = self.hooks.convert({"trace": trace, "observations": observations})
            return conversion_to_turn(conv, trace, base)

        converted = convert_langfuse_session(session, traces, mapper=mapper)
        converted["legacy_converter"] = {
            "project": self.config.datasource_legacy_project,
            "sha256": hashlib.sha256(self.code.encode()).hexdigest(),
        }
        from .langfuse_mapper import build_mapper_registry

        registry = build_mapper_registry(self.config)
        return registry.apply_session_hooks(converted, session, traces) if registry else converted


def _preview_worker():
    import contextlib
    import io
    import sys

    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
    except ImportError:
        pass
    payload = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            hooks = compile_converter(str(payload.get("code") or ""))
            raw = payload.get("raw") or {}
            conv = hooks.convert(raw)
            from .langfuse_convert import convert_trace_to_turn

            trace = {**raw.get("trace", {}), "observations": raw.get("observations", [])}
            turn = conversion_to_turn(conv, trace, convert_trace_to_turn(trace, 1))
        print(json.dumps({"conversion": conv, "turn": turn}, ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:500]}"}))


if __name__ == "__main__":
    _preview_worker()
