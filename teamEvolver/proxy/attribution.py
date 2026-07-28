"""Skill attribution from tool calls.

Pure functions that map a model's tool calls to the skills it read or
modified: file-path extraction (patch / shell / args dict), Hermes-native
skill-name resolution, path→skill lookup, and false-positive pruning of
failed Hermes skill writes.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_READ_TOOL_NAMES = {"read", "file_read", "read_file", "readfile"}
_HERMES_SKILL_READ_TOOL_NAMES = {"skill_view"}
_CLAUDE_CODE_SKILL_TOOL_NAMES = {"skill"}
_SKILL_WRITE_TOOL_NAMES = {
    "write",
    "file_write",
    "write_file",
    "writefile",
    "create_file",
    "edit",
    "edit_file",
    "replace",
    "replace_in_file",
    "append",
    "append_file",
    "patch",
    "apply_patch",
    "move",
    "rename",
    "mv",
}
_HERMES_SKILL_WRITE_TOOL_NAMES = {"skill_manage"}
_SHELL_TOOL_NAMES = {"shell", "exec", "bash", "terminal"}
_PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_SHELL_SKILL_PATH_RE = re.compile(
    r"([~./A-Za-z0-9_\-][^\n\"'`]*?"
    r"(?:SKILL\.md|references/[^\s\"'`]+|scripts/[^\s\"'`]+|assets/[^\s\"'`]+|history/[^\s\"'`]+))"
)


def _normalize_tool_call_name(name: str) -> str:
    return str(name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _extract_skill_names(items: list[Any] | None) -> set[str]:
    names: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            raw = item.get("skill_name") or item.get("name") or item.get("skill")
        else:
            raw = item
        name = str(raw or "").strip()
        if name:
            names.add(name)
    return names


def _extract_modified_skill_names(turns: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for turn in turns or []:
        if isinstance(turn, dict):
            names.update(_extract_skill_names(turn.get("modified_skills")))
    return names


def _deduplicate_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for path in paths:
        clean = str(path or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        return False
    return "/" in text or "\\" in text or text.startswith("~") or text.endswith("SKILL.md")


def _extract_skill_paths_from_patch(raw_text: str) -> list[str]:
    return _deduplicate_paths(
        [match.group(1).strip() for match in _PATCH_PATH_RE.finditer(str(raw_text or "")) if match.group(1).strip()]
    )


def _extract_skill_paths_from_shell(command: str) -> list[str]:
    return _deduplicate_paths(
        [
            match.group(1).strip()
            for match in _SHELL_SKILL_PATH_RE.finditer(str(command or ""))
            if match.group(1).strip()
        ]
    )


def _extract_skill_paths_from_args_dict(args: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in (
        "path",
        "file",
        "file_path",
        "target",
        "destination",
        "dest",
        "to",
        "source",
        "src",
        "old_path",
        "new_path",
    ):
        value = args.get(key)
        if isinstance(value, str) and _looks_like_path(value):
            paths.append(value.strip())

    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        for item in raw_paths:
            if isinstance(item, str) and _looks_like_path(item):
                paths.append(item.strip())
    return _deduplicate_paths(paths)


def _extract_skill_paths_from_tool_call(tool_call: dict) -> tuple[str, list[str]]:
    func = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    tool_name = _normalize_tool_call_name(func.get("name") or "")
    args_raw = func.get("arguments", "{}")
    if not isinstance(args_raw, str):
        try:
            args_raw = json.dumps(args_raw, ensure_ascii=False)
        except Exception:
            args_raw = "{}"

    paths: list[str] = []
    args_obj: Any = None
    try:
        args_obj = json.loads(args_raw)
    except Exception:
        args_obj = None

    if isinstance(args_obj, dict):
        paths.extend(_extract_skill_paths_from_args_dict(args_obj))
        if tool_name.lower() in _SHELL_TOOL_NAMES:
            command = str(args_obj.get("command") or args_obj.get("cmd") or "")
            paths.extend(_extract_skill_paths_from_shell(command))

    if tool_name.lower() in {"apply_patch", "patch"}:
        paths.extend(_extract_skill_paths_from_patch(args_raw))
    elif tool_name.lower() in _SHELL_TOOL_NAMES:
        paths.extend(_extract_skill_paths_from_shell(args_raw))

    return tool_name, _deduplicate_paths(paths)


def _extract_hermes_skill_name_from_tool_call(tool_call: dict) -> tuple[str, str, str]:
    """Extract Hermes-native skill name + relative file path from skill calls."""
    func = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
    tool_name = _normalize_tool_call_name(func.get("name") or "")
    args_raw = func.get("arguments", "{}")
    if not isinstance(args_raw, str):
        try:
            args_raw = json.dumps(args_raw, ensure_ascii=False)
        except Exception:
            args_raw = "{}"

    try:
        args_obj = json.loads(args_raw)
    except Exception:
        args_obj = {}

    if not isinstance(args_obj, dict):
        return tool_name, "", ""

    rel_path = ""
    for key in ("file_path", "path"):
        value = args_obj.get(key)
        if isinstance(value, str) and value.strip():
            rel_path = value.strip()
            break
    for key in ("skill_name", "name", "skill"):
        value = args_obj.get(key)
        if isinstance(value, str) and value.strip():
            return tool_name, value.strip(), rel_path
    return tool_name, "", rel_path


def _resolve_skill_reference(
    path: str,
    skill_path_map: dict[str, dict[str, str]],
) -> dict[str, str]:
    expanded = os.path.expanduser(str(path or "").strip())
    real_path = os.path.realpath(expanded) if expanded else ""
    skill_info = (
        skill_path_map.get(real_path) or skill_path_map.get(expanded) or skill_path_map.get(str(path or "").strip())
    )
    if skill_info:
        return {
            "skill_id": str(skill_info.get("skill_id", "") or ""),
            "skill_name": str(skill_info.get("skill_name", "") or ""),
            "path": str(path or "").strip(),
        }
    return {
        "skill_id": "",
        "skill_name": "",
        "path": str(path or "").strip(),
    }


def _resolve_skill_reference_by_name(
    skill_name: str,
    skill_path_map: dict[str, dict[str, str]],
    rel_path: str = "",
) -> dict[str, str]:
    clean_name = str(skill_name or "").strip()
    if not clean_name:
        return {"skill_id": "", "skill_name": "", "path": ""}
    normalized_rel = str(rel_path or "").strip().replace("\\", "/").lstrip("./")
    if normalized_rel:
        suffix = f"/{normalized_rel}"
        for path, skill_info in skill_path_map.items():
            if str(skill_info.get("skill_name", "") or "").strip() != clean_name:
                continue
            candidate = str(path or "").replace("\\", "/")
            if candidate.endswith(suffix) or candidate == normalized_rel:
                return {
                    "skill_id": str(skill_info.get("skill_id", "") or ""),
                    "skill_name": clean_name,
                    "path": str(path or ""),
                }
    for path, skill_info in skill_path_map.items():
        if str(skill_info.get("skill_name", "") or "").strip() == clean_name:
            return {
                "skill_id": str(skill_info.get("skill_id", "") or ""),
                "skill_name": clean_name,
                "path": str(path or ""),
            }
    return {"skill_id": "", "skill_name": clean_name, "path": ""}


def _extract_read_skills_from_tool_calls(
    tool_calls: list[dict],
    skill_path_map: dict[str, dict[str, str]],
) -> list[dict]:
    """Identify which skill bundle files were read from the model's tool_calls.

    Returns a list of ``{"skill_id": ..., "skill_name": ...}`` dicts for
    each ``read`` tool call whose ``path`` argument points inside a skill.
    """
    read_skills: list[dict] = []
    seen_ids: set[str] = set()
    for tc in tool_calls:
        tool_name, skill_paths = _extract_skill_paths_from_tool_call(tc)
        normalized = tool_name.lower()
        if normalized in _HERMES_SKILL_READ_TOOL_NAMES:
            _, skill_name, rel_path = _extract_hermes_skill_name_from_tool_call(tc)
            skill_ref = _resolve_skill_reference_by_name(skill_name, skill_path_map, rel_path)
            dedupe_key = skill_ref.get("skill_id") or skill_ref.get("skill_name")
            if dedupe_key and dedupe_key not in seen_ids:
                read_skills.append(skill_ref)
                seen_ids.add(dedupe_key)
            continue
        if normalized in _CLAUDE_CODE_SKILL_TOOL_NAMES:
            _, skill_name, rel_path = _extract_hermes_skill_name_from_tool_call(tc)
            skill_ref = _resolve_skill_reference_by_name(skill_name, skill_path_map, rel_path)
            dedupe_key = skill_ref.get("skill_id") or skill_ref.get("skill_name")
            if dedupe_key and dedupe_key not in seen_ids:
                read_skills.append(skill_ref)
                seen_ids.add(dedupe_key)
            continue
        if normalized not in _READ_TOOL_NAMES:
            continue
        for path in skill_paths:
            skill_ref = _resolve_skill_reference(path, skill_path_map)
            if not skill_ref.get("skill_id") and not skill_ref.get("skill_name"):
                continue
            dedupe_key = skill_ref.get("skill_id") or skill_ref.get("path") or skill_ref.get("skill_name")
            if not dedupe_key or dedupe_key in seen_ids:
                continue
            read_skills.append(skill_ref)
            seen_ids.add(dedupe_key)

    return read_skills


def _extract_modified_skills_from_tool_calls(
    tool_calls: list[dict],
    skill_path_map: dict[str, dict[str, str]],
) -> list[dict]:
    """Identify skill bundle files the model attempted to write or update."""
    modified_skills: list[dict] = []
    seen_ids: set[str] = set()
    for tc in tool_calls:
        tool_name, skill_paths = _extract_skill_paths_from_tool_call(tc)
        normalized = tool_name.lower()
        if normalized in _READ_TOOL_NAMES:
            continue
        if normalized in _HERMES_SKILL_WRITE_TOOL_NAMES:
            _, skill_name, rel_path = _extract_hermes_skill_name_from_tool_call(tc)
            skill_ref = _resolve_skill_reference_by_name(skill_name, skill_path_map, rel_path)
            dedupe_key = skill_ref.get("skill_id") or skill_ref.get("skill_name")
            if dedupe_key and dedupe_key not in seen_ids:
                modified_skills.append({**skill_ref, "action": normalized})
                seen_ids.add(dedupe_key)
            continue
        if normalized not in _SKILL_WRITE_TOOL_NAMES and normalized not in _SHELL_TOOL_NAMES:
            continue
        for path in skill_paths:
            skill_ref = _resolve_skill_reference(path, skill_path_map)
            if not skill_ref.get("skill_id") and not skill_ref.get("skill_name"):
                continue
            dedupe_key = skill_ref.get("skill_id") or skill_ref.get("path") or skill_ref.get("skill_name")
            if not dedupe_key or dedupe_key in seen_ids:
                continue
            modified_skills.append(
                {
                    **skill_ref,
                    "action": "shell" if normalized in _SHELL_TOOL_NAMES else normalized,
                }
            )
            seen_ids.add(dedupe_key)
    return modified_skills


def _drop_failed_hermes_skill_writes(turn_record: dict, summaries: list[dict]) -> None:
    """Remove failed Hermes-native skill writes from modified-skill attribution.

    The proxy injects skills as file paths. If the model calls Hermes' native
    ``skill_manage`` tool against such a skill, Hermes may fail with
    "Skill ... not found" because the skill is not installed in the active
    Hermes profile. That tool call must not count as a successful proxy
    skill modification, or evolve will learn from a false positive.
    """
    modified = turn_record.get("modified_skills")
    tool_calls = turn_record.get("tool_calls")
    if not isinstance(modified, list) or not isinstance(tool_calls, list):
        return

    failed_call_ids: set[str] = set()
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        if str(summary.get("tool_name") or "").lower() not in _HERMES_SKILL_WRITE_TOOL_NAMES:
            continue
        content = str(summary.get("content") or "").lower()
        if not summary.get("has_error") and '"success": false' not in content and "'success': false" not in content:
            continue
        if "not found" not in content and '"success": false' not in content and "'success': false" not in content:
            continue
        call_id = str(summary.get("tool_call_id") or "").strip()
        if call_id:
            failed_call_ids.add(call_id)

    if not failed_call_ids:
        return

    failed_names: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        if str(tool_call.get("id") or "").strip() not in failed_call_ids:
            continue
        tool_name, skill_name, _ = _extract_hermes_skill_name_from_tool_call(tool_call)
        if str(tool_name or "").lower() in _HERMES_SKILL_WRITE_TOOL_NAMES and skill_name:
            failed_names.add(skill_name)

    if failed_names:
        kept: list[dict] = []
        for item in modified:
            if not isinstance(item, dict):
                continue
            if str(item.get("skill_name") or "") in failed_names:
                continue
            kept.append(item)
        turn_record["modified_skills"] = kept
