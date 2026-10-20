"""Classify Native Compile failures without turning runtime diagnostics into instructions."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

PROTOCOL = "te-native-failure.v1"
REPAIRABLE_KINDS = {"artifact_validation", "output_truncated"}
_MISSING = re.compile(r"(?:File not found|SOURCE_NOT_FOUND|RESOURCE_NOT_FOUND)\s*:?\s*(viking://[^\s\"'\]<>),;；]+)",
                      re.IGNORECASE)
_ARTIFACT_CODES = {"ARTIFACT_VALIDATION_FAILED", "SCHEMA_VALIDATION_FAILED", "OUTPUT_VALIDATION_FAILED"}
_TRUNCATION_CODES = {"OUTPUT_TRUNCATED", "AGENT_OUTPUT_TRUNCATED"}
_AUTH_CODES = {"UNAUTHORIZED", "FORBIDDEN", "PERMISSION_DENIED", "AUTHENTICATION_FAILED"}
_TRANSPORT_CODES = {"CONNECTION_ERROR", "NETWORK_ERROR", "CONNECT_TIMEOUT", "READ_TIMEOUT"}
_OUTPUT_PROTOCOL = re.compile(r"\b(?:map|reduce|reduce_merge):\s*(?:format|schema):")
_OUTPUT_SEMANTIC = re.compile(r"\b(?:map|reduce|reduce_merge):\s*semantic:")


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def classify_native_failure(task: dict, *, compiler_uri: str = "", source_uris=(),
                            local_validation_detail: str | None = None) -> dict:
    """Use explicit codes or specific runtime evidence; generic failure is never content repair.

    A caller may identify an error raised by the local artifact contract explicitly.
    Remote error wording cannot grant itself that privilege.
    """
    error = deepcopy(task.get("error"))
    pipeline = deepcopy((task.get("meta") or {}).get("pipeline") or {})
    events = deepcopy(task.get("execution_events"))
    errors = pipeline.get("errors") or []
    errors = errors if isinstance(errors, list) else [errors]
    messages = [_text(error or ""), *(_text(row) for row in errors)]
    codes = {str(error.get("code") or "").upper()} if isinstance(error, dict) else set()
    codes.update(str(row.get("code") or "").upper() for row in errors if isinstance(row, dict))
    missing = sorted({match[1] for message in messages for match in _MISSING.finditer(message)})
    compiler_root = compiler_uri.rstrip("/")
    dependencies = [uri for uri in missing if (
        (compiler_root and uri.startswith(compiler_root + "/")) or uri.startswith("viking://agent/skills/")
    )]
    sources = [uri for uri in missing if any(
        uri == root.rstrip("/") or uri.startswith(root.rstrip("/") + "/") for root in source_uris if root
    )]
    if dependencies:
        kind = "compiler_dependency"
    elif sources or "SOURCE_NOT_FOUND" in codes:
        kind = "source_missing"
    elif codes & _AUTH_CODES or any(re.search(r"\b(?:HTTP|status(?:_code)?)\s*[:= ]\s*(?:401|403)\b", msg,
                                             re.IGNORECASE) for msg in messages):
        kind = "auth"
    elif codes & _TRANSPORT_CODES or any("All connection attempts failed" in msg for msg in messages):
        kind = "transport"
    elif missing or any("File not found:" in message for message in messages):
        kind = "unknown"
    elif local_validation_detail is not None:
        kind = "artifact_validation"
    elif codes & _TRUNCATION_CODES or any("Agent output was truncated;" in msg for msg in messages):
        kind = "output_truncated"
    elif codes & _ARTIFACT_CODES:
        kind = "artifact_validation"
    elif any(_OUTPUT_SEMANTIC.search(message) for message in messages):
        kind = "unknown"
    elif any(_OUTPUT_PROTOCOL.search(message) for message in messages):
        kind = "output_protocol"
    else:
        kind = "unknown"
    diagnosis = {"origin": "local_artifact_contract" if local_validation_detail is not None else "remote_task",
                 "error": error, "pipeline": pipeline, "execution_events": events}
    if local_validation_detail is not None:
        diagnosis["validation_detail"] = local_validation_detail
    failure = {"schema_version": PROTOCOL, "kind": kind, "repairable": kind in REPAIRABLE_KINDS,
               "task_id": str(task.get("task_id") or ""), "remote_status": str(task.get("status") or ""),
               "missing_uris": missing, "dependency_uris": dependencies, "source_uris": sources,
               "diagnostics": diagnosis}
    failure["fingerprint"] = hashlib.sha256(
        json.dumps(failure, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return failure
