"""An in-memory OV protocol server behind the actual platform client interface."""

from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace

from starlette.exceptions import HTTPException

from tests.miner.skill_miner.support.ov_round_bridge import SOURCE, FakeOV


class PlatformRemote:
    def __init__(self):
        self.remote = FakeOV()
        self.remote.lookup_supported = True
        self.uploads = {}
        self.closed = False
        self.calls = []

    async def aclose(self):
        self.closed = True

    async def request(self, method, path, *, json_body=None, params=None, idempotency_key="", **kwargs):
        self.calls.append((method, path))
        try:
            data = await self._request(method, path, json_body, params, idempotency_key, kwargs)
            return SimpleNamespace(ok=True, status_code=200, error="", data=data)
        except HTTPException as exc:
            return SimpleNamespace(ok=False, status_code=exc.status_code, error=str(exc.detail), data=None)

    async def _request(self, method, path, body, params, key, options):
        if path == "/api/v1/resources/temp_upload":
            identifier = "upload-" + str(len(self.uploads))
            with zipfile.ZipFile(io.BytesIO(options["files"]["file"][1])) as archive:
                self.uploads[identifier] = {name: archive.read(name) for name in archive.namelist()}
            return {"temp_file_id": identifier}
        if path.startswith("/api/v1/skills/"):
            name = path.rsplit("/", 1)[-1]
            files = self.remote.compiler_assets.get(name) if hasattr(self.remote, "compiler_assets") else None
            if files is None:
                raise HTTPException(404, "NOT_FOUND")
            return {"root_uri": "viking://agent/skills/" + name,
                    "content": files["SKILL.md"].decode(),
                    "content_sha256": hashlib.sha256(files["SKILL.md"]).hexdigest(),
                    "files": [{"path": name, "sha256": hashlib.sha256(raw).hexdigest()}
                              for name, raw in files.items()]}
        if path == "/api/v1/skills":
            files = self.uploads[body["temp_file_id"]]
            name = files["SKILL.md"].decode().split("name: ", 1)[1].split("\n")[0]
            if not hasattr(self.remote, "compiler_assets"):
                self.remote.compiler_assets = {}
            self.remote.compiler_assets[name] = files
            return {"root_uri": "viking://agent/skills/" + name}
        if path == "/api/v1/fs/mkdir":
            self.remote.mkdirs.append(body["uri"])
            return {"ok": True}
        return await self.remote.request("default", method, path, json=body, params=params,
                                         extra_headers={"Idempotency-Key": key})

    def complete(self, job, text="第一轮未经内容验收的 Skill"):
        self.remote.content[job["output_uri"] + "/compiled_skill/lesson/SKILL.md"] = text
        self.remote.content[job["output_uri"] + "/compiled_skill/lesson/references/course.json"] = "原始资料字节"
        self.remote.tasks[job["ov_task_id"]] = {"status": "completed", "stage": "done"}


__all__ = ["PlatformRemote", "SOURCE"]
