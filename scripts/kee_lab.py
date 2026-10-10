#!/usr/bin/env python3
"""Loopback-only lab: real TE console + temporary PG, generic OV and model HTTP stubs.

No production configuration/environment is loaded. All credentials below are lab-only.
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("kee_lab_fixtures", ROOT / "tests/test_kee_r6.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def mock_app():
    ov = fixtures.GenericOV()
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def model(request: Request):
        body = await request.json()
        source = json.loads(body["messages"][-1]["content"])["sources"][0]
        pack = fixtures.sample(source)
        return {
            "id": "lab-model-stub",
            "object": "chat.completion",
            "created": 0,
            "model": "lab-stub",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(pack, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
        }

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def route(request: Request, path: str):
        native = httpx.Request(
            request.method, str(request.url), headers=dict(request.headers), content=await request.body()
        )
        try:
            result = ov.respond(native)
        except AssertionError:
            result = httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return Response(result.content, result.status_code, media_type="application/json")

    return app


def console():
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.proxy.server import ProxyServer

    os.environ["TEAMEVOLVER_ROOT_API_KEY"] = "kee-lab-root-only"
    cfg = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn=os.environ["KEE_R6_TEST_DSN"],
        storage_pg_schema="kee_console",
        proxy_host="127.0.0.1",
        proxy_port=52010,
        self_registration_enabled=False,
        sharing_viking_endpoint="http://127.0.0.1:52012",
        sharing_viking_account="lab",
        sharing_viking_user="reviewer",
        sharing_viking_api_key="lab-key",
        ontology={
            "enabled": True,
            "state_dir": "/tmp/kee-r6-lab/ontology",
            "allow_fixture": True,
            "kee_query_enabled": True,
        },
        llm_api_base="http://127.0.0.1:52012/v1",
        llm_api_key="lab-model-key",
        llm_model_id="lab-stub",
        skills_runtime_cache_dir="/tmp/kee-r6-lab/skills",
    )
    return ProxyServer(config=cfg).app


async def initialize():
    from teamEvolver.storage.pg_pool import init_pg_schema

    c = await asyncpg.connect(os.environ["KEE_R6_TEST_DSN"])
    await init_pg_schema(c, "kee_console")
    await c.close()


if __name__ == "__main__":
    kind = sys.argv[1]
    if kind == "init":
        asyncio.run(initialize())
    else:
        pid = Path(f"/tmp/te_kee_r6_{kind}.pid")
        pid.write_text(str(os.getpid()))
        try:
            uvicorn.run(
                mock_app() if kind == "mock" else console(), host="127.0.0.1", port=52012 if kind == "mock" else 52010
            )
        finally:
            pid.unlink(missing_ok=True)
