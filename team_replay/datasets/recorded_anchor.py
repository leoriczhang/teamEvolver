"""Materialize a Prompt query from its frozen original target.

The model chooses an anchor and independently proposes Checks. It never supplies
the executable query in this mode. Legacy query validation remains separate.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping

from teamEvolver.evidence_context import EvidenceContextError, build_evidence_context, has_isolated_records

from ..execution_inputs import normalize_execution_inputs, source_execution_inputs
from ..source_inputs import has_template_input_contract, verified_source_execution_inputs

QUERY_MODE = "recorded_anchor"
QUERY_SCHEMA = "te.recorded-anchor-query.v1"
QUERY_PROTOCOL = """

本轮业务Prompt的离线独立记录支持query_mode="recorded_anchor"协议。
此mode取代前文要求模型直接返回query的字段约定，不改变源业务要求和Checks标准。
选择已提供examples中一条relationship=isolated_record的anchor_id，并保留合法source_session_ids。
每个此类用例须返回query_mode="recorded_anchor"、anchor_id、name、checks、source_session_ids、evidence_window；
不要返回query字段（即使为空也不返回）。服务端仅按选中锚点逐字读取完整recorded_query作为执行题目。
Checks须适用于这条锚点真实query的历史、当前消息和原业务条件；不得串用另一条记录专属的阶段、时间或事实。
仍从多个真实来源归纳共性Checks，不能将多条记录拼接成新会话，不把机器评分当业务授权或黄金答案。
若选中来源不需要离线锚点，仍可沿用原query协议；该协议不改变本轮Case、Check或独立来源根的数量要求。
"""


LIVE_REQUEST_QUERY_PROTOCOL = """

本轮业务Prompt的已记录原始请求支持query_mode="recorded_anchor"协议。
此mode取代前文要求模型直接返回query的字段约定，不改变源业务要求和Checks标准。
选择已提供examples中一条relationship=isolated_record，或其example_id在verified_request_anchor_ids中的
已核验recorded_session_turn作为anchor_id，并保留合法source_session_ids。
recorded_session_turn仍是原持续会话中的一轮，不得重标为独立记录或据此增加来源根数量。
每个此类用例须返回query_mode="recorded_anchor"、anchor_id、name、checks、source_session_ids、evidence_window；
不要返回query字段（即使为空也不返回）。服务端仅按选中锚点逐字读取完整recorded_query作为执行题目。
Checks须适用于这条锚点真实query的历史、当前消息和原业务条件；不得串用另一条记录专属的阶段、时间或事实。
仍从多个真实来源归纳共性Checks，不能将多条记录拼接成新会话，不把机器评分当业务授权或黄金答案。
若选中来源不需要原始请求锚点，仍可沿用原query协议；该协议不改变本轮Case、Check或独立来源根的数量要求。
"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _query(turn):
    return turn.get("prompt_text") or turn.get("instruction") or turn.get("user_query") or ""


def _trace(turn):
    return str(turn.get("source_trace_id") or turn.get("trace_id") or "")


def _references(values):
    if not isinstance(values, list):
        raise EvidenceContextError("Recorded anchor provenance requires a Trace reference list")
    output = set()
    for value in values:
        ref = value.get("trace_id") if isinstance(value, Mapping) else value
        if not isinstance(ref, str) or not ref:
            raise EvidenceContextError("Recorded anchor provenance has an invalid Trace reference")
        output.add(ref)
    return output


def bind_verified_request_anchors(evidence, sessions, baseline):
    """Enable original live targets only after verifying their observed inputs.

    This adds a host-derived selector, not an isolated-record declaration. The
    materializer repeats verification against the original source and Baseline.
    """
    if not has_template_input_contract(baseline):
        return evidence
    packet = copy.deepcopy(evidence)
    verified = []
    for example in packet.get("examples") or []:
        if example.get("relationship") != "recorded_session_turn":
            continue
        matches = [(session, turn) for session in sessions for turn in session.get(
            "_evidence_turns", session.get("turns", []))
            if session.get("session_id") == example["session_id"]
            and _trace(turn) == example.get("trace_id")]
        if len(matches) != 1:
            continue
        try:
            inputs = verified_source_execution_inputs(*matches[0], baseline)
        except (KeyError, TypeError, ValueError):
            continue
        if inputs is not None:
            verified.append(example["example_id"])
    if not verified:
        return evidence
    packet["verified_request_anchor_ids"] = sorted(verified)
    packet["coverage"].pop("context_sha256", None)
    packet["coverage"]["context_sha256"] = _hash(packet)
    return packet


def recorded_anchor_records(evidence):
    verified = set(evidence.get("verified_request_anchor_ids") or [])
    return {item["example_id"]: item for item in evidence.get("examples") or []
            if item.get("relationship") == "isolated_record" or (
                item.get("relationship") == "recorded_session_turn" and item.get("example_id") in verified)}


def anchor_required_sources(evidence):
    verified = set(evidence.get("verified_request_anchor_ids") or [])
    return set(evidence.get("anchor_required_for_session_ids") or []) | {
        item["session_id"] for item in evidence.get("examples") or [] if item.get("example_id") in verified}


def _validate_supporting_targets(source_id, sessions):
    """An additional source root must carry actual targets, never Context padding."""
    originals = [session for session in sessions if session.get("session_id") == source_id]
    found_body = False
    for session in originals:
        if session.get("context_only") is True or session.get("usage") == "context_only":
            raise EvidenceContextError("Context-only supporting Sessions cannot supply recorded anchor evidence")
        provenance = session.get("provenance") or {}
        if not isinstance(provenance, Mapping):
            raise EvidenceContextError("Recorded anchor supporting source provenance must be an object")
        targets = _references(provenance["target_trace_refs"]) if "target_trace_refs" in provenance else None
        contexts = _references(provenance.get("context_trace_refs") or [])
        turns = session.get("_evidence_turns", session.get("turns", []))
        if not isinstance(turns, list):
            raise EvidenceContextError("Recorded anchor supporting target turns must be a list")
        for turn in turns:
            if not isinstance(turn, Mapping):
                continue
            tid = _trace(turn)
            if ((targets is not None and tid not in targets)
                    or (tid in contexts and (targets is None or tid not in targets))):
                continue
            if turn.get("context_only") is True or turn.get("usage") == "context_only":
                raise EvidenceContextError("Context-only supporting turns cannot supply recorded anchor evidence")
            texts = [_query(turn), turn.get("response_text")]
            texts.extend(message.get("content") for message in turn.get("messages") or []
                         if isinstance(message, Mapping))
            found_body |= any(isinstance(text, str) and bool(text.strip()) for text in texts)
    if not found_body:
        raise EvidenceContextError("Recorded anchor supporting source requires an actual target body")


def _validate_original_target(anchor, sessions, *, baseline=None, verified_request=False):
    sid, tid = anchor.get("session_id"), anchor.get("trace_id")
    if anchor.get("evidence_ref") != (tid or sid):
        raise EvidenceContextError("Recorded anchor Trace and evidence_ref differ")
    originals = [session for session in sessions if session.get("session_id") == sid]
    if not originals:
        raise EvidenceContextError("Recorded anchor Session is absent from frozen sources")
    matched = []
    for session in originals:
        if session.get("context_only") is True or session.get("usage") == "context_only":
            raise EvidenceContextError("Context-only Sessions cannot supply a recorded anchor")
        provenance = session.get("provenance") or {}
        if not isinstance(provenance, Mapping):
            raise EvidenceContextError("Recorded anchor Session provenance must be an object")
        targets = _references(provenance["target_trace_refs"]) if "target_trace_refs" in provenance else None
        contexts = _references(provenance.get("context_trace_refs") or [])
        if targets is not None and (not tid or tid not in targets):
            raise EvidenceContextError("Recorded anchor is outside the explicit frozen target Trace set")
        if tid in contexts and (targets is None or tid not in targets):
            raise EvidenceContextError("A Context Trace cannot supply a recorded anchor")
        original_examples = build_evidence_context([session], max_bytes=8 * 1024 * 1024)["examples"]
        if not any(example["example_id"] == anchor.get("example_id")
                   and example["session_id"] == sid and example.get("trace_id") == tid
                   and example.get("evidence_ref") == anchor.get("evidence_ref")
                   and example["recorded_query"] == anchor["recorded_query"]
                   and example["relationship"] == anchor.get("relationship") for example in original_examples):
            raise EvidenceContextError("Recorded anchor identity differs from its frozen original source")
        turns = session.get("_evidence_turns", session.get("turns", []))
        if not isinstance(turns, list):
            raise EvidenceContextError("Recorded anchor source turns must be a list")
        for turn in turns:
            if not isinstance(turn, Mapping) or (tid and _trace(turn) != tid):
                continue
            if not tid and (_trace(turn) or _query(turn) != anchor["recorded_query"]):
                continue
            if turn.get("context_only") is True or turn.get("usage") == "context_only":
                raise EvidenceContextError("A Context-only turn cannot supply a recorded anchor")
            if _query(turn) != anchor["recorded_query"]:
                raise EvidenceContextError("Recorded anchor query differs from its frozen original target")
            if verified_request:
                if (anchor.get("relationship") != "recorded_session_turn"
                        or not has_template_input_contract(baseline)):
                    raise EvidenceContextError("Live request anchors require a frozen shared-template plan")
                try:
                    inputs = verified_source_execution_inputs(session, turn, baseline)
                except (KeyError, TypeError, ValueError) as exc:
                    raise EvidenceContextError("Live request anchor source inputs are not verified") from exc
                if inputs is None:
                    raise EvidenceContextError("Live request anchor source inputs are not verified")
            elif not has_isolated_records([{**session, "_evidence_turns": [turn]}]):
                raise EvidenceContextError("Recorded anchor mode requires an explicitly isolated original record")
            matched.append((session, turn))
    if len(matched) != 1:
        raise EvidenceContextError("Recorded anchor has no matching frozen original target turn")
    return matched[0]


def materialize_recorded_anchor_queries(parsed, *, evidence, sessions, enabled=False, baseline=None):
    """Return a canonical copy and stable per-index bindings, without repair.

    With no new mode this is a no-op. A model query is never overwritten, even
    when it happens to match another legitimate source record.
    """
    if not enabled or not isinstance(parsed, Mapping) or not isinstance(parsed.get("test_datasets"), list):
        return parsed, {}
    raw_cases = parsed["test_datasets"]
    if not any(isinstance(case, Mapping) and "query_mode" in case for case in raw_cases):
        return parsed, {}
    if not isinstance(evidence, Mapping):
        raise EvidenceContextError("Recorded anchor mode requires complete frozen Evidence")
    packet = copy.deepcopy(evidence)
    coverage = packet.get("coverage") or {}
    recorded_packet_hash = coverage.pop("context_sha256", None)
    if not isinstance(recorded_packet_hash, str) or _hash(packet) != recorded_packet_hash:
        raise EvidenceContextError("Recorded anchor Evidence packet hash differs from its complete body")
    examples = evidence.get("examples") or []
    anchors = recorded_anchor_records(evidence)
    if len({item["example_id"] for item in examples}) != len(examples):
        raise EvidenceContextError("Recorded anchor identities must be unique")
    allowed_sources = set(evidence["coverage"].get("source_session_ids") or [])
    original_sources = {str(session.get("session_id") or "") for session in sessions}
    output, bindings = copy.deepcopy(parsed), {}
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, Mapping) or "query_mode" not in raw:
            continue
        if raw["query_mode"] != QUERY_MODE:
            raise EvidenceContextError("Unsupported offline Prompt query_mode")
        if "query" in raw:
            raise EvidenceContextError("recorded_anchor mode must omit query; model text is never silently replaced")
        sources = raw.get("source_session_ids")
        if (not isinstance(sources, list) or not sources or any(not isinstance(sid, str) or not sid
                or sid not in allowed_sources or sid not in original_sources for sid in sources)
                or len(set(sources)) != len(sources)):
            raise EvidenceContextError("Recorded anchor sources must be exact distinct frozen Session IDs")
        for source_id in sources:
            _validate_supporting_targets(source_id, sessions)
        anchor = anchors.get(raw.get("anchor_id")) if isinstance(raw.get("anchor_id"), str) else None
        if (not anchor or anchor.get("session_id") not in sources
                or anchor.get("session_id") not in anchor_required_sources(evidence)):
            raise EvidenceContextError(
                "Recorded anchor must select an isolated or verified request target from its declared source Sessions",
            )
        query = anchor.get("recorded_query")
        if not isinstance(query, str) or not query.strip():
            raise EvidenceContextError("Recorded anchor requires its complete nonempty original query")
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        if (query_hash != (anchor.get("content_sha256") or {}).get("recorded_query")
                or type((anchor.get("content_utf8_bytes") or {}).get("recorded_query")) is not int
                or len(query.encode()) != anchor["content_utf8_bytes"]["recorded_query"]):
            raise EvidenceContextError("Recorded anchor query hash or complete byte length differs")
        verified_request = anchor["example_id"] in set(evidence.get("verified_request_anchor_ids") or [])
        original_session, original_turn = _validate_original_target(
            anchor, sessions, baseline=baseline, verified_request=verified_request,
        )
        inputs = source_execution_inputs(original_session, original_turn)
        if inputs is not None:
            if "execution_inputs" in raw or "execution_inputs" in (raw.get("provenance") or {}):
                raise EvidenceContextError("Model-supplied execution input bindings are not allowed")
            supplied = (anchor.get("recorded_context") or {}).get("template_inputs")
            if (not isinstance(supplied, Mapping)
                    or normalize_execution_inputs({**supplied, "source_ref": inputs["source_ref"]}) != inputs):
                raise EvidenceContextError("Recorded anchor template inputs differ from its frozen original source")
        output["test_datasets"][index]["query"] = query
        bindings[index] = {
            "schema_version": QUERY_SCHEMA, "query_mode": QUERY_MODE, "raw_case_index": index,
            "anchor_id": raw["anchor_id"], "session_id": anchor["session_id"], "trace_id": anchor.get("trace_id"),
            "evidence_ref": anchor["evidence_ref"], "recorded_query_sha256": query_hash,
            "recorded_query_utf8_bytes": len(query.encode()), "evidence_context_sha256": recorded_packet_hash,
            "query_origin": "frozen_original_target", "model_query_supplied": False,
        }
        if inputs is not None:
            bindings[index]["execution_inputs_sha256"] = _hash(inputs)
    return output, bindings


def recorded_anchor_review_bindings(cases):
    """Expose only server-verified query identities, never source answers or grades."""
    output = []
    for case in cases:
        provenance = case.get("provenance") or {}
        binding = provenance.get("query_materialization")
        if binding is None:
            continue
        anchor, query = provenance.get("evidence_anchor") or {}, case.get("query")
        if (not isinstance(binding, Mapping) or binding.get("schema_version") != QUERY_SCHEMA
                or binding.get("query_mode") != QUERY_MODE or binding.get("model_query_supplied") is not False
                or not isinstance(query, str) or hashlib.sha256(query.encode()).hexdigest() != binding.get(
                    "recorded_query_sha256") or len(query.encode()) != binding.get("recorded_query_utf8_bytes")
                or any(binding.get(key) != anchor.get(key) for key in (
                    "anchor_id", "session_id", "trace_id", "evidence_ref", "recorded_query_sha256",
                    "evidence_context_sha256",
                ))):
            raise EvidenceContextError("Recorded anchor review requires an intact server-materialized query binding")
        if "execution_inputs_sha256" in binding:
            inputs = normalize_execution_inputs(provenance.get("execution_inputs"))
            if _hash(inputs) != binding["execution_inputs_sha256"]:
                raise EvidenceContextError("Recorded anchor review input bindings differ from their frozen receipt")
        output.append({"case_id": case["case_id"], **copy.deepcopy(binding)})
    return output


def attach_recorded_anchor_wire_receipts(cases, *, raw_response, parsed_wire):
    """Retain original model bytes and distinguish them from canonical Cases."""
    for case in cases:
        binding = (case.get("provenance") or {}).get("query_materialization")
        if binding is None:
            continue
        raw_case = parsed_wire["test_datasets"][binding["raw_case_index"]]
        canonical_case_hash = _hash(case)
        case["provenance"]["query_materialization_receipt"] = {
            "schema_version": "te.recorded-anchor-wire.v1", "raw_generation_response": raw_response,
            "raw_generation_response_sha256": hashlib.sha256(raw_response.encode()).hexdigest(),
            "raw_case": copy.deepcopy(raw_case), "raw_case_sha256": _hash(raw_case),
            "canonical_query_sha256": binding["recorded_query_sha256"],
            "canonical_case_sha256": canonical_case_hash,
            "canonical_case_hash_scope": "normalized_case_before_review_receipts_and_hydration",
        }
