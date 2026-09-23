"""Disposable Team Memory UI preview; no production config or upstream I/O."""

import argparse
import os
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from team_memory.maintenance.maintenance_skill import DEFAULT_MAINTENANCE_SKILL_BODY
from team_memory.aggregation.okf_skill import DEFAULT_OKF_SKILL_BODY


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=52016)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="te-memory-preview-") as temp:
        app = FastAPI()
        settings = {
            "enabled": True,
            "shared_knowledge_prefix": "shared-knowledge",
            "target_root": "viking://resources/shared-knowledge",
            "okf_skill_uri": "viking://agent/skills/team-memory-okf",
            "maintenance_skill_uri": "viking://agent/skills/team-memory-maintenance",
        }
        skills = {"aggregation": DEFAULT_OKF_SKILL_BODY, "maintenance": DEFAULT_MAINTENANCE_SKILL_BODY}
        runs = []

        @app.get("/api/auth/status")
        async def auth():
            return {"authenticated": True, "user": {"id": "preview", "display_name": "Local Preview", "role": "admin"}}

        @app.get("/api/sharing-config")
        async def sharing():
            return {"enabled": True, "endpoint": "https://preview.invalid", "service_api_key_present": True}

        @app.get("/api/aggregation/settings")
        async def get_settings():
            return settings

        @app.post("/api/aggregation/settings")
        async def save_settings(request: Request):
            settings.update(await request.json())
            return settings

        @app.get("/api/aggregation/okf-skill")
        async def get_skill(stage: str = "aggregation"):
            return {
                "body": skills[stage],
                "skill_uri": settings["okf_skill_uri" if stage == "aggregation" else "maintenance_skill_uri"],
                "revision": "preview",
            }

        @app.put("/api/aggregation/okf-skill")
        async def save_skill(request: Request, stage: str = "aggregation"):
            skills[stage] = (await request.json())["body"]
            return await get_skill(stage)

        @app.post("/api/aggregation/users")
        async def users():
            return {"users": ["alice", "bob", "carol"]}

        @app.get("/api/aggregation/runs")
        async def list_runs():
            return {"runs": runs}

        @app.post("/api/aggregation/run")
        async def run(request: Request):
            body = await request.json()
            record = {
                "task_id": "preview-run",
                "status": "pending",
                "stage": "pending",
                "groups": [],
                "account_id": body.get("account_id") or "default",
                **body,
            }
            runs.insert(0, record)
            return record

        @app.get("/api/aggregation/status/{task_id}")
        async def status(task_id: str):
            run = runs[0]
            run.update(
                status="completed",
                stage="completed",
                snapshot_uri="viking://user/team/resources/preview/snapshot",
                groups=[
                    {"group_key": key, "status": "ok", "detail": "preview"}
                    for key in ("aggregation", "snapshot", "maintenance")
                ],
            )
            return run

        @app.get("/")
        async def index():
            return FileResponse(root / "teamEvolver/web/dist/index.html")

        app.mount("/assets", StaticFiles(directory=root / "teamEvolver/web/dist/assets"))
        Path("/tmp/te_team_memory_preview.pid").write_text(str(os.getpid()))
        uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
