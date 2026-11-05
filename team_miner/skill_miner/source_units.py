"""Exact Markdown source units; original chunks remain the immutable transport archive."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import bounded_compile_plan as planning

PROTOCOL = "te-exact-source-units.v1"


class SourceUnitCapacityError(ValueError):
    """A complete archived source unit needs a larger bounded assignment, not new Evidence."""


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _io():
    from .ov_staged_orchestration import _read, _save

    return _read, _save


def _table_separator(line: str) -> bool:
    cells = line.strip().strip("|").split("|")
    return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _heading(line: str) -> int:
    match = re.match(r"^ {0,3}(#{1,6})(?:\s+|$)", line)
    return len(match[1]) if match else 0


def source_units(text: str, source_uri: str, chunks: list[dict], *, start_index: int = 0) -> list[dict]:
    """Keep full table rows, paragraphs and fenced code with exact Unicode offsets and hashes."""
    if not isinstance(text, str) or not isinstance(source_uri, str) or not source_uri.startswith("viking://"):
        raise ValueError("Source units require exact original text and its Viking URI")
    if type(start_index) is not int or start_index < 0 or not chunks:
        raise ValueError("Source units require original chunk metadata and a valid start index")
    expected, source_hash = 0, _sha(text)
    for chunk in chunks:
        if (chunk.get("source_uri") != source_uri or chunk.get("source_sha256") != source_hash
                or chunk.get("start_char") != expected or type(chunk.get("ordinal")) is not int
                or type(chunk.get("end_char")) is not int or not expected <= chunk["end_char"] <= len(text)
                or chunk.get("sha256") != _sha(text[expected:chunk["end_char"]])):
            raise ValueError("Source units must match the complete contiguous frozen original chunks")
        expected = chunk["end_char"]
    if expected != len(text) or len({row["ordinal"] for row in chunks}) != len(chunks):
        raise ValueError("Source units require complete, unique frozen original coverage")

    lines = text.splitlines(keepends=True)
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    offsets.append(cursor)
    records, headings, table_header = [], [], None

    def emit(first: int, after: int, kind: str, level: int = 0) -> None:
        nonlocal table_header
        start, end = offsets[first], offsets[after]
        raw, index = text[start:end], start_index + len(records)
        digest = _sha(raw)
        identity = "src-" + _sha(_json([source_uri, start, end, digest]))[:24]
        if kind == "heading":
            while headings and headings[-1][0] >= level:
                headings.pop()
        context = [entry[1] for entry in headings]
        if kind == "table_row" and table_header:
            context.append(table_header)
        record = {
            "id": identity, "source_uri": source_uri, "start_char": start, "end_char": end,
            "sha256": digest, "source_sha256": source_hash, "content": raw,
            "unit_index": index, "ordinal": index, "kind": kind,
            "ordinals": [row["ordinal"] for row in chunks
                         if row["start_char"] < end and start < row["end_char"] or start == end == 0],
            "context_unit_ids": context,
        }
        records.append(record)
        if kind == "heading":
            headings.append((level, identity))
        if kind == "table_header":
            table_header = identity
        elif kind != "table_row":
            table_header = None

    i = 0
    while i < len(lines):
        first, line = i, lines[i]
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if fence and not (fence[1][0] == "`" and "`" in fence[2]):
            marker, width = fence[1][0], len(fence[1])
            closing = re.compile(r"^ {0,3}" + re.escape(marker) + "{" + str(width) + r",}\s*$")
            i += 1
            while i < len(lines):
                i += 1
                if closing.fullmatch(lines[i - 1].rstrip("\r\n")):
                    break
            emit(first, i, "code_block")
        elif not line.strip():
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            emit(first, i, "separator")
        elif line.startswith(("    ", "\t")):
            i += 1
            while i < len(lines):
                if lines[i].startswith(("    ", "\t")):
                    i += 1
                    continue
                after_blanks = i
                while after_blanks < len(lines) and not lines[after_blanks].strip():
                    after_blanks += 1
                if after_blanks < len(lines) and lines[after_blanks].startswith(("    ", "\t")):
                    i = after_blanks + 1
                    continue
                break
            emit(first, i, "code_block")
        elif _heading(line):
            i += 1
            emit(first, i, "heading", _heading(line))
        elif i + 1 < len(lines) and "|" in line and _table_separator(lines[i + 1]):
            i += 2
            emit(first, i, "table_header")
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                emit(i, i + 1, "table_row")
                i += 1
        elif i + 1 < len(lines) and re.fullmatch(r" {0,3}(?:=+|-+)\s*", lines[i + 1].rstrip("\r\n")):
            i += 2
            emit(first, i, "heading", 1 if "=" in lines[first + 1] else 2)
        else:
            i += 1
            while i < len(lines) and lines[i].strip():
                if (_heading(lines[i]) or re.match(r"^ {0,3}(?:`{3,}|~{3,})", lines[i])
                        or i + 1 < len(lines) and "|" in lines[i] and _table_separator(lines[i + 1])):
                    break
                i += 1
            emit(first, i, "paragraph")
    if not records:
        emit(0, 0, "empty")
    if "".join(row["content"] for row in records) != text:
        raise ValueError("Source-unit parser lost frozen source text")
    return records


def freeze_source_units(root: Path, state: dict) -> dict:
    """Archive semantic units from complete existing chunks without rewriting any original file."""
    read, save = _io()
    if state.get("source_unit_protocol") != PROTOCOL:
        raise ValueError("Exact source units require the frozen protocol flag")
    manifest_path = root / "source-units/manifest.json"
    if manifest_path.exists():
        manifest = read(manifest_path)
        if manifest.get("protocol") != PROTOCOL or manifest.get("chunk_count") != state["chunk_count"]:
            raise ValueError("Frozen source-unit manifest protocol or original chunk count changed")
        return manifest
    items, files, pending = [], [], []

    def flush() -> None:
        if not pending:
            return
        uri, text = pending[0]["source_uri"], "".join(row["content"] for row in pending)
        records = source_units(text, uri, pending, start_index=len(items))
        for unit in records:
            path = root / "source-units/records" / f"{unit['unit_index']:08d}.json"
            if path.exists() and read(path) != unit:
                raise ValueError("Frozen source-unit record changed during initialization")
            save(path, unit)
            items.append({"id": unit["id"], "index": unit["unit_index"], "sha256": _sha(_json(unit))})
        files.append({"source_uri": uri, "sha256": _sha(text), "chars": len(text),
                      "ordinals": [row["ordinal"] for row in pending]})
        pending.clear()

    seen_sources = set()
    for ordinal in range(state["chunk_count"]):
        chunk = read(root / "chunks" / f"{ordinal:08d}.json")
        planning.validate_chunks([chunk])
        if chunk.get("ordinal") != ordinal:
            raise ValueError("Frozen original chunk ordinal changed")
        if pending and chunk["source_uri"] != pending[0]["source_uri"]:
            flush()
        if not pending:
            if chunk["source_uri"] in seen_sources:
                raise ValueError("Original file chunks must remain contiguous in the frozen archive")
            seen_sources.add(chunk["source_uri"])
        pending.append(chunk)
    flush()
    manifest = {"protocol": PROTOCOL, "chunk_count": state["chunk_count"], "unit_count": len(items),
                "sources": files, "units": items, "id_to_index": {row["id"]: row["index"] for row in items}}
    if not items or len(manifest["id_to_index"]) != len(items):
        raise ValueError("Exact source-unit archive is empty or has duplicate identities")
    save(manifest_path, manifest)
    return manifest


def load_source_unit(root: Path, identity: str | int) -> dict:
    """Verify unit identity and full content against immutable original chunk slices."""
    read, _ = _io()
    manifest = read(root / "source-units/manifest.json")
    if manifest.get("protocol") != PROTOCOL:
        raise ValueError("Exact source-unit manifest is missing its frozen protocol")
    index = manifest["id_to_index"].get(identity) if isinstance(identity, str) else identity
    if type(index) is not int or not 0 <= index < manifest["unit_count"]:
        raise ValueError("Unknown exact source-unit identity")
    locator = manifest["units"][index]
    unit = read(root / "source-units/records" / f"{index:08d}.json")
    if (locator["index"] != index or locator["id"] != unit.get("id")
            or unit.get("unit_index") != index or unit.get("ordinal") != index
            or locator["sha256"] != _sha(_json(unit))):
        raise ValueError("Frozen exact source unit changed after archival")
    planning.validate_chunks([unit])
    parts = []
    for ordinal in unit["ordinals"]:
        chunk = read(root / "chunks" / f"{ordinal:08d}.json")
        planning.validate_chunks([chunk])
        if chunk["source_uri"] != unit["source_uri"] or chunk["source_sha256"] != unit["source_sha256"]:
            raise ValueError("Source unit escaped its original file or frozen hash")
        start, end = max(unit["start_char"], chunk["start_char"]), min(unit["end_char"], chunk["end_char"])
        parts.append(chunk["content"][start - chunk["start_char"]:end - chunk["start_char"]])
    if "".join(parts) != unit["content"]:
        raise ValueError("Exact source-unit content differs from the original archived slices")
    return unit


def load_source_units(root: Path, identities: list[str | int], *, include_context: bool = False) -> list[dict]:
    """Optionally include structural headings/table headers, never neighbouring scenario rows."""
    units = {unit["id"]: unit for unit in (load_source_unit(root, identity) for identity in identities)}
    if include_context:
        for unit in list(units.values()):
            for identity in unit["context_unit_ids"]:
                units.setdefault(identity, load_source_unit(root, identity))
    return sorted(units.values(), key=lambda unit: unit["unit_index"])
