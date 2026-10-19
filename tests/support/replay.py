"""Deterministic Replay host, judge and immutable dataset factories."""

from __future__ import annotations

import copy

from team_replay.datasets.schema import make_document
from team_replay.hooks import AgentObservation
from team_skills.workbench.experiments import prepare_spec


def document(*, empty=False):
    return make_document(
        dataset_id="ds-test",
        name="Dataset",
        skills=["demo", "helper"],
        allow_empty=empty,
        cases=[]
        if empty
        else [
            {
                "case_id": "case-1",
                "query": "Execute the task",
                "skill_ids": ["demo", "helper"],
                "checks": [{"id": "R1", "kind": "output", "text": "Produce the answer"}],
                "materials": [],
                "provenance": {"source_runtime": {"type": "fake"}},
                "replay": {},
                "metadata": {},
            }
        ],
    )


def bundle(name, text="Work"):
    return {"SKILL.md": f"---\nname: {name}\ndescription: Use for work\n---\n{text}\n".encode()}


def spec():
    return prepare_spec(
        run_id="run-1",
        skill_id="demo",
        dataset=document(),
        bundles={"demo": bundle("demo"), "helper": bundle("helper")},
        versions={"demo": 1, "helper": 2},
        candidate_skill_md=bundle("demo", "Better")["SKILL.md"].decode(),
        candidate_files={"scripts/run.py": "print('unsaved')"},
        runtime={"type": "fake"},
        options={"timeout_seconds": 30, "max_interactions": 4},
        dataset_hash="frozen",
    )


def judge(**kwargs):
    return {
        "judge": "model",
        "all_satisfied": True,
        "total": len(kwargs["checklist"]),
        "items": [{**item, "satisfied": True, "evidence": "observed answer"} for item in kwargs["checklist"]],
    }


class Host:
    def __init__(self, *, metrics=True):
        self.metrics = metrics
        self.contexts = []
        self.closed = []

    def resolve_replay_factory(self, runtime_type, source_session):
        host = self

        class Session:
            def __init__(self, context):
                self.context = context

            def send(self, message):
                metrics = (
                    {"tool_call_count": 1, "total_tokens": (20 if self.context.treatment.branch == "baseline" else 10)}
                    if host.metrics
                    else {}
                )
                return AgentObservation("Answer", metrics=metrics, trace_id=self.context.request_id)

            def close(self):
                host.closed.append(self.context.request_id)

        class Factory:
            revision = "source-v1"

            def open(self, context):
                host.contexts.append(copy.deepcopy(context))
                return Session(context)

        return Factory()

    def judge_harness(self):
        return {}
