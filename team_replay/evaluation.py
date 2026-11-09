"""Optional external scoring of frozen, genuinely executed Replay pairs."""
from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

from ._util import stable_hash

CHECKLIST_EFFICIENCY_MODE = "checklist_efficiency"
EXTERNAL_AGENT_MODE = "external_agent"
EXTERNAL_AGENT_SCORE_POLICY = "external-agent-score"
EXTERNAL_REQUEST_SCHEMA = "team-replay.external-evaluation.request.v1"


def _number(value, name, *, low=0, high=100):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a finite number in {low}..{high}")
    return value


def normalize_evaluation(value=None):
    if value is None:
        value = {}
    if not isinstance(value, Mapping) or set(value) - {"mode", "external_agent"}:
        raise ValueError("invalid Replay evaluation configuration")
    mode = value.get("mode", CHECKLIST_EFFICIENCY_MODE)
    if mode not in {CHECKLIST_EFFICIENCY_MODE, EXTERNAL_AGENT_MODE}:
        raise ValueError("unsupported Replay evaluation mode")
    raw = value.get("external_agent", {})
    fields = {"url", "secret_ref", "timeout_seconds", "rubric", "min_score", "min_improvement", "adapter"}
    if not isinstance(raw, Mapping) or set(raw) - fields:
        raise ValueError("invalid external evaluator configuration")
    external = {"url": "", "secret_ref": "", "timeout_seconds": 60, "rubric": "",
                "min_score": 0, "min_improvement": 0, **raw}
    adapter = external.get("adapter")
    if adapter is not None:
        if (not isinstance(adapter, Mapping) or set(adapter) != {"file", "revision", "source_hash"}
                or not isinstance(adapter.get("file"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.py", adapter["file"])
                or type(adapter.get("revision")) is not int or adapter["revision"] < 1
                or not isinstance(adapter.get("source_hash"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", adapter["source_hash"])):
            raise ValueError("external evaluator adapter requires file, revision and source_hash")
        external["adapter"] = dict(adapter)
    else:
        # Preserve the exact historical HTTP policy hash when no adapter is selected.
        external.pop("adapter", None)
    for key in ("url", "secret_ref", "rubric"):
        if not isinstance(external[key], str):
            raise ValueError(f"external evaluator {key} must be a string")
        if len(external[key]) > {"url": 4096, "secret_ref": 512, "rubric": 262144}[key]:
            raise ValueError(f"external evaluator {key} exceeds its size limit")
    if external["secret_ref"] and not re.fullmatch(r"sref_[a-f0-9]{32}", external["secret_ref"]):
        raise ValueError("external evaluator credentials must use a valid Secret Ref")
    if external["url"]:
        parsed = urlsplit(external["url"])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("external evaluator URL must be HTTP(S) without embedded credentials")
        if parsed.fragment:
            raise ValueError("external evaluator URL cannot contain a fragment")
        sensitive = {"apikey", "token", "password", "accesstoken", "authtoken", "secret", "authorization"}
        if any(key.lower().replace("_", "").replace("-", "") in sensitive
               for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError("external evaluator URL must use Secret Ref authentication")
    elif mode == EXTERNAL_AGENT_MODE and adapter is None:
        raise ValueError("external evaluator URL is required")
    if type(external["timeout_seconds"]) is not int or not 1 <= external["timeout_seconds"] <= 1800:
        raise ValueError("external evaluator timeout_seconds must be 1..1800")
    for key in ("min_score", "min_improvement"):
        _number(external[key], key)
    return {"mode": mode, "external_agent": external}


def external_evaluation_enabled(value=None):
    return normalize_evaluation(value)["mode"] == EXTERNAL_AGENT_MODE


def evaluation_policy_hash(value):
    return stable_hash(normalize_evaluation(value))


def external_case_request(spec, case, results):
    """Project actual public execution, excluding source answers and editing evidence."""
    evaluation = normalize_evaluation(spec.evaluation)
    provenance = case.get("provenance") or {}
    inputs = provenance.get("execution_inputs") or {}
    branches = {}
    for branch in ("baseline", "candidate"):
        result = results[branch]
        branches[branch] = {
            "messages": [
                {key: copy.deepcopy(message[key]) for key in ("role", "content", "tool_calls", "tool_call_id", "name")
                 if key in message}
                for message in result.get("messages") or []
                if isinstance(message, Mapping) and message.get("role") in {"user", "assistant", "tool"}
            ],
            "interactions": [
                {key: copy.deepcopy(item[key]) for key in ("interaction_num", "prompt", "response") if key in item}
                for item in result.get("interactions") or [] if isinstance(item, Mapping)
            ],
            "artifacts": copy.deepcopy(result.get("artifacts") or []),
            "final_response": str(result.get("final_response") or ""),
        }
    request = {
        "schema_version": EXTERNAL_REQUEST_SCHEMA,
        "case": {"case_id": case["case_id"], "case_hash": stable_hash(case), "query": case["query"],
                 "inputs": {"template_variables": copy.deepcopy(inputs.get("template_variables") or {}),
                            "input_messages": copy.deepcopy(inputs.get("input_messages") or []),
                            "context_snapshot": copy.deepcopy(provenance.get("context_snapshot") or {}),
                            "materials": copy.deepcopy(case.get("materials") or [])}},
        "branches": branches, "rubric": evaluation["external_agent"]["rubric"],
    }
    adapter = evaluation["external_agent"].get("adapter")
    identity = {"request": request, "evaluator_adapter": adapter} if adapter else request
    return {"request_id": stable_hash(identity), **request}


def _scores(raw):
    if not isinstance(raw, Mapping):
        raise ValueError("external evaluation response must be an object")
    result = {}
    for branch in ("baseline", "candidate"):
        side = raw.get(branch)
        if not isinstance(side, Mapping) or not isinstance(side.get("reason"), str) or not side["reason"].strip():
            raise ValueError("external evaluation requires a score and reason for each branch")
        result[branch] = {"score": _number(side.get("score"), branch + " score"), "reason": side["reason"]}
    return result


def normalize_external_report(value, request):
    scores = _scores(value)
    if value.get("request_id") not in (None, request["request_id"]):
        raise ValueError("external evaluator response references another request")
    usage = {"available": False}
    raw_usage = value.get("usage")
    if raw_usage is not None:
        if not isinstance(raw_usage, Mapping):
            usage = {"available": False, "incomplete_reason": "invalid_usage"}
        else:
            known = {key: raw_usage[key] for key in ("total_tokens", "input_tokens", "output_tokens")
                     if key in raw_usage}
            if any(type(count) is not int or count < 0 for count in known.values()):
                usage = {"available": False, "incomplete_reason": "invalid_usage"}
            else:
                usage = {"available": "total_tokens" in known, **known}
    return {"status": "evaluated", "request_hash": stable_hash(request), **scores, "usage": usage}


def validate_external_report(value):
    if not isinstance(value, Mapping) or value.get("status") != "evaluated":
        raise ValueError("external evaluation is unavailable")
    request_hash = value.get("request_hash")
    if (not isinstance(request_hash, str) or len(request_hash) != 64
            or any(char not in "0123456789abcdef" for char in request_hash)):
        raise ValueError("external evaluation request hash is missing")
    _scores(value)
    return copy.deepcopy(dict(value))


def valid_external_result(result, evaluation, case=None):
    """A different policy or incomplete cached score cannot become a cache hit."""
    try:
        if (result.get("evaluation_policy_hash") != evaluation_policy_hash(evaluation)
                or normalize_evaluation(result.get("evaluation")) != normalize_evaluation(evaluation)
                or result.get("status") != "evaluated"):
            return False
        validate_external_report(result.get("external_evaluation"))
        pairs = result.get("cases") or []
        if len(pairs) != 1 or any(pairs[0].get(branch, {}).get("ok") is not True
                                  for branch in ("baseline", "candidate")):
            return False
        if case is not None:
            request = external_case_request(SimpleNamespace(evaluation=evaluation), case, pairs[0])
            if (result.get("case_id") != case["case_id"] or result.get("case_hash") != stable_hash(case)
                    or result["external_evaluation"]["request_hash"] != stable_hash(request)):
                return False
        return True
    except (TypeError, ValueError, KeyError):
        return False


def external_score_decision(reports, evaluation):
    config = normalize_evaluation(evaluation)["external_agent"]
    reports = list(reports)
    valid = []
    for report in reports:
        try:
            valid.append(validate_external_report(report))
        except (TypeError, ValueError):
            pass
    result = {
        "policy": EXTERNAL_AGENT_SCORE_POLICY, "primary_metric": "external_agent_score",
        "accepted": False, "verdict": "inconclusive", "no_regression": False,
        "decision_basis": "external_evaluation_unavailable", "case_count": len(reports),
        "evaluated_case_count": len(valid), "baseline_score": None, "candidate_score": None,
        "score_delta": None, "min_score": config["min_score"], "min_improvement": config["min_improvement"],
    }
    if not reports or len(valid) != len(reports):
        return result
    baseline = sum(Decimal(str(report["baseline"]["score"])) for report in valid) / len(valid)
    candidate = sum(Decimal(str(report["candidate"]["score"])) for report in valid) / len(valid)
    delta = candidate - baseline
    result.update(baseline_score=float(baseline), candidate_score=float(candidate), score_delta=float(delta))
    if (candidate < Decimal(str(config["min_score"])) or delta < 0
            or 0 < delta < Decimal(str(config["min_improvement"]))):
        result.update(verdict="reject", decision_basis="external_score_below_requirement")
    elif delta > 0:
        result.update(accepted=True, verdict="accept", no_regression=True,
                      decision_basis="external_score_improved")
    else:
        result.update(no_regression=True, decision_basis="external_score_tied")
    return result
