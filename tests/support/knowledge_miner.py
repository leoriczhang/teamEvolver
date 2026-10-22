"""Stateful OpenViking transport for Miner API → Worker acceptance tests."""

from teamEvolver.openviking import OpenVikingResult


def worker(pg_env, ov):
    from team_miner.knowledge_miner.handlers import register_handlers
    from teamEvolver.runtime.services import build_services

    return build_services(role="worker", tx=pg_env.tx, handlers=register_handlers(),
                          extras={"miner_client_factory": lambda *_: ov})


def run_job(pg_env, ov):
    from teamEvolver.runtime.worker import process_once

    return pg_env.tx.pool.run(process_once(worker(pg_env, ov), owner_id="workspace-test"))


def bind(client, ov):
    client.app.state.services.extras["miner_client_factory"] = lambda *_: ov


class KnowledgeOV:
    def __init__(self):
        self.calls = []
        self.documents = {}
        self.directories = {"viking://resources"}
        self.task_status = "completed"
        self.task_id = "knowledge-upstream"
        self.submissions = []
        self.cancelled = []
        self.fail_writes = set()

    async def aclose(self):
        pass

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        params = kwargs.get("params") or {}
        body = kwargs.get("json_body") or {}
        if path == "/api/v1/fs/mkdir":
            target = body["uri"]
            if target in self.directories:
                return OpenVikingResult(False, 409, error="ALREADY_EXISTS")
            if target.rsplit("/", 1)[0] not in self.directories:
                return OpenVikingResult(False, 404, error="parent does not exist")
            self.directories.add(target)
            return OpenVikingResult(True, 200, {})
        if path == "/api/v1/compile/capabilities":
            return OpenVikingResult(True, 200, {"configured": True, "can_create": True})
        if path == "/api/v1/compile" and method == "POST":
            self.submissions.append(kwargs)
            return OpenVikingResult(True, 202, {"task_id": self.task_id, "status": "running"})
        if path.startswith("/api/v1/compile/submissions/"):
            return OpenVikingResult(True, 200, {"task_id": self.task_id, "status": "running"})
        if path == "/api/v1/tasks":
            return OpenVikingResult(True, 200, {"tasks": [
                {"task_id": self.task_id, "status": self.task_status},
                {"task_id": "other-tenant-task", "status": "completed"},
            ]})
        if path.endswith("/cancel"):
            self.cancelled.append(path.split("/")[-2])
            self.task_status = "cancelled"
            return OpenVikingResult(True, 200, {"task_id": self.task_id, "status": "cancelled"})
        if path.startswith("/api/v1/tasks/"):
            return OpenVikingResult(True, 200, {"task_id": self.task_id, "status": self.task_status})
        if path.startswith("/api/v1/skills/"):
            return OpenVikingResult(True, 200, {"content": "Skill", "revision": "one"})
        if path == "/api/v1/fs/tree":
            root = params["uri"]
            entries = [{"uri": key, "name": key.rsplit("/", 1)[-1], "isDir": False}
                       for key in sorted(self.documents) if key.startswith(root + "/")]
            return OpenVikingResult(True, 200, entries)
        if path == "/api/v1/content/read":
            target = params["uri"]
            if target not in self.documents:
                return OpenVikingResult(False, 404, error="not found")
            return OpenVikingResult(True, 200, self.documents[target])
        if path == "/api/v1/content/write":
            if body["uri"].rsplit("/", 1)[0] not in self.directories:
                return OpenVikingResult(False, 404, error="parent does not exist")
            if body["uri"] in self.fail_writes:
                return OpenVikingResult(False, 503, error="write unavailable")
            self.documents[body["uri"]] = body["content"]
            return OpenVikingResult(True, 200, {
                "uri": body["uri"], "content_updated": True,
                "semantic_status": "queued", "vector_status": "queued", "queue_status": None,
            })
        if path == "/api/v1/fs/stat":
            target = params["uri"]
            if target in self.documents or target in self.directories:
                return OpenVikingResult(True, 200, {"uri": target, "isDir": target in self.directories})
            return OpenVikingResult(False, 404, error="not found")
        if path == "/api/v1/fs" and method == "DELETE":
            target = params["uri"]
            if target not in self.documents and target not in self.directories:
                return OpenVikingResult(False, 404, error="not found")
            self.documents = {key: value for key, value in self.documents.items()
                              if key != target and not key.startswith(target + "/")}
            self.directories = {key for key in self.directories
                                if key != target and not key.startswith(target + "/")}
            return OpenVikingResult(True, 200, {
                "uri": target, "estimated_deleted_count": 1,
                "semantic_status": "queued",
            })
        return OpenVikingResult(True, 200, {})
