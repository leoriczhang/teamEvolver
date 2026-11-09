"""Verify installed Agent transport attestations; ordinary telemetry is insufficient."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

from .execution_plan import execution_plan_hash, normalize_execution_plan
from .hooks import ReplayUnsupported
from .variable_context import project_inline_plan

SCHEMA = "teamevolver.remote-dispatch.v1"


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def verify_dispatch_attestation(envelope, *, secret, context, turn_num, session_id, nonce,
                                output=None, runtime_fingerprint=None, transport_plan=None):
    try:
        if not secret or not isinstance(envelope, dict) or set(envelope) != {"payload_b64", "signature"}:
            raise ValueError("signed transport observation is required")
        if len(envelope["payload_b64"]) > 12 * 1024 * 1024:
            raise ValueError("transport attestation exceeds the limit")
        raw = base64.b64decode(envelope["payload_b64"], validate=True)
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, envelope["signature"]):
            raise ValueError("invalid transport signature")
        payload = json.loads(raw, object_pairs_hook=_object)
        projection = None
        plan = context.execution_plan
        if transport_plan is not None:
            plan = normalize_execution_plan(transport_plan)
            if plan != normalize_execution_plan(project_inline_plan(context.execution_plan)):
                raise ValueError("transport plan is not the deterministic logical projection")
            projection = {"logical_plan_hash": execution_plan_hash(context.execution_plan),
                          "transport_plan_hash": execution_plan_hash(plan)}
        expected = {"schema_version": SCHEMA, "request_id": context.request_id, "run_id": context.request_id,
                    "branch": context.treatment.branch, "plan_hash": execution_plan_hash(plan),
                    "turn_num": turn_num, "session_id": session_id, "nonce": nonce}
        if any(payload.get(k) != v for k, v in expected.items()) or not payload.get("runtime_fingerprint"):
            raise ValueError("transport attestation binding mismatch")
        if runtime_fingerprint is not None and payload["runtime_fingerprint"] != runtime_fingerprint:
            raise ValueError("remote Runtime changed during Replay")
        if output is not None and payload.get("output_sha256") != hashlib.sha256(output.encode()).hexdigest():
            raise ValueError("assistant output differs from the signed transport result")
        dispatches = payload["dispatches"]
        if not isinstance(dispatches, list) or not 1 <= len(dispatches) <= 4:
            raise ValueError("transport attestation lacks bounded model dispatches")
        records, seen = [], set()
        for item in dispatches:
            identity = item["dispatch_id"]
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("duplicate or missing remote dispatch identity")
            seen.add(identity)
            body_bytes = base64.b64decode(item["raw_request_base64"], validate=True)
            if len(body_bytes) > 2 * 1024 * 1024:
                raise ValueError("model body exceeds the limit")
            digest = hashlib.sha256(body_bytes).hexdigest()
            if digest != item["request_sha256"] or not 200 <= item["response_status"] < 300:
                raise ValueError("remote model body hash or response status mismatch")
            destination = item["destination"]
            if set(destination) != {"scheme", "host", "port", "path"} or (
                destination["scheme"] not in {"http", "https"} or not destination["host"]
                or "?" in destination["path"] or "@" in destination["host"]
            ):
                raise ValueError("unsafe model destination evidence")
            body = json.loads(body_bytes, object_pairs_hook=_object)
            if not isinstance(body, dict):
                raise ValueError("model request must be an object")
            records.append({"dispatch_id": identity, "request": body,
                            "request_hash": digest, "succeeded": True, "destination": destination,
                            "observation": "remote_attested_dispatch", "usage": item.get("usage"),
                            "runtime_fingerprint": payload["runtime_fingerprint"]})
            if projection is not None:
                records[-1]["execution_projection"] = projection
        return records
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReplayUnsupported("unverifiable remote model transport evidence") from exc
