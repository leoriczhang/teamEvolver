"""语料加载：从 PDF/DOCX/XLSX/Markdown/HTML/TXT 抽取纯文本。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".xlsx"}


@dataclass
class Doc:
    path: Path
    text: str

    @property
    def doc_id(self) -> str:
        return hashlib.sha1(str(self.path.resolve()).encode("utf-8")).hexdigest()[:12]


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        raw = path.read_bytes()
        for encoding in ("utf-8", "gb18030", "utf-16", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    if suffix in {".html", ".htm"}:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(path.read_bytes(), "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n")
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        from docx import Document

        document = Document(str(path))
        parts: list[str] = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append("\t".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    if suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in workbook.worksheets:
            lines.append(f"# sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(cell).strip() if cell is not None else "" for cell in row]
                if any(values):
                    lines.append("\t".join(values))
        return "\n".join(lines)
    raise ValueError(f"unsupported file type: {suffix} ({path.name})")


def load_corpus(path: Path, suffixes: set[str] | None = None) -> list[Doc]:
    """加载目录（递归）或单文件，返回文档列表。"""
    suffixes = suffixes or SUPPORTED_SUFFIXES
    target = Path(path)
    files: list[Path]
    if target.is_dir():
        files = sorted(p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in suffixes)
    elif target.is_file():
        files = [target]
    else:
        raise FileNotFoundError(f"corpus path not found: {target}")
    if not files:
        raise ValueError(f"no supported documents found under {target}")
    docs: list[Doc] = []
    for file in files:
        try:
            docs.append(Doc(file, extract_text(file)))
        except Exception as exc:  # 单文件失败不阻断整体
            raise ValueError(f"failed to extract {file}: {exc}") from exc
    return docs


def load_tool_catalog(path: Path) -> dict[str, dict]:
    """加载工具目录：支持 {tool_name: schema} 或 MCP tools_json 数组形式。"""
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and all(isinstance(v, dict) for v in payload.values()):
        return payload
    if isinstance(payload, list):
        result: dict[str, dict] = {}
        for item in payload:
            name = item.get("name") or (item.get("function") or {}).get("name")
            if name:
                result[name] = item
        if result:
            return result
    raise ValueError(
        f"unsupported tool catalog format in {path}: expected {{tool_name: schema}} dict or MCP tools_json list"
    )
