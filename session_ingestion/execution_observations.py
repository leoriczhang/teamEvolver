"""Normalize upstream business-asset metadata without inventing execution proof.

The Agent supplies asset identities and actual input values. Only preserved model
requests can establish whether those values were used; this module does not load
current configuration or infer a template from rendered text.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping

OBSERVATION_SCHEMA = "teamevolver.execution-observation.v1"
_BINDING_FIELDS = ("agent_id", "execution_profile_id", "runtime_type", "release_id", "composition_hash")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def observation_context(trace):
    """Return preserved binding/inputs and safe, deterministic validation errors.

    Legacy flat metadata remains accepted. Conflicting declarations are retained
    as an error rather than choosing whichever value happens to be read first.
    """
    metadata = trace.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        return {}, None, ["execution metadata must be an object"]
    binding = {key: metadata[key] for key in _BINDING_FIELDS
               if isinstance(metadata.get(key), str) and metadata[key]}
    if "teamevolver_execution" not in metadata:
        return binding, None, []
    raw = metadata["teamevolver_execution"]
    errors = []
    if not isinstance(raw, Mapping):
        return binding, None, ["teamevolver_execution must be an object"]
    if raw.get("schema_version") != OBSERVATION_SCHEMA:
        errors.append("unsupported execution observation schema")
    if set(raw) - {"schema_version", "template_inputs", *_BINDING_FIELDS}:
        errors.append("unsupported execution observation fields")
    for key in ("execution_profile_id", "agent_id", "runtime_type"):
        if not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 200:
            errors.append("execution observation requires a bounded " + key)
    for key in _BINDING_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            errors.append("invalid execution observation " + key)
            continue
        if key in binding and binding[key] != value:
            errors.append("conflicting execution observation " + key)
        binding[key] = value
    if "composition_hash" in raw and (
        not isinstance(raw["composition_hash"], str) or not _HASH.fullmatch(raw["composition_hash"])
    ):
        errors.append("execution composition_hash must be SHA256")
    inputs = raw.get("template_inputs")
    if inputs is not None:
        if not isinstance(inputs, Mapping):
            errors.append("execution observation template_inputs must be an object")
            inputs = None
        else:
            try:
                encoded = json.dumps(dict(inputs), ensure_ascii=False, allow_nan=False)
                if len(encoded.encode("utf-8")) > 2_200_000:
                    errors.append("execution observation template_inputs exceed the size limit")
                    inputs = None
                else:
                    inputs = copy.deepcopy(dict(inputs))
            except (ValueError, TypeError):
                errors.append("execution observation template_inputs must contain JSON values")
                inputs = None
    return binding, inputs, errors


def attach_observation(context, trace):
    """Attach declarations separately from the independently observed request."""
    binding, inputs, errors = observation_context(trace)
    if binding:
        context["execution_binding"] = {**binding, "evidence_level": "upstream_metadata"}
    if inputs is not None:
        context["template_inputs"] = inputs
        legacy = context.get("trace_input")
        if isinstance(legacy, Mapping) and "template_inputs" in legacy and legacy["template_inputs"] != inputs:
            errors.append("conflicting execution observation template_inputs")
    if errors:
        context["execution_observation_errors"] = errors
    return context
