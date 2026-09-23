"""分块：标题感知切分，稳定 chunk id（doc 哈希 + 序号）。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_HTML_HEADING_RE = re.compile(r"^\s*<h([1-6])[^>]*>(.*?)</h\1>\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    doc: str
    index: int
    heading: str
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


def _doc_hash(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def _split_by_heading(text: str, doc_id: str) -> list[tuple[str, str]]:
    """返回 [(heading, section_text)]；无标题文档归入 '' 标题段。"""
    pattern = _HEADING_RE if text.lstrip().startswith("#") or "##" in text[:200] else None
    if pattern is None:
        return [("", text)]
    sections: list[tuple[str, str]] = []
    current_heading = ""
    current_body: list[str] = []
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            if current_body or current_heading:
                sections.append((current_heading, "\n".join(current_body)))
            current_heading = f"{match.group(1)} {match.group(2)}"
            current_body = []
        else:
            current_body.append(line)
    if current_body or current_heading:
        sections.append((current_heading, "\n".join(current_body)))
    return sections


def _split_long(section: str, max_chars: int, overlap: int) -> list[str]:
    text = section.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    paragraphs = re.split(r"\n\s*\n|\n(?=[-•\d一二三四五六七八九十]+[、.．)）]\s*)", text)
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for i in range(0, len(para), max_chars - overlap):
                chunks.append(para[i : i + max_chars])
            continue
        if buffer and len(buffer) + len(para) + 1 > max_chars:
            chunks.append(buffer)
            tail = buffer[-overlap:] if overlap else ""
            buffer = tail + para if tail else para
        else:
            buffer = f"{buffer}\n{para}" if buffer else para
    if buffer:
        chunks.append(buffer)
    return chunks


def chunk_doc(doc_id: str, doc: str, text: str, max_chars: int = 2000, overlap: int = 200) -> list[Chunk]:
    chunks: list[Chunk] = []
    index = 0
    for heading, section in _split_by_heading(text, doc_id):
        for piece in _split_long(section, max_chars, overlap):
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_id}-{index:04d}",
                    doc_id=doc_id,
                    doc=doc,
                    index=index,
                    heading=heading,
                    text=piece,
                )
            )
            index += 1
    return chunks


def chunk_docs(docs: list[tuple[str, str, str]], max_chars: int = 2000, overlap: int = 200) -> list[Chunk]:
    """docs: [(doc_id, doc_name, text)]"""
    result: list[Chunk] = []
    for doc_id, doc, text in docs:
        result.extend(chunk_doc(doc_id, doc, text, max_chars, overlap))
    return result


def doc_id_for_path(path: str) -> str:
    return _doc_hash(path)
