"""Scripted async Compile/content client for Memory and Miner tests."""

from teamEvolver.openviking import OpenVikingResult


class OV:
    def __init__(self):
        self.calls, self.submits = [], []
        self.status = "completed"
        self.block = None

    async def aclose(self):
        pass

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/v1/compile":
            self.submits.append(kwargs)
            return OpenVikingResult(True, 202, {"task_id": "upstream", "status": "running"})
        if path.startswith("/api/v1/compile/submissions/"):
            return OpenVikingResult(True, 200, {"task_id": "upstream", "status": "running"})
        if path.startswith("/api/v1/tasks/"):
            if path.endswith("/cancel"):
                self.status = "cancelled"
                return OpenVikingResult(True, 200, {"task_id": "upstream", "status": "running"})
            if self.block:
                await self.block.wait()
            return OpenVikingResult(True, 200, {"task_id": "upstream", "status": self.status})
        if path.startswith("/api/v1/skills/"):
            return OpenVikingResult(True, 200, {"content": "Skill", "revision": "one"})
        if path == "/api/v1/fs/tree":
            root = kwargs["params"]["uri"]
            return OpenVikingResult(True, 200, [{"uri": root + "/profile.md", "modTime": "now"}])
        if path == "/api/v1/content/read":
            return OpenVikingResult(True, 200, "Fact")
        return OpenVikingResult(True, 200, {})
