"""Historical sampling freezes unique inputs and retains per-Dataset evidence."""
import copy
import os

import pytest

from team_replay.datasets.candidate_selection import sample_history
from team_replay.gateway import HostReplayGateway
from team_replay.hooks import AgentObservation
from team_skills.evolution.validation import TrueReplayValidator
from tests.support.replay import bundle, document


def case(number):
    result = copy.deepcopy(document()["cases"][0])
    result.update(case_id=f"case-{number}", query=f"Task {number}", skill_ids=["demo"])
    return result


def history(dataset_id, number):
    return {"dataset_id": dataset_id, "case": case(number)}


def test_history_samples_three_distinct_datasets_and_excludes_current_and_duplicates():
    rows = [history(f"dataset-{n}", n) for n in range(6)]
    rows.extend([history("duplicate", 1), history("current", 100)])
    before = copy.deepcopy(rows)
    selected = sample_history(rows, [case(100)], seed="fixed")
    assert len(selected) == 3
    assert len({row["dataset_id"] for row in selected}) == 3
    assert all(row["case"]["query"] != "Task 100" for row in selected)
    assert selected == sample_history(rows, [case(100)], seed="fixed")
    assert rows == before


def test_history_uses_remaining_unique_cases_when_fewer_datasets_exist():
    rows = [history("one", n) for n in range(4)]
    assert len(sample_history(rows, [], seed="fixed")) == 3
    assert len(sample_history(rows[:2], [], seed="fixed")) == 2
    assert sample_history([], [], seed="fixed") == []


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL required")
def test_acceptance_pins_history_and_excludes_experiments_and_other_skills(pg_env):
    from team_replay.datasets.candidate_selection import freeze_candidate_cases
    from team_replay.datasets.repository import DatasetRepository
    from team_replay.datasets.schema import make_document
    from tests.support.postgres import _run

    async def seed(conn):
        repo = DatasetRepository(pg_env.tx)
        for number in range(5):
            item = case(number)
            document = make_document(
                dataset_id=f"ds-{number}", name=f"Dataset {number}", skills=["demo"], cases=[item],
                source={"kind": "evolution"},
            )
            await repo.save(conn, tenant_id="default", document=document)
        item = case(20)
        await repo.save(conn, tenant_id="default", document=make_document(
            dataset_id="experiment", name="experiment", skills=["demo"], cases=[item],
        ))
        frozen = await freeze_candidate_cases(
            conn, pg_env.tx, tenant_id="default", candidate_id="candidate", skill_name="demo",
            detail={"replay_cases": [case(100), case(101)]}, seed="command",
        )
        assert frozen["replay_sampling"]["historical_selected"] == 3
        assert len(frozen["replay_cases"]) == 5
        ids = {c["provenance"]["dataset_id"] for c in frozen["replay_cases"][2:]}
        assert "experiment" not in ids
        old = copy.deepcopy(frozen)
        edited = await repo.get(conn, next(iter(ids)))
        edited["cases"][0]["query"] = "Changed afterwards"
        document = {k: v for k, v in edited.items()
                    if k not in {"revision", "document_hash", "case_count", "ingested_at"}}
        await repo.save(conn, tenant_id="default", document=document, expected_revision=1)
        assert frozen == old
        assert all(c["query"] != "Changed afterwards" for c in frozen["replay_cases"])
        assert all(c["provenance"]["dataset_revision"] == 1 for c in frozen["replay_cases"])

    _run(pg_env, "default", seed)


@pytest.mark.asyncio
async def test_complete_engine_covers_current_and_history_with_all_metrics_improved():
    cases = [case(n) for n in range(9)]
    for index, item in enumerate(cases):
        item["provenance"].update(
            dataset_id="current" if index < 6 else f"history-{index}",
            dataset_name=f"Dataset {index}", dataset_revision=1,
            replay_selection="current" if index < 6 else "historical",
        )
    job = {
        "job_id": "candidate", "skill_name": "demo",
        "candidate_skill": {"name": "demo", "bundle": {
            "format": "skill-bundle", "files": [],
        }}, "replay_cases": cases,
        "replay_sampling": {"historical_requested": 3, "historical_selected": 3},
    }
    from team_skills.evolution.history import skill_payload
    job["candidate_skill"] = skill_payload("demo", bundle("demo"))

    class FixtureHost:
        def resolve_replay_factory(self, runtime_type, source_session):
            class Session:
                def __init__(self, context):
                    self.context, self.turn = context, 0

                def send(self, message):
                    self.turn += 1
                    candidate = self.context.treatment.branch == "candidate"
                    complete = candidate or self.turn == 3
                    response = "Completed with evidence" if complete else "Working on the task"
                    calls = 2 if candidate else 3
                    return AgentObservation(
                        response, messages=({"role": "assistant", "content": response, "tool_calls": [
                            {"id": str(index), "function": {"name": "verify", "arguments": "{}"}}
                            for index in range(calls)
                        ]},), metrics={"tool_call_count": calls, "total_tokens": 800 if candidate else 1200},
                    )

                def close(self):
                    pass

            class Factory:
                revision = "fixture-v1"

                def open(self, context):
                    return Session(context)

            return Factory()

        def judge_harness(self):
            return {}

    def judge(**kwargs):
        complete = kwargs["interactions"][-1]["response"].startswith("Completed")
        return {
            "judge": "model", "all_satisfied": complete,
            "items": [{**item, "satisfied": complete, "evidence": "Observed completion" if complete else ""}
                      for item in kwargs["checklist"]],
            "positive_observations": ["Completed"] if complete else [],
        }

    validator = TrueReplayValidator(HostReplayGateway(
        FixtureHost(), judge, lambda **kwargs: "Please continue",
    ))
    result = await validator(job, {"concurrency": 2, "max_interactions": 4})
    assert result["verdict"] == "accept" and result["runtime_gate"]["status"] == "passed"
    assert result["succeeded"] == result["completed"] == result["total"] == 9
    assert len(result["datasets"]) == 4
    for dataset in result["datasets"]:
        assert dataset["result"]["verdict"] == "accept"
        assert len(dataset["result"]["cases"]) == dataset["total"]
        for metric in ("interaction_turns", "tool_call_count", "total_tokens"):
            assert dataset["result"]["efficiency"]["dimensions"][metric]["winner"] == "candidate"
    for pair in result["cases"]:
        assert len(pair["baseline"]["interactions"]) == 3
        assert len(pair["candidate"]["interactions"]) == 1
        for branch in ("baseline", "candidate"):
            report = pair[branch]["checklist_report"]
            assert report["all_satisfied"] and all(item["evidence"] for item in report["items"])
