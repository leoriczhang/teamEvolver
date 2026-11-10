"""Frozen input factories and a restartable fake host for dialogue protocol tests."""

import copy
import json
from types import SimpleNamespace

from starlette.exceptions import HTTPException

from team_miner.skill_miner.dialogue_dimensions.compiler import SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.input import canonical, case_key, normalize_case, parse_model, sha


def case(text="HWP：您好。您好。金额1.25元！\r\nC：好，那个个……。\n", number=1):
    return normalize_case({"case_ref": "case_" + sha(str(number))[:24], "course": "课程", "chapter": "章节",
                           "knowledge_point": "知识点", "scene": "场景", "dialogue_text": text})


def finding(value, **fields):
    return canonical({"schema_version": "taikang.dialogue-findings.v1", "case_ref": value["case_ref"],
                      "case_version": value["case_version"], "analysis_status": "analyzed",
                      "observations": [{"label": "开场", "behavior": "向客户问候", "sentence_refs": ["s000001"]}],
                      **fields})


class MemoryJournal:
    def __init__(self, *_):
        self.values = {}

    async def get(self, key):
        return copy.deepcopy(self.values.get(key))

    async def save(self, **objects):
        self.values.update(copy.deepcopy(objects))


class Admission:
    def __init__(self):
        self.live = {}

    async def acquire(self, job, current):
        self.live.setdefault(current["key"], "")
        return True

    async def mark(self, current):
        self.live[current["key"]] = current["task_id"]

    async def release(self, task_id="", key=""):
        for identity in list(self.live):
            if identity == key or self.live[identity] == task_id:
                self.live.pop(identity)


class Host:
    def __init__(self, value):
        self.tenant = "tenant"
        self.job = value
        self.admission = Admission()
        self.remote = {}
        self.posts = {}
        self.lose_response = False
        self.requests = []
        self.omit_cases = set()
        self.repository = SimpleNamespace(update_job=self.update, get_job=lambda *_: copy.deepcopy(self.job))

    def _ov_skill_compiler_body(self):
        return SKILL_BODY

    def update(self, _, **fields):
        self.job.update(fields)
        return copy.deepcopy(self.job)

    async def _knowledge_request(self, account, method, path, **kwargs):
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[-1]
            if key not in self.posts:
                raise HTTPException(404)
            return {"task_id": self.posts[key]}
        if path == "/api/v1/compile":
            key = kwargs["extra_headers"]["Idempotency-Key"]
            self.posts.setdefault(key, "task-" + str(len(self.posts) + 1))
            payload = kwargs["json"]
            self.requests.append(copy.deepcopy(payload))
            for uri in payload["from"]:
                text = self.remote[uri]
                if "stage=extract;" in payload["instruction"]:
                    value = parse_model(text)
                    if case_key(value) in self.omit_cases:
                        continue
                    relative = f"analysis/dialogue_dimensions/receipts/{value['case_ref']}-{value['case_version']}.json"
                    self.remote[payload["to"] + "/" + relative] = finding(value)
                else:
                    window = json.loads(text.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
                    self.remote[payload["to"] + "/" + window["output_path"]] = canonical({
                        "window_id": window["window_id"], "kept_separate": [
                            {"member_ref": m["member_ref"], "reason": "独立保留"} for m in window["members"]]})
            if self.lose_response:
                self.lose_response = False
                raise ConnectionError("lost acknowledgement")
            return {"task_id": self.posts[key]}
        if path.startswith("/api/v1/tasks/") and method == "GET":
            return {"task_id": path.rsplit("/", 1)[-1], "status": "completed"}
        if path == "/api/v1/content/read":
            uri = kwargs["params"]["uri"]
            if uri not in self.remote:
                raise HTTPException(404)
            return {"content": self.remote[uri]}
        raise AssertionError(path)
