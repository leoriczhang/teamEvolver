"""Project source-derived Case materials into real, immutable candidate files."""

from __future__ import annotations

import base64
import copy
import hashlib
import re

from team_replay.datasets.schema import normalize_document

from .bounded_compile_plan import normalize_case_materials
from .storage import file_path


def inline_path(case_id, content):
    identifier = str(case_id)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}", identifier):
        identifier = hashlib.sha256(identifier.encode()).hexdigest()
    return f"references/mining-materials/{identifier}/{hashlib.sha256(content).hexdigest()}.txt"


def _add_file(bundle, path, content):
    if path in bundle and bundle[path] != content:
        raise ValueError("Case material collides with an existing candidate file")
    if any(other != path and (other.startswith(path + "/") or path.startswith(other + "/")) for other in bundle):
        raise ValueError("Case material path collides with a candidate directory")
    bundle[path] = content


def materialize(dataset, bundle):
    """Preserve text and disclosure metadata; require every reference to exist."""
    document, result = copy.deepcopy(dataset), dict(bundle)
    for index, raw in enumerate(document["cases"]):
        case, _ = normalize_case_materials(raw)
        materials = case.get("materials", [])
        if not isinstance(materials, list):
            raise ValueError("Case materials must be a list or an inline text/object")
        normalized, seen = [], set()
        for value in materials:
            item = {"content": value} if isinstance(value, str) else copy.deepcopy(value)
            if not isinstance(item, dict):
                raise ValueError("Case material must be text or a file reference")
            inline = item.get("content")
            if "content" in item and not isinstance(inline, str):
                raise ValueError("Inline Case material content must be text")
            path = item.get("path")
            if not path:
                if not isinstance(inline, str):
                    raise ValueError("Inline Case material requires source content")
                content = inline.encode("utf-8")
                path = inline_path(case["case_id"], content)
            else:
                if not isinstance(path, str):
                    raise ValueError("Case material path must be text")
                path = file_path(path)
                content = inline.encode("utf-8") if isinstance(inline, str) else result.get(path)
                if content is None:
                    raise ValueError("Case material file is missing from the candidate bundle")
            _add_file(result, path, content)
            sha = hashlib.sha256(content).hexdigest()
            if item.get("sha256") and item["sha256"] != sha:
                raise ValueError("Case material content hash mismatch")
            if "size" in item and item["size"] != len(content):
                raise ValueError("Case material content size mismatch")
            if "content_b64" in item:
                try:
                    encoded = base64.b64decode(item["content_b64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("Case material base64 is invalid") from exc
                if encoded != content:
                    raise ValueError("Case material content differs from its candidate file")
            if path in seen:
                raise ValueError("Duplicate Case material reference")
            seen.add(path)
            normalized.append({**{key: value for key, value in item.items()
                                  if key not in {"content", "content_b64"}},
                               "path": path, "sha256": sha, "size": len(content)})
        case["materials"] = normalized
        document["cases"][index] = case
    return normalize_document(document), result


def hydrate(cases, bundle, *, maximum):
    """Replay receives verified bytes for both branches, including Baseline."""
    result = copy.deepcopy(cases)
    total = 0
    for case in result:
        for item in case["materials"]:
            content = bundle.get(item["path"])
            if content is None:
                raise ValueError("Case material was excluded from the canonical candidate bundle")
            total += len(content)
            if total > maximum:
                raise ValueError("Replay Case materials exceed the candidate bundle byte limit")
            item["content_b64"] = base64.b64encode(content).decode("ascii")
    return result
