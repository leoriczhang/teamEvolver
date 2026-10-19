"""Bounded insertions into a frozen integrated draft, retaining every existing byte.

This is a recovery transport for an already accepted integrated draft. It is not
a source migration: current raw chunks and their coverage remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import bounded_compile_plan as planning
from .output_language import validate_artifact_headings

PROTOCOL = "te-integrated-body-insert.v1"
SCHEMA = "skill-miner.body-insert.v1"
CAPACITY_SCHEMA = "skill-miner.body-insert.capacity.v1"
PROJECTION_SCHEMA = "skill-miner.integrated-body.v2"
MAX_BODY_CHARS = 60000
RULE = """For packets with derived_controls.body_insert, use the following contract instead of
rewriting a complete Skill. previous_body is the complete generated draft for THIS candidate and round,
not new Evidence. Read it completely and inspect all current raw Evidence. Return exactly schema_version
skill-miner.body-insert.v1, base_body_sha256, input_ids, title, insert and no_change_reason. Preserve the
exact frozen Chinese title, base hash and ALL current input IDs. insert is a list of objects with exactly
anchor_id, text, support_ids. Each anchor_id comes from body_insert.anchors; the host inserts text at the
END of that section, before the next heading. Each text is at most 1200 characters and support_ids is a
nonempty subset of current raw input IDs. Only add genuinely new operational conditions, exceptions,
concise steps or parameterized examples within the candidate's task/trigger/boundary. Attach conditions
to their corresponding section. Do not repeat steps, copy whole dialogues, or introduce neighboring
workflows or persona versions. Never return old paragraphs or another complete body. The host preserves
all existing text, timing, consent prerequisites, citations and unresolved issues byte for byte. If raw
Evidence conflicts with a draft claim, add an explicit Chinese unresolved correction/conflict note at
that claim's section; do not treat the draft as trusted Evidence or invent a resolution. Keep optional
steps optional, additional time additional, example names/amounts/company claims as source parameters,
and training separate from real purchase/eligibility decisions. All new text/reasons/headings are Chinese.
No new rule: insert=[] and give a Chinese no_change_reason; otherwise no_change_reason="". The entire
JSON must fit 12000 characters. If CURRENT additions need a smaller source batch, return exactly
schema_version=skill-miner.body-insert.capacity.v1, base_body_sha256, input_ids, reason (Chinese, <=400).
Do not omit inputs, truncate raw sources, overwrite old conditions or return a whole-section revision.
Packets without body_insert retain the integrated-body contract below.
"""


def body_sha(previous: dict) -> str:
    text = json.dumps({key: previous.get(key, "") for key in ("title", "content")},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def anchors(previous: dict) -> list[dict]:
    """Full heading labels and offsets locate additions; the full draft is still sent."""
    content = previous["content"]
    headings = list(re.finditer(r"(?m)^#{1,6}[^\n]*", content))
    if not headings:
        return [{"id": "s0000", "heading": previous["title"], "end_char": len(content)}]
    return [{"id": f"s{index:04d}", "heading": heading.group(),
             "end_char": headings[index + 1].start() if index + 1 < len(headings) else len(content)}
            for index, heading in enumerate(headings)]


def controls(previous: dict, input_ids: list[str]) -> dict:
    if not previous.get("content"):
        raise ValueError("A body insertion requires an accepted complete draft")
    return {"protocol": PROTOCOL, "base_body_sha256": body_sha(previous), "title": previous["title"],
            "input_ids": input_ids, "anchors": anchors(previous), "previous_body_is_evidence": False}


def validate(output: str | dict, previous: dict, input_ids: list[str], policy: dict) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Body insertion JSON cannot repeat fields")
            value[key] = item
        return value

    if isinstance(output, str):
        if len(output) > planning.MAX_OUTPUT_CHARS:
            raise ValueError("Body insertion exceeds 12K characters")
        output = json.loads(output, object_pairs_hook=unique)
    if not isinstance(output, dict) or len(json.dumps(output, ensure_ascii=False)) > planning.MAX_OUTPUT_CHARS:
        raise ValueError("Body insertion must be a bounded complete JSON object")
    capacity = output.get("schema_version") == CAPACITY_SCHEMA
    fields = {"schema_version", "base_body_sha256", "input_ids"} | (
        {"reason"} if capacity else {"title", "insert", "no_change_reason"}
    )
    if (set(output) != fields or output.get("schema_version") != (CAPACITY_SCHEMA if capacity else SCHEMA)
            or output["base_body_sha256"] != body_sha(previous)
            or output["input_ids"] != input_ids):
        raise ValueError("Body insertion changed its exact draft, title or source assignment")
    if capacity:
        reason = output["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 400:
            raise ValueError("Body insertion capacity needs a bounded reason")
        validate_artifact_headings("", policy, title=reason)
        return output
    if output["title"] != previous["title"]:
        raise ValueError("Body insertion cannot rename the frozen method")
    rows, reason = output["insert"], output["no_change_reason"]
    if (not isinstance(rows, list) or len(rows) > 32 or not isinstance(reason, str)
            or len(reason) > 2000 or (bool(rows) == bool(reason.strip()))):
        raise ValueError("Body insertion needs additions or an explicit no-change reason")
    allowed = {row["id"] for row in anchors(previous)}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"anchor_id", "text", "support_ids"}:
            raise ValueError("Body insertion rows must retain their exact fields")
        text, support = row["text"], row["support_ids"]
        if (row["anchor_id"] not in allowed or not isinstance(text, str) or not text.strip()
                or len(text) > 1200 or text.lstrip().startswith("---")
                or not isinstance(support, list) or not support or len(set(support)) != len(support)
                or any(identity not in input_ids for identity in support)):
            raise ValueError("Body insertion escaped its section, source IDs or text limit")
        normalized = text.strip()
        if normalized in seen or normalized in previous["content"]:
            raise ValueError("Body insertion repeats an existing paragraph")
        seen.add(normalized)
        validate_artifact_headings(text, policy, title=text)
    if reason:
        validate_artifact_headings("", policy, title=reason)
    return output


def project(patch: dict, previous: dict, input_ids: list[str], policy: dict) -> dict:
    patch = validate(patch, previous, input_ids, policy)
    if patch["schema_version"] == CAPACITY_SCHEMA:
        raise ValueError("Capacity response cannot become an accepted body revision")
    if not patch["insert"]:
        return {"schema_version": planning.SECTION_SKIP_SCHEMA, "input_ids": input_ids,
                "reason": patch["no_change_reason"]}
    offsets = {row["id"]: row["end_char"] for row in anchors(previous)}
    additions = {}
    for row in patch["insert"]:
        additions.setdefault(offsets[row["anchor_id"]], []).append(row["text"])
    content = previous["content"]
    for offset in sorted(additions, reverse=True):
        content = content[:offset] + "\n\n" + "\n\n".join(additions[offset]) + "\n\n" + content[offset:]
    if len(content) > MAX_BODY_CHARS:
        raise ValueError("完整正文超过本地有界容量；未裁剪或丢弃旧条件")
    validate_artifact_headings(content, policy, title=previous["title"])
    return {"schema_version": PROJECTION_SCHEMA, "input_ids": input_ids, "title": previous["title"],
            "content": content, "patch": patch}


def validate_projection(value: dict, previous: dict) -> dict:
    if value != project(value.get("patch"), previous, value.get("input_ids"), {"language": "zh-CN"}):
        raise ValueError("Accepted body insertion projection changed")
    return value
