"""Compile Port over the existing job lease, with subprocess isolation and bounded shared model dispatch."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from teamEvolver.llm import _call_in_pool, _dispatcher_for

from .contracts import ProcedurePack, canonical, digest, verify_evidence

# Additional global ceiling for KEE workers; existing host dispatcher supplies queue/backpressure.
SLOTS = threading.BoundedSemaphore(8)
SKILL = Path(__file__).with_name("compile-skill.md")


class CompilePort(Protocol):
    async def compile(self, job, sources, model): ...
    async def cancel(self, job): ...


def model_call(payload):
    with SLOTS:
        child = subprocess.Popen(
            [sys.executable, "-m", "team_ontology.kee.worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PATH",
                    "PYTHONPATH",
                    "LANG",
                    "LC_ALL",
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "NO_PROXY",
                    "http_proxy",
                    "https_proxy",
                    "no_proxy",
                    "SSL_CERT_FILE",
                }
            },
        )
        pid_file = Path(f"/tmp/te_kee_compile_{child.pid}.pid")
        pid_file.write_text(str(child.pid))
        try:
            output, _ = child.communicate(canonical(payload), timeout=70)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise RuntimeError("COMPILE_MODEL_DEADLINE") from None
        finally:
            pid_file.unlink(missing_ok=True)
        if len(output.encode()) > 2 * 1024 * 1024:
            raise RuntimeError("COMPILE_OUTPUT_LIMIT")
        value = json.loads(output)
        if child.returncode or "error" in value:
            raise HTTPException(502, "COMPILE_MODEL_FAILED_" + str(value.get("error", "INVALID_OUTPUT"))[:60])
        return value


class InternalCompile:
    executor = "kee.internal.v1"

    def __init__(self, ops):
        self.ops = ops

    async def compile(self, job, sources, model):
        request = job["request"]
        result = dict(job["result"])
        groups, group, size = [], [], 0
        for source in sources:
            if len(source["text"]) > 80000:
                if group:
                    groups.append(group)
                    group, size = [], 0
                for offset in range(0, len(source["text"]), 80000):
                    groups.append(
                        [
                            {
                                **{k: source[k] for k in ("source_id", "uri", "revision", "digest")},
                                "text": source["text"][offset : offset + 80000],
                                "unicode_offset": offset,
                            }
                        ]
                    )
                continue
            if group and size + len(source["text"]) > 80000:
                groups.append(group)
                group, size = [], 0
            group.append({k: source[k] for k in ("source_id", "uri", "revision", "digest", "text")})
            size += len(source["text"])
        if group:
            groups.append(group)
        if len(groups) > self.ops.config.kee_max_model_calls:
            raise HTTPException(422, "COMPILE_MODEL_CALL_BUDGET")
        if request.get("fixture") is None and not all(model.get(k) for k in ("model", "base_url", "api_key")):
            raise HTTPException(503, "COMPILE_MODEL_CONFIGURATION_REQUIRED")
        if request.get("fixture") is not None:
            if not self.ops.config.allow_fixture:
                raise HTTPException(403, "FIXTURE_DISABLED")
            pack = ProcedurePack.model_validate(request["fixture"])
            verify_evidence(pack, sources)
            return pack.model_dump(mode="json"), {"calls": 0, "fixture": True}
        input_digest = digest(
            {
                "sources": sources,
                "clarification": request.get("clarification", ""),
                "skill": SKILL.read_text(),
                "model": {k: v for k, v in model.items() if k != "api_key"},
            }
        )
        cached = result.get("compile", {})
        if cached.get("input_digest") != input_digest:
            cached = {
                "input_digest": input_digest,
                "batches": [],
                "usage": {},
                "executor": self.executor,
                "started": time.time(),
            }
        for index in range(len(cached["batches"]), len(groups)):
            p = {"tenant": job["tenant"], "subject": job["subject"]}
            active = await self.ops.store.get(p, job["id"])
            if active["state"] != "running" or active["attempt"] != job["attempt"]:
                raise HTTPException(409, "COMPILE_CANCELLED_OR_FENCED")
            if time.time() - cached["started"] > self.ops.config.kee_compile_deadline_seconds:
                raise HTTPException(408, "COMPILE_DEADLINE_EXCEEDED")
            attempts = cached.setdefault("attempts", {})
            batch_key = str(index)
            if attempts.get(batch_key, 0) >= 2 or sum(attempts.values()) >= self.ops.config.kee_max_model_calls:
                raise HTTPException(409, "COMPILE_MODEL_RETRY_BUDGET_EXHAUSTED")
            attempts[batch_key] = attempts.get(batch_key, 0) + 1
            result["compile"] = cached
            job = await self.ops.store.checkpoint(job, result, "running")
            response = await _call_in_pool(
                model_call,
                dispatcher=_dispatcher_for(job["tenant"], 8, 64),
                payload={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SKILL.read_text()},
                        {
                            "role": "user",
                            "content": canonical(
                                {"sources": groups[index], "clarification": request.get("clarification", "")}
                            ),
                        },
                    ],
                },
            )
            draft = ProcedurePack.model_validate_json(response["content"])
            verify_evidence(draft, sources)
            cached["batches"].append(draft.model_dump(mode="json"))
            for key, value in response["usage"].items():
                cached["usage"][key] = cached["usage"].get(key, 0) + value
            result["compile"] = cached
            job = await self.ops.store.checkpoint(job, result, "running")
        methods, schema, gaps, facts = [], {}, [], []
        for batch_index, batch in enumerate(cached["batches"]):
            mapping = {
                m["method_id"]: "m_" + digest({"batch": batch_index, "id": m["method_id"]})[:32]
                for m in batch["methods"]
            }
            for method in batch["methods"]:
                methods.append(
                    {
                        **method,
                        "method_id": mapping[method["method_id"]],
                        "related_methods": [mapping[key] for key in method["related_methods"]],
                    }
                )
            for key, value in batch["schema_body"].items():
                if key in schema and schema[key] != value:
                    gaps.append("SCHEMA_CONFLICT:" + key)
                else:
                    schema[key] = value
            gaps.extend(batch["gaps"])
            facts.extend(batch["facts"])
        merged = ProcedurePack(
            pack_id=request["pack_id"],
            version=input_digest[:16],
            schema_revision="proposal-" + digest(schema)[:16],
            schema_body=schema,
            methods=methods,
            gaps=gaps,
            facts=facts,
        )
        return merged.model_dump(mode="json"), {
            "calls": len(cached["batches"]),
            "usage": cached["usage"],
            "executor": self.executor,
        }

    async def cancel(self, job):
        # Model subprocess is bounded to 70s. Existing attempt fence rejects any late output.
        return {"fenced": True, "executor": self.executor}
