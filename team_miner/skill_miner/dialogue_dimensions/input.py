"""Lossless whitelist import and Unicode sentence coordinates."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

SCHEMA = "taikang.dialogue-case.v1"
PREPARER = "taikang-dialogue-prepare.v1"
BUSINESS = ("course", "chapter", "knowledge_point", "scene", "dialogue_text")
COLUMNS = dict(zip(BUSINESS, ("课程名称", "章节名称", "知识点", "场景名称", "对话内容")))
META = ("schema_version", "case_ref", "case_version", *BUSINESS[:-1])
SPEAKER = re.compile(r"(?m)^(HWP|C)[：:]")
ENDING = re.compile(r"[。！？!?]+[”’\"」』]*")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def case_key(case):
    return case["case_ref"] + "@" + case["case_version"]


def identifier(value):
    if value is None:
        return ""
    if isinstance(value, (float, Decimal)) and value == int(value):
        return str(int(value))
    return str(value)


def sentences(text):
    markers = list(SPEAKER.finditer(text))
    turns = []
    if not markers or markers[0].start():
        turns.append((0, markers[0].start() if markers else len(text), "unknown"))
    for i, marker in enumerate(markers):
        turns.append((marker.end(), markers[i + 1].start() if i + 1 < len(markers) else len(text), marker[1]))
    result = []
    for turn, (start, end, speaker) in enumerate(turns, 1):
        cursor = start
        for match in ENDING.finditer(text, start, end):
            finish = match.end()
            if text[cursor:finish].strip():
                result.append({"sentence_id": f"s{len(result) + 1:06d}", "turn_index": turn, "speaker": speaker,
                               "char_start": cursor, "char_end": finish, "boundary_kind": "sentence"})
            cursor = finish
        while cursor < end and text[cursor] in "\r\n":
            cursor += 1
        finish = end
        while finish > cursor and text[finish - 1] in "\r\n":
            finish -= 1
        if text[cursor:finish].strip():
            result.append({"sentence_id": f"s{len(result) + 1:06d}", "turn_index": turn, "speaker": speaker,
                           "char_start": cursor, "char_end": finish, "boundary_kind": "utterance"})
    return result


def normalize_case(value):
    """Unknown fields and supplied indexes never cross the model boundary."""
    if value.get("schema_version", SCHEMA) != SCHEMA:
        raise ValueError("unsupported_case_schema")
    business = {key: value.get(key, "") for key in BUSINESS}
    if any(not isinstance(item, str) for item in business.values()) or not business["dialogue_text"].strip():
        raise ValueError("missing_or_non_text_dialogue")
    ref = value.get("case_ref", "")
    version = sha(canonical(business))
    if not re.fullmatch(r"case_[a-f0-9]{24,64}", ref):
        raise ValueError("invalid_case_ref")
    if value.get("case_version", version) != version:
        raise ValueError("case_version_mismatch")
    return {"schema_version": SCHEMA, "case_ref": ref, "case_version": version, **business,
            "sentence_index": sentences(business["dialogue_text"])}


def read_xlsx(source, *, namespace="taikang-bac", filename="", source_sha256=""):
    import openpyxl

    book = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        for sheet in book:
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, ())
            if COLUMNS["dialogue_text"] not in headers:
                continue
            for row_number, values in enumerate(rows, 2):
                row = dict(zip(headers, values))
                if all(item is None for item in values):
                    continue
                identity = {"dataset_namespace": namespace, "dialogue_id": identifier(row.get("对练id")),
                            "instance_id": identifier(row.get("实例id"))}
                issues = []
                if not identity["dialogue_id"] or not identity["instance_id"]:
                    identity = {"dataset_namespace": namespace, "fallback": [filename, sheet.title, row_number]}
                    issues.append("identity_fallback")
                business = {key: row.get(column) if row.get(column) is not None else ""
                            for key, column in COLUMNS.items()}
                origin = {"filename": filename, "source_sha256": source_sha256, "sheet": sheet.title,
                          "excel_row": row_number, "dialogue_column": COLUMNS["dialogue_text"],
                          "dialogue_id": identifier(row.get("对练id")), "instance_id": identifier(row.get("实例id"))}
                try:
                    case = normalize_case({**business, "case_ref": "case_" + sha(canonical(identity))})
                    # Use 96 identity bits; full identity digest remains available for conflict checks.
                    case["case_ref"] = case["case_ref"][:29]
                    yield case, origin, issues
                except ValueError as exc:
                    yield None, origin, [str(exc)]
    finally:
        book.close()


def read_jsonl(lines):
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        origin = {"jsonl_line": line_number}
        try:
            value = json.loads(line)
            case = normalize_case(value)
            yield case, value.get("origin", origin), []
        except (ValueError, TypeError, AttributeError) as exc:
            yield None, origin, [str(exc)]


def render(case):
    case = normalize_case(case)
    text = case["dialogue_text"]
    fence = "`" * max(3, max((len(x) for x in re.findall(r"`+", text)), default=0) + 1)
    return (f"# Case {case['case_ref']}\n\n## Metadata\n```json\n"
            + canonical({key: case[key] for key in META}) + "\n```\n\n## Dialogue (Evidence)\n"
            + fence + "text\n" + text + "\n" + fence + "\n\n## Sentence index\n```json\n"
            + canonical(case["sentence_index"]) + "\n```\n")


def parse_model(text):
    match = re.fullmatch(
        r"# Case (case_[a-f0-9]+)\n\n## Metadata\n```json\n(.+)\n```\n\n"
        r"## Dialogue \(Evidence\)\n(`{3,})text\n(.*)\n\3\n\n## Sentence index\n```json\n(.+)\n```\n",
        text, re.S,
    )
    if not match:
        raise ValueError("invalid_model_case_wrapper")
    meta = json.loads(match[2])
    if set(meta) != set(META) or meta["case_ref"] != match[1] or meta["schema_version"] != SCHEMA:
        raise ValueError("model_metadata_not_whitelisted")
    case = normalize_case({**meta, "dialogue_text": match[4]})
    if json.loads(match[5]) != case["sentence_index"] or render(case) != text:
        raise ValueError("frozen_sentence_index_or_wrapper_changed")
    return case
