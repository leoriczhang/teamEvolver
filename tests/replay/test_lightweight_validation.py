"""Candidate execution completion, external blocks and archived judge retries."""
import copy
from dataclasses import replace

import pytest

from team_replay.contracts import skill_job_from_spec
from team_replay.gateway import HostReplayGateway
from team_replay.hooks import AgentObservation
from team_replay.runner import evaluate_spec
from team_skills.evolution.validation import TrueReplayValidator
from tests.support.replay import Host, judge, spec


def light_job():
    job = skill_job_from_spec(spec())
    job["validation_method"] = "candidate_execution"
    job["validation_scope"] = "完成指定任务"
    item = copy.deepcopy(job["replay_cases"][0])
    job["replay_cases"] = []
    for index in range(3):
        case = copy.deepcopy(item)
        case["case_id"] = f"case-{index}"
        job["replay_cases"].append(case)
    return job


@pytest.mark.asyncio
async def test_one_candidate_completes_without_baseline_or_efficiency_metrics():
    host = Host(metrics=False)
    result = await TrueReplayValidator(HostReplayGateway(host, judge))(
        light_job(), {"max_interactions": 20},
    )
    assert result["verdict"] == "accept"
    assert result["basic_validation"]["status"] == "passed"
    assert result["candidate_completed"] == 1 and result["ab_completed"] == 0
    assert result["ab_verdict"] == "inconclusive"
    assert result["completed"] == 1 and result["total"] == 3
    assert [c.treatment.branch for c in host.contexts] == ["candidate"]
    assert len(host.closed) == 1


@pytest.mark.asyncio
async def test_matching_completed_pair_is_reused_even_if_baseline_failed():
    full = evaluate_spec(spec(), host=Host(), judge=judge)
    pair = full["cases"][0]
    pair["baseline"].update(completed=False, status="failed", error="Baseline timeout")
    full.update(case_id="case-2", verdict="inconclusive")
    host = Host()
    result = await TrueReplayValidator(HostReplayGateway(host, judge))(
        light_job(), {}, cached={"case-2": full},
    )
    assert result["verdict"] == "accept" and result["candidate_completed"] == 1
    assert result["ab_verdict"] == "inconclusive"
    assert host.contexts == []


@pytest.mark.asyncio
async def test_only_baseline_completion_does_not_count_as_candidate_completion():
    value = evaluate_spec(spec(), host=Host(), judge=judge)
    for pair in value["cases"]:
        pair["candidate"].update(completed=False, ok=False)
    host = Host()
    job = light_job()
    result = await TrueReplayValidator(HostReplayGateway(host, judge))(
        {**job, "replay_cases": job["replay_cases"][:1]}, {}, cached={"case-0": value},
    )
    assert result["candidate_completed"] == 0 and result["verdict"] == "inconclusive"
    assert host.contexts == []


def test_missing_authorization_stops_after_one_response_and_discloses_known_requirements():
    prompts = []

    class BlockedHost(Host):
        def resolve_replay_factory(self, *_):
            class Session:
                def send(self, message):
                    prompts.append(message)
                    return AgentObservation("The source needs authorization")

                def close(self):
                    pass

            class Factory:
                def open(self, context):
                    return Session()
            return Factory()

    def blocked(**kwargs):
        return {
            "judge": "model", "all_satisfied": False,
            "items": [{**r, "satisfied": False, "evidence": "Authorization required"} for r in kwargs["checklist"]],
            "external_blocked": True, "blocking_conditions": ["Source permission unavailable"],
        }

    pinned = replace(spec(), metadata={"validation_method": "candidate_execution"})
    result = evaluate_spec(pinned, host=BlockedHost(), judge=blocked,
                           feedback=lambda **_: pytest.fail("No repeated authorization simulation"))
    assert len(prompts) == 1 and "Produce the answer" in prompts[0]
    assert result["cases"][0]["candidate"]["external_blocked"]
    assert result["cases"][0]["candidate"]["status"] == "external_blocked"


@pytest.mark.asyncio
async def test_judge_retry_uses_archived_result_without_new_agent_session():
    host = Host()

    def unavailable(**kwargs):
        return {"judge": "unavailable", "items": []}

    job = light_job()
    first = await TrueReplayValidator(HostReplayGateway(host, unavailable))(job, {})
    assert first["cases"][0]["candidate"]["status"] == "assessment_pending"
    host.contexts.clear()
    result = await TrueReplayValidator(HostReplayGateway(host, judge))(
        job, {}, cached={"case-0": first["window_results"]["recent:case-0"]},
    )
    assert result["candidate_completed"] == 1 and result["verdict"] == "accept"
    assert not host.contexts
