"""Root-declared destination/credential groups with bounded operator options."""
from __future__ import annotations

import copy
import json
import re

from .langfuse_selection import trace_names

_ALIAS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_SECRET = re.compile(r"sref_[0-9a-f]{32}\Z")
_CREDENTIALS = {"publickey", "secretkey", "apikey", "authorization", "password", "accesstoken",
                "rootkey", "rootapikey", "clientsecret", "bearertoken", "subscriptionkey"}
_DESTINATIONS = {"host", "url", "baseurl", "endpoint", "proxy", "headers", "auth", "transport"}


def _check_options(value, *, editable):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower()) if isinstance(key, str) else ""
            if not normalized or normalized in _CREDENTIALS or editable and normalized in _DESTINATIONS:
                raise ValueError("connection destinations and credentials must come from a Root-declared profile")
            _check_options(child, editable=editable)
    elif isinstance(value, list):
        for child in value:
            _check_options(child, editable=editable)


def declared_profiles(metadata):
    profiles = metadata.get("connection_profiles", {"default": {}})
    if not isinstance(profiles, dict) or not profiles or len(profiles) > 32:
        raise ValueError("connection_profiles must contain 1..32 named profiles")
    output = {}
    for name, raw in profiles.items():
        if not isinstance(name, str) or not _ALIAS.fullmatch(name):
            raise ValueError("connection profile names must be identifiers")
        if not isinstance(raw, dict) or set(raw) - {"options", "secret_refs", "env_refs"}:
            raise ValueError("invalid declared connection profile")
        value = {key: copy.deepcopy(raw.get(key, {})) for key in ("options", "secret_refs", "env_refs")}
        if any(not isinstance(part, dict) for part in value.values()):
            raise ValueError("connection profile fields must be objects")
        _check_options(value["options"], editable=False)
        seen = set(value["options"])
        for kind in ("secret_refs", "env_refs"):
            for alias, reference in value[kind].items():
                if not isinstance(alias, str) or not _ALIAS.fullmatch(alias) or alias in seen:
                    raise ValueError("credential aliases must be distinct identifiers")
                seen.add(alias)
                if kind == "secret_refs":
                    if not isinstance(reference, str) or not _SECRET.fullmatch(reference):
                        raise ValueError("declared secret_refs require Secret Ref handles")
                elif not isinstance(reference, str) or not reference.isidentifier() or not reference.isupper():
                    raise ValueError("declared env_refs require environment variable names")
        output[name] = value
    json.dumps(output, allow_nan=False)
    return output


def normalize_settings(raw, metadata):
    if not isinstance(raw, dict) or set(raw) - {"profile", "options"}:
        raise ValueError("settings may contain only a declared profile and editable options")
    profiles = declared_profiles(metadata)
    profile = raw.get("profile", "default")
    if not isinstance(profile, str) or profile not in profiles:
        raise ValueError("select a Root-declared connection profile")
    options = copy.deepcopy(raw.get("options", {}))
    allowed = metadata.get("connection_options", [])
    if not isinstance(options, dict) or set(options) - set(allowed):
        raise ValueError("connection options are not declared editable by the adapter")
    _check_options(options, editable=True)
    if metadata.get("provider") == "langfuse" and "trace_names" in options:
        options["trace_names"] = list(trace_names(multiple=options["trace_names"]))
        if not options["trace_names"]:
            raise ValueError("trace_names must select at least one Langfuse Trace name")
    reserved = set(profiles[profile]["secret_refs"]) | set(profiles[profile]["env_refs"])
    if reserved.intersection(options):
        raise ValueError("editable options cannot override declared credentials")
    encoded = json.dumps(options, allow_nan=False).encode()
    if len(encoded) > 65536:
        raise ValueError("connection settings exceed 64 KiB")
    return {"profile": profile, "options": options}


def expanded_settings(settings, metadata):
    normalized = normalize_settings(settings, metadata)
    value = declared_profiles(metadata)[normalized["profile"]]
    value["options"].update(normalized["options"])
    return value
