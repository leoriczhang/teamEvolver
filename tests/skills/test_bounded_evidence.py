"""Long MA evidence must include late child tools without oversized calls."""
import asyncio
import copy
import json

import pytest

from team_skills.evolution.stages.analyze import SessionAnalysisError, analyze_session
from team_skills.evolution.stages.trace_analyze import analyze_and_dispatch_traces
from teamEvolver.evidence import MODEL_EVIDENCE_BYTES, bounded_evidence, byte_size, evidence_summary_cache


class EvidenceClient:
    model = "test"

    def __init__(self):
        self.inputs = []
        self.covered = []

    async def chat(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        assert byte_size(payload) <= MODEL_EVIDENCE_BYTES
        self.inputs.append(payload)
        if "records" in payload:
            self.covered.extend(payload["records"])
            return json.dumps({
                "covered_record_ids": [r["record_id"] for r in payload["records"]],
                "summary": "子线程的调用证据包含后部错误，需结合全部批次判断。",
            })
        if "traces" in payload:
            return json.dumps({"traces": [
                {"trace_id": t["trace_id"], "turn_num": t["turn_num"], "is_badcase": False,
                 "problem_type": "other", "problem_description": ""} for t in payload["traces"]
            ]})
        return (
            '<classification>{"decision":"valuable","confidence":0.9,"reason":"工具证据充分"}</classification>'
            "<summary>依据全部子线程分析。</summary>"
            '<judge>{"task_completion":0.5,"response_quality":0.5,"efficiency":0.5,"tool_usage":0.5,'
            '"overall_score":0.5,"evolution_evidence":"defect","skill_experiences":[]}</judge>'
        )


def long_session():
    child = {"trace_id": "child", "session_thread_id": "producer", "agent_name": "Producer",
             "used_skills": ["render"], "prompt_text": "Render", "response_text": "Failed",
             "tool_calls": [], "tool_results": []}
    for idx in range(110):
        child["tool_calls"].append({
            "id": f"call-{idx}", "function": {"name": "bash", "arguments": f"tool-{idx} " + "界" * 1500},
        })
        child["tool_results"].append({
            "tool_call_id": f"call-{idx}", "content": "界" * 1500 + f" late-result-{idx}",
        })
    return {"session_id": "parent", "turns": [
        {"trace_id": "root", "prompt_text": "Make video", "response_text": "Done",
         "used_skills": ["render"], "tool_calls": copy.deepcopy(child["tool_calls"]),
         "tool_results": copy.deepcopy(child["tool_results"]), "agent_threads": [child]},
    ]}


def test_all_long_child_tools_reach_bounded_model_and_storage_is_not_mutated():
    session = long_session()
    stored = copy.deepcopy(session["turns"])
    client = EvidenceClient()
    asyncio.run(analyze_session(client, session))
    records = json.dumps(client.covered, ensure_ascii=False)
    assert "tool-109" in records and "late-result-109" in records
    assert "producer" in records and "render" in records
    for key in ("tool_calls", "tool_results", "agent_threads", "prompt_text", "response_text"):
        assert session["turns"][0][key] == stored[0][key], key
    assert session["_judge_scores"]["source"] == "llm"


def test_incomplete_summary_coverage_blocks_final_judgment():
    class Broken(EvidenceClient):
        async def chat(self, messages, **kwargs):
            result = await super().chat(messages, **kwargs)
            if "records" in self.inputs[-1]:
                return '{"covered_record_ids":[],"summary":"incomplete"}'
            return result

    session = long_session()
    with pytest.raises(SessionAnalysisError, match="every input record"):
        asyncio.run(analyze_session(Broken(), session))
    assert "_judge_scores" not in session and "_summary" not in session


def test_long_traces_still_get_one_verdict_each():
    result = asyncio.run(analyze_and_dispatch_traces(EvidenceClient(), long_session(), fire_hook=False))
    assert [r["trace_id"] for r in result] == ["root", "child"]
    assert result[1]["session_thread_id"] == "producer"
    assert result[1]["used_skills"] == ["render"]


def test_large_string_sends_all_parts_including_the_middle_and_tail():
    payload = {"content": "a" * 100_000 + "middle-marker" + "b" * 100_000 + "tail-marker"}
    client = EvidenceClient()
    asyncio.run(bounded_evidence(client, payload))
    sent = "".join(r["value"] for r in client.covered)
    assert sent == payload["content"]


def test_malformed_chunk_retries_without_resending_validated_siblings():
    class Truncated(EvidenceClient):
        def __init__(self):
            super().__init__()
            self.attempts = {}

        async def chat(self, messages, **kwargs):
            batch = kwargs["trace_metadata"]["batch"]
            self.attempts[batch] = self.attempts.get(batch, 0) + 1
            result = await super().chat(messages, **kwargs)
            if batch == 0 and self.attempts[batch] == 1:
                return '{"covered_record_ids":'
            return result

    client = Truncated()
    result = asyncio.run(bounded_evidence(client, {"content": "界" * 80_000}))
    assert result["evidence_mode"] == "all_chunks_summarized"
    assert client.attempts[0] == 2
    assert len(client.attempts) > 1
    assert all(count == 1 for batch, count in client.attempts.items() if batch != 0)


def test_coverage_retry_distinguishes_current_ids_from_nested_historical_ids():
    class Confused(EvidenceClient):
        def __init__(self):
            super().__init__()
            self.attempts = {}

        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            batch = kwargs["trace_metadata"]["batch"]
            self.attempts[batch] = self.attempts.get(batch, 0) + 1
            assert payload["expected_record_ids"] == [r["record_id"] for r in payload["records"]]
            if self.attempts[batch] == 1:
                return json.dumps({"covered_record_ids": ["historical:wrong"], "summary": "旧摘要编号"})
            assert "上一回复未通过完整性校验" in messages[0]["content"]
            assert "historical:wrong" in messages[0]["content"]
            assert "missing=" in messages[0]["content"]
            return await super().chat(messages, **kwargs)

    client = Confused()
    result = asyncio.run(bounded_evidence(client, {
        "summary": "历史摘要中的编号 historical:wrong " + "界" * 80_000,
    }))
    assert result["evidence_mode"] == "all_chunks_summarized"
    assert all(count == 2 for count in client.attempts.values())


def test_failed_reduction_resumes_validated_batches_with_a_new_client():
    class Cache:
        def __init__(self):
            self.values = {}

        def get_evidence_summary(self, key):
            return self.values.get(key)

        def put_evidence_summary(self, key, value):
            return self.values.setdefault(key, value)

    class ProviderFailure(EvidenceClient):
        async def chat(self, messages, **kwargs):
            if kwargs["trace_metadata"]["batch"] == 1:
                await asyncio.sleep(0.05)
                raise RuntimeError("provider unavailable")
            return await super().chat(messages, **kwargs)

    cache = Cache()
    payload = {"content": "界" * 80_000}
    first = ProviderFailure()
    with evidence_summary_cache(cache), pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(bounded_evidence(first, payload))
    assert cache.values
    cached_records = {r["record_id"] for r in first.covered}
    resumed = EvidenceClient()
    with evidence_summary_cache(cache):
        result = asyncio.run(bounded_evidence(resumed, payload))
    assert result["evidence_mode"] == "all_chunks_summarized"
    assert resumed.covered
    assert not cached_records.intersection(r["record_id"] for r in resumed.covered)

    changed_model = EvidenceClient()
    changed_model.model = "different-model"
    with evidence_summary_cache(cache):
        asyncio.run(bounded_evidence(changed_model, payload))
    assert {r["record_id"] for r in changed_model.covered} == {
        r["record_id"] for r in first.covered + resumed.covered
    }

    uncached = EvidenceClient()
    asyncio.run(bounded_evidence(uncached, payload))
    assert len(uncached.covered) == len(changed_model.covered)


def test_seed_summary_json_mode_does_not_change_final_judge_options():
    class Seed(EvidenceClient):
        model = "doubao-seed-evolving"

        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            if "records" in payload:
                assert kwargs["response_format"] == {"type": "json_object"}
                assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
            else:
                assert "extra_body" not in kwargs
                assert "response_format" not in kwargs
            return await super().chat(messages, **kwargs)

    asyncio.run(analyze_session(Seed(), long_session()))


def test_repeated_replay_views_keep_every_distinct_value_once():
    source = {"session_id": "source", "content": "完整原文" * 5000 + "late-output"}
    cases = [
        {"case_id": "one", "provenance": source, "verdict": "incomplete"},
        {"case_id": "two", "provenance": copy.deepcopy(source), "verdict": "complete"},
    ]
    payload = {"cases": cases, "datasets": [{"cases": copy.deepcopy(cases)}],
               "window_results": {"recent": {"cases": copy.deepcopy(cases), "window": "recent"}}}
    original = copy.deepcopy(payload)
    client = EvidenceClient()
    result = asyncio.run(bounded_evidence(client, payload))
    assert not client.inputs  # Exact references make this fit without model compression.
    assert result["evidence_mode"] == "exact_duplicates_referenced"
    unique = result["evidence"]
    assert unique["cases"][0] == cases[0]
    assert unique["cases"][1]["verdict"] == "complete"
    assert unique["cases"][1]["provenance"] == {"$evidence_ref": '$["cases"][0]["provenance"]'}
    assert unique["datasets"][0]["cases"] == {"$evidence_ref": '$["cases"]'}
    assert unique["window_results"]["recent"]["cases"] == {"$evidence_ref": '$["cases"]'}
    assert payload == original
    assert byte_size(result) < MODEL_EVIDENCE_BYTES


def test_trace_batch_boundary_does_not_summarize_the_same_trace_twice(monkeypatch):
    from team_skills.evolution.stages import trace_analyze

    prepared = []

    async def reduce(_llm, payload, **_kwargs):
        prepared.append(payload["trace_id"])
        return {"evidence_mode": "all_chunks_summarized", "summary": "x" * 46_000}

    monkeypatch.setattr(trace_analyze, "bounded_evidence", reduce)
    template = long_session()["turns"][0]["agent_threads"][0]
    session = {"session_id": "batch-boundary", "turns": [
        {**copy.deepcopy(template), "trace_id": f"trace-{idx}"} for idx in range(3)
    ]}
    client = EvidenceClient()
    results = asyncio.run(analyze_and_dispatch_traces(client, session, fire_hook=False))
    assert len(client.inputs) == 2
    assert prepared == ["trace-0", "trace-1", "trace-2"]
    assert [row["trace_id"] for row in results] == prepared


def test_complete_reordered_coverage_reaches_final_judge_without_resending_batches():
    class Reordered(EvidenceClient):
        def __init__(self):
            super().__init__()
            self.attempts = {}

        async def chat(self, messages, **kwargs):
            raw = await super().chat(messages, **kwargs)
            if "records" in self.inputs[-1]:
                key = (kwargs["trace_metadata"]["depth"], kwargs["trace_metadata"]["batch"])
                self.attempts[key] = self.attempts.get(key, 0) + 1
                result = json.loads(raw)
                result["covered_record_ids"].reverse()
                return json.dumps(result)
            return raw

    client = Reordered()
    session = long_session()
    asyncio.run(analyze_session(client, session))
    assert client.attempts and all(count == 1 for count in client.attempts.values())
    assert session["_judge_scores"]["source"] == "llm"
    sent = json.dumps(client.covered, ensure_ascii=False)
    assert "tool-109" in sent and "late-result-109" in sent


@pytest.mark.parametrize("covered", [
    ["current:0", "current:0"],
    ["current:0", "historical:1"],
    ["current:0"],
    ["current:0", "current:1", "current:1"],
    ["current:0", ["current:1"]],
    None,
])
def test_reordered_coverage_fix_still_rejects_missing_duplicate_and_foreign_records(covered):
    from teamEvolver.evidence import _validate_summary

    batch = [{"record_id": "current:0"}, {"record_id": "current:1"}]
    with pytest.raises(ValueError, match="every input record"):
        _validate_summary(json.dumps({
            "covered_record_ids": covered, "summary": "必须覆盖本次全部不同记录",
        }), batch)
