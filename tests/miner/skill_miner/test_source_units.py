"""Exact source units preserve source text and semantic boundaries without widening evidence."""

from __future__ import annotations

import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_units import (
    PROTOCOL,
    freeze_source_units,
    load_source_unit,
    load_source_units,
    source_units,
)

URI = "viking://resources/original.md"


def chunks_for(text, uri=URI, *, max_chars=800, start=0):
    return [dict(chunk, ordinal=start + index)
            for index, chunk in enumerate(planning.source_chunks(text, uri, max_chars=max_chars))]


def frozen(root, texts, *, max_chars=800):
    chunks = []
    for index, text in enumerate(texts):
        chunks.extend(chunks_for(text, f"viking://resources/source-{index}.md", max_chars=max_chars, start=len(chunks)))
    for chunk in chunks:
        _save(root / "chunks" / f"{chunk['ordinal']:08d}.json", chunk)
    state = {"source_unit_protocol": PROTOCOL, "chunk_count": len(chunks)}
    manifest = freeze_source_units(root, state)
    return state, manifest


@pytest.mark.parametrize("text", [
    "", "只有正文🙂\r\n第二行\r\n\r\n最后一段", "# 标题\n\n段落 A\n续行\n\n段落 B\n",
    "章节\n====\n\n## 下级\n\n### 细节\n内容\n## 其他\n正文\n",
    "| 列 |\n| --- |\n| 一 |\n| 二 |\n",
    "```md\n# 不是标题\n\n| a | b |\n|---|---|\n```\n\n真正正文\n",
    "~~~text\n未关闭代码\n\n全部仍为代码\n",
    "    code()\n\n    more()\n\n正文\n",
])
def test_units_roundtrip_unicode_offsets_hashes_and_empty_sources(text):
    chunks = chunks_for(text)
    units = source_units(text, URI, chunks)
    assert "".join(unit["content"] for unit in units) == text
    assert [unit["ordinal"] for unit in units] == list(range(len(units)))
    assert units[0]["start_char"] == 0 and units[-1]["end_char"] == len(text)
    planning.validate_chunks(units)
    for unit in units:
        assert unit["content"] == text[unit["start_char"]:unit["end_char"]]
        assert unit["ordinals"]


def test_complete_table_row_crosses_chunks_and_keeps_conditions_without_adjacent_rows(tmp_path):
    long_row = "| 条件甲 | " + "先获得同意再执行；" * 100 + "结束条件。 |\r\n"
    text = "# 章节\r\n\r\n| 名称 | 方法 |\r\n| --- | --- |\r\n" + long_row + "| 条件乙 | 另一方法 |\r\n"
    _, manifest = frozen(tmp_path, [text])
    all_units = load_source_units(tmp_path, list(range(manifest["unit_count"])))
    rows = [unit for unit in all_units if unit["kind"] == "table_row"]
    assert len(rows) == 2 and rows[0]["content"] == long_row and len(rows[0]["ordinals"]) > 1
    selected = load_source_units(tmp_path, [rows[0]["id"]], include_context=True)
    assert [unit["kind"] for unit in selected] == ["heading", "table_header", "table_row"]
    assert rows[1]["id"] not in {unit["id"] for unit in selected}
    assert load_source_unit(tmp_path, rows[0]["id"]) == load_source_unit(tmp_path, rows[0]["unit_index"])


def test_heading_ancestry_and_table_headers_do_not_escape_their_original_file(tmp_path):
    _, manifest = frozen(tmp_path, ["# A\n## B\n段落甲\n# C\n段落乙\n", "无标题段落\n"])
    units = load_source_units(tmp_path, list(range(manifest["unit_count"])))
    first = next(unit for unit in units if unit["content"] == "段落甲\n")
    second = next(unit for unit in units if unit["content"] == "段落乙\n")
    assert [unit["content"] for unit in load_source_units(tmp_path, [first["id"]], include_context=True)] == [
        "# A\n", "## B\n", "段落甲\n",
    ]
    assert [unit["content"] for unit in load_source_units(tmp_path, [second["id"]], include_context=True)] == [
        "# C\n", "段落乙\n",
    ]
    assert units[-1]["context_unit_ids"] == []


def test_fenced_and_indented_code_preserve_internal_blank_lines_and_table_syntax():
    for code in ("```md\n\n| 标题 |\n| --- |\n# 文本\n```\n", "    run()\n\n\tcontinue()\n"):
        text = code + "\n正文\n"
        units = source_units(text, URI, chunks_for(text))
        assert units[0]["kind"] == "code_block" and units[0]["content"] == code
        assert units[-1]["kind"] == "paragraph"


@pytest.mark.parametrize("target", ["unit", "chunk"])
def test_loading_rejects_modified_unit_or_original_chunk(tmp_path, target):
    _, manifest = frozen(tmp_path, ["# 标题\n正文\n"])
    path = tmp_path / ("source-units/records/00000001.json" if target == "unit" else "chunks/00000000.json")
    record = _read(path)
    record["content"] += "修改"
    _save(path, record)
    with pytest.raises(ValueError):
        load_source_unit(tmp_path, manifest["units"][1]["id"])


def test_freeze_is_idempotent_and_never_rewrites_original_chunks(tmp_path):
    state, manifest = frozen(tmp_path, ["段落甲\n\n段落乙\n", "其他原文"])
    before = {path.name: path.read_bytes() for path in (tmp_path / "chunks").glob("*.json")}
    assert freeze_source_units(tmp_path, state) == manifest
    assert {path.name: path.read_bytes() for path in (tmp_path / "chunks").glob("*.json")} == before
    with pytest.raises(ValueError, match="protocol"):
        freeze_source_units(tmp_path, {**state, "source_unit_protocol": "unknown"})


@pytest.mark.parametrize("mutate", ["gap", "hash", "duplicate", "file"])
def test_parser_rejects_noncanonical_original_chunk_coverage(mutate):
    text = "来源正文" * 800
    chunks = copy.deepcopy(chunks_for(text))
    if mutate == "gap":
        chunks[1]["start_char"] += 1
    elif mutate == "hash":
        chunks[1]["source_sha256"] = "bad"
    elif mutate == "duplicate":
        chunks[1]["ordinal"] = chunks[0]["ordinal"]
    else:
        chunks[1]["source_uri"] = "viking://resources/foreign.md"
    with pytest.raises(ValueError):
        source_units(text, URI, chunks)
