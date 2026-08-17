from __future__ import annotations

import hashlib
from types import SimpleNamespace

from teamEvolver.dreamcycle.memory_replay import MemoryTrueReplayRunner


class _Ledger:
    def __init__(self, *, action: str = "update") -> None:
        self.action = action
        self.saved: list[dict] = []

    def load_change(self, change_id: str):
        return {
            "change_id": change_id,
            "action": self.action,
            "snapshot_status": "complete",
            "before_oid": "a" * 40,
            "after_oid": "b" * 40,
            "before_path": "viking://user/memories/pattern/demo.md",
            "after_path": (
                "viking://user/memories/_archived/pattern/demo.md"
                if self.action == "archive"
                else "viking://user/memories/pattern/demo.md"
            ),
            "before_exists": True,
            "after_exists": True,
            "before_hash": hashlib.sha256(b"old memory").hexdigest(),
            "after_hash": hashlib.sha256(b"new memory").hexdigest(),
        }

    def read_snapshot_text(self, *, oid: str, path: str) -> str:
        del path
        return "old memory" if oid.startswith("a") else "new memory"

    def save_replay(self, payload):
        replay = dict(payload)
        replay["record_key"] = "memory-replays/test/result.json"
        self.saved.append(replay)
        return replay

    def list_replays(self, *, change_id: str = "", limit: int = 100):
        del change_id, limit
        return list(reversed(self.saved))


class _Sessions:
    session = {
        "session_id": "session-1",
        "runtime": {
            "type": "agentshub",
            "integration_id": "agentshub:tenant-a",
            "replay_endpoint": "http://127.0.0.1:9999/replay",
        },
        "runtime_context": {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "model_config_id": "model-a",
        },
        "turns": [{"turn_num": 1, "context_usage": {}}],
    }

    def load_session(self, session_id: str):
        return dict(self.session) if session_id == "session-1" else None

    def list_conversations(self, *, limit: int = 100):
        del limit
        return [{"session_id": "session-1"}]


def _branch_runner(
    branch,
    instruction,
    branch_skill,
    job,
    case,
    source_session,
    timeout,
    max_interactions,
):
    del instruction, branch_skill, job, source_session, timeout, max_interactions
    items = list(case["context_snapshot"].get("items") or [])
    treatment = next(
        (item for item in items if item.get("scope") == "team_memory"),
        None,
    )
    content = (
        str((treatment or {}).get("expanded", {}).get("full") or "")
    )
    candidate = branch == "candidate"
    assert content == ("new memory" if candidate else "old memory")
    return {
        "branch": branch,
        "runtime": "agentshub",
        "ok": True,
        "final_response": "done",
        "messages": [],
        "interaction_turns": 1 if candidate else 2,
        "tool_call_count": 1,
        "total_tokens": 100 if candidate else 140,
        "input_tokens": 80,
        "output_tokens": 20,
        "interactions": [],
        "context_input_hash": hashlib.sha256(content.encode()).hexdigest(),
        "checklist_report": {
            "items": [
                {"id": "M01", "satisfied": True, "evidence": "done"}
            ],
            "all_satisfied": True,
        },
        "safety_report": {"external_side_effects": "blocked"},
    }


def test_memory_true_replay_runs_before_and_after_as_only_treatment() -> None:
    ledger = _Ledger()
    runner = MemoryTrueReplayRunner(
        ledger=ledger,
        app_config=SimpleNamespace(),
        branch_runner=_branch_runner,
        session_store=_Sessions(),
    )

    result = runner.run(
        change_id="mch-test",
        query="Complete the task.",
        checklist=["Task is complete"],
        source_session_id="session-1",
        max_interactions=4,
    )

    assert result["status"] == "evaluated"
    assert result["verdict"] == "accept"
    assert result["accepted"] is True
    assert result["efficiency"]["dimensions"]["interaction_turns"] == {
        "baseline": 2,
        "candidate": 1,
        "delta": 1,
        "reduction_ratio": 0.5,
        "winner": "candidate",
    }
    assert result["treatment"]["before_hash"] != result["treatment"]["after_hash"]
    assert result["cases"][0]["baseline"]["context_input_hash"] != (
        result["cases"][0]["candidate"]["context_input_hash"]
    )
    assert ledger.saved[0]["schema_version"] == (
        "teamevolver.memory-true-replay.v1"
    )


def test_archive_replay_removes_memory_from_candidate_context() -> None:
    captured: dict[str, list] = {}

    def branch_runner(branch, *_args):
        case = _args[3]
        captured[branch] = list(case["context_snapshot"].get("items") or [])
        return {
            "branch": branch,
            "runtime": "agentshub",
            "ok": True,
            "messages": [],
            "interaction_turns": 1,
            "tool_call_count": 0,
            "total_tokens": 10,
            "checklist_report": {
                "items": [
                    {"id": "M01", "satisfied": True, "evidence": "done"}
                ],
                "all_satisfied": True,
            },
        }

    runner = MemoryTrueReplayRunner(
        ledger=_Ledger(action="archive"),
        app_config=SimpleNamespace(),
        branch_runner=branch_runner,
        session_store=_Sessions(),
    )
    runner.run(
        change_id="mch-archive",
        query="Complete the task.",
        checklist=["Task is complete"],
        source_session_id="session-1",
    )

    assert any(
        item.get("scope") == "team_memory"
        for item in captured["baseline"]
    )
    assert not any(
        item.get("scope") == "team_memory"
        for item in captured["candidate"]
    )
