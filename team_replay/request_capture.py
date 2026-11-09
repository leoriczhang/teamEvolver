"""Capture immutable bytes at a trusted Adapter's final model-transport boundary.

The owner-supplied, revision-pinned Adapter is trusted code, not a security sandbox.
Receipt IDs must resolve to branch-owned dispatch records captured locally or
authenticated at the installed remote transport. A transport callback must send
the supplied bytes unchanged. No credential/header is persisted here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import uuid
from typing import Mapping
from urllib.parse import urlsplit

from ._util import stable_hash
from .execution_inputs import plan_input_messages
from .execution_plan import EXECUTION_RECEIPT_SCHEMA, execution_plan_hash, render_components
from .hooks import ReplayUnsupported
from .variable_context import project_inline_plan


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class RequestObserver:
    """One branch's evidence, kept outside the Adapter's returned JSON receipt."""

    def __init__(self):
        self._records = []
        self._lock = threading.Lock()

    def __deepcopy__(self, memo):
        # Contexts are defensively copied before open(). Retain the same TE-owned observer.
        return self

    def dispatch(self, body: Mapping, transport, *, destination: str):
        """Invoke the actual final transport with exactly the captured immutable bytes."""
        address = urlsplit(destination)
        if address.scheme not in {"http", "https"} or not address.hostname or (
            address.username or address.password or address.query or address.fragment
        ):
            raise ReplayUnsupported("observed model destination must be an HTTP URL without embedded credentials/query")
        raw = canonical_json(dict(body)).encode("utf-8")
        record = {
            "dispatch_id": uuid.uuid4().hex, "request": json.loads(raw),
            "request_hash": hashlib.sha256(raw).hexdigest(), "succeeded": False,
            "destination": {"scheme": address.scheme, "host": address.hostname,
                            "port": address.port or (443 if address.scheme == "https" else 80), "path": address.path},
        }
        with self._lock:
            self._records.append(record)
        response = transport(raw)
        with self._lock:
            record["succeeded"] = True
        return response, record["dispatch_id"]

    def records(self):
        with self._lock:
            return copy.deepcopy(self._records)

    def accept_attested(self, envelope, *, secret, context, turn_num, session_id, nonce,
                        output=None, runtime_fingerprint=None, transport_plan=None):
        """Authenticate bytes captured at an installed remote model transport."""
        from .remote_capture import verify_dispatch_attestation

        records = verify_dispatch_attestation(
            envelope, secret=secret, context=context, turn_num=turn_num, session_id=session_id, nonce=nonce,
            output=output, runtime_fingerprint=runtime_fingerprint, transport_plan=transport_plan,
        )
        with self._lock:
            seen = {r["dispatch_id"] for r in self._records}
            if any(r["dispatch_id"] in seen for r in records):
                raise ReplayUnsupported("remote model dispatch was already consumed")
            self._records.extend(records)
        return [r["dispatch_id"] for r in records]


def _snapshot_without_replaced_items(context):
    snapshot = copy.deepcopy(dict(context.context_snapshot))
    refs = {
        item["binding"].get("context_ref")
        for item in context.execution_plan["components"]
        if item["kind"] == "context_fragment" and item["binding"].get("context_ref")
    }
    if refs:
        items = snapshot.get("items", [])
        for ref in refs:
            found = [item for item in items if item.get("ref_id") == ref or item.get("uri") == ref]
            if len(found) != 1:
                raise ReplayUnsupported("context replacement must bind exactly one frozen Context item")
        snapshot["items"] = [
            item for item in items if not any(item.get("ref_id") == ref or item.get("uri") == ref for ref in refs)
        ]
    return snapshot


def frozen_input_messages(context):
    """Reference materialization preserves all inputs and replaces bound Context once."""
    values = [
        ("Frozen Skill treatment", context.treatment.skill),
        ("Frozen source materials", list(context.materials)),
        ("Frozen Context snapshot", _snapshot_without_replaced_items(context)),
    ]
    messages = [
        {"role": "user", "content": label + "\n" + canonical_json(value)}
        for label, value in values if value
    ]
    return messages + plan_input_messages(context.execution_plan)


def materialize_messages(context, user_message: str, history=()):
    """Use a complete frozen Prompt surface; never append the original system prompt."""
    messages = [{"role": item["role"], "content": item["content"]}
                for item in render_components(context.execution_plan)]
    messages.extend(frozen_input_messages(context))
    messages.extend([{**copy.deepcopy(m), "content": format_message(context.execution_plan, m["role"], m["content"])}
                     for m in history])
    messages.append({"role": "user", "content": format_message(context.execution_plan, "user", user_message)})
    return messages


def format_message(plan, role, content):
    """Freeze the business Runtime's ordinary speaker-prefix convention."""
    prefixes = plan.get("environment", {}).get("message_prefixes", {})
    prefix = prefixes.get(role, "")
    if not prefix:
        return content
    speaker = prefix.rstrip(":：")
    return content if re.match(r"^\s*" + re.escape(speaker) + r"\s*[:：]", content) else prefix + content


def make_execution_receipt(context, turn_num: int, dispatch_ids):
    records = {item["dispatch_id"]: item for item in context.request_observer.records()}
    requests = []
    for identity in dispatch_ids:
        record = records[identity]
        requests.append({
            "dispatch_id": identity, "request_hash": record["request_hash"], "request": record["request"],
            "destination": record["destination"],
            **{key: record[key] for key in ("observation", "runtime_fingerprint", "execution_projection")
               if key in record},
            "component_positions": [
                {"component_id": item["component_id"], "message_index": index}
                for index, item in enumerate(render_components(context.execution_plan))
            ],
            "component_renderings": _rendering_receipt(context.execution_plan),
        })
    return {
        "schema_version": EXECUTION_RECEIPT_SCHEMA, "request_id": context.request_id,
        "turn_num": turn_num, "plan_hash": execution_plan_hash(context.execution_plan),
        "binding": copy.deepcopy(context.execution_plan["binding"]), "requests": requests,
    }


def _rendering_receipt(plan):
    return [{key: value for key, value in item.items() if key not in {"role", "content"}}
            for item in render_components(plan)]


def verify_execution_receipt(context, receipt, *, turn_num: int, user_message: str, after: int):
    """Return TE-captured requests, rejecting missing, fake, stale or overwritten loads."""
    if not isinstance(receipt, Mapping) or receipt.get("schema_version") != EXECUTION_RECEIPT_SCHEMA:
        raise ReplayUnsupported("missing verifiable execution_receipt")
    if receipt.get("request_id") != context.request_id or receipt.get("turn_num") != turn_num:
        raise ReplayUnsupported("execution_receipt Session/turn mismatch")
    if receipt.get("plan_hash") != execution_plan_hash(context.execution_plan) or (
        receipt.get("binding") != context.execution_plan["binding"]
    ):
        raise ReplayUnsupported("execution_receipt plan hash/binding mismatch")
    if not isinstance(context.request_observer, RequestObserver):
        raise ReplayUnsupported("no trusted final model request observer")
    actual = context.request_observer.records()[after:]
    supplied = receipt.get("requests")
    if not actual or not isinstance(supplied, list) or len(supplied) != len(actual):
        raise ReplayUnsupported("execution_receipt lacks final model dispatches")
    rendered = render_components(context.execution_plan)
    expected_renderings = _rendering_receipt(context.execution_plan)
    for record, reported in zip(actual, supplied, strict=True):
        if not isinstance(reported, Mapping) or not record["succeeded"] or any(
            reported.get(key) != record[key] for key in ("dispatch_id", "request_hash", "request", "destination")
        ):
            raise ReplayUnsupported("execution_receipt does not match captured final model request")
        if any(reported.get(key) != record.get(key) for key in (
            "observation", "runtime_fingerprint", "execution_projection",
        )):
            raise ReplayUnsupported("execution_receipt changed captured transport provenance")
        if "execution_projection" in record and record["execution_projection"] != {
            "logical_plan_hash": execution_plan_hash(context.execution_plan),
            "transport_plan_hash": execution_plan_hash(project_inline_plan(context.execution_plan)),
        }:
            raise ReplayUnsupported("execution transport projection differs from the frozen logical plan")
        body = record["request"]
        messages = body.get("messages")
        if not isinstance(messages, list) or len(messages) < len(rendered):
            raise ReplayUnsupported("captured final request has no complete model messages")
        positions = reported.get("component_positions")
        expected = [{"component_id": item["component_id"], "message_index": index}
                    for index, item in enumerate(rendered)]
        if positions != expected:
            raise ReplayUnsupported("execution component placement/order mismatch")
        if ("component_renderings" in reported or any(item["variable_names"] for item in rendered)) and (
            reported.get("component_renderings") != expected_renderings
        ):
            raise ReplayUnsupported("execution template rendering hashes do not match the frozen environment")
        for index, component in enumerate(rendered):
            if messages[index] != {"role": component["role"], "content": component["content"]}:
                raise ReplayUnsupported("execution component was ignored, changed or loaded in the wrong slot")
        remainder = messages[len(rendered):]
        if any(item.get("role") in {"system", "developer"} for item in remainder if isinstance(item, Mapping)):
            raise ReplayUnsupported("an unplanned privileged message overrides the frozen execution plan")
        fixed = frozen_input_messages(context)
        if remainder[:len(fixed)] != fixed:
            raise ReplayUnsupported("captured request dropped or changed frozen Skill/material/Context inputs")
        if not any(item == {"role": "user", "content": format_message(context.execution_plan, "user", user_message)}
                   for item in remainder):
            raise ReplayUnsupported("captured request did not execute the current user interaction")
        request_fields = context.execution_plan["environment"].get("request")
        if request_fields is not None and {k: v for k, v in body.items() if k != "messages"} != request_fields:
            raise ReplayUnsupported("captured request changed the frozen model/tool configuration")
        first = context.request_observer.records()[0]["request"]
        if {k: v for k, v in body.items() if k != "messages"} != {
            k: v for k, v in first.items() if k != "messages"
        }:
            raise ReplayUnsupported("model/tool configuration changed within the observed Replay Session")
        if record["destination"] != context.request_observer.records()[0]["destination"]:
            raise ReplayUnsupported("model destination changed within the observed Replay Session")
    return copy.deepcopy(list(supplied))


def comparable_requests(requests, plan):
    """Compare actual A/B non-target request fields, including opaque vendor settings."""
    targets = set(plan["target_component_ids"])
    result = []
    rendered = render_components(plan)
    for item in requests:
        body = copy.deepcopy(item["request"])
        for position, component in enumerate(rendered):
            if component["component_id"] in targets:
                body["messages"][position]["content"] = "<TE_REPLAY_TARGET>"
            else:
                content = body["messages"][position]["content"]
                for span in reversed(component.get("inline_spans") or []):
                    if span["component_id"] in targets:
                        content = content[:span["start"]] + "<TE_REPLAY_TARGET>" + content[span["end"]:]
                body["messages"][position]["content"] = content
        if plan["binding"].get("allow_skill_change"):
            index = len(rendered)
            if index < len(body["messages"]) and body["messages"][index].get("content", "").startswith(
                "Frozen Skill treatment\n"
            ):
                del body["messages"][index]
        result.append({"request_hash": stable_hash(body), "request": body, "destination": item["destination"],
                       **({"runtime_fingerprint": item["runtime_fingerprint"]}
                          if item.get("runtime_fingerprint") else {})})
    return result
