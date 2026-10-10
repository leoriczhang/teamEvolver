"""Convert bounded Office, PDF and text uploads to UTF-8 Markdown before OpenViking import."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from xml.etree import ElementTree

SUPPORTED_UPLOAD_SUFFIXES = {".md", ".markdown", ".txt", ".docx", ".xlsx", ".pdf", ".csv", ".pptx"}
MAX_ARCHIVE_FILES = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_NORMALIZED_DOCUMENT_CHARS = 5_000_000
MAX_SPREADSHEET_CELLS = 200_000
MAX_PDF_PAGES = 2_000


@dataclass(frozen=True)
class ConvertedKnowledgeUpload:
    markdown: str
    source_format: str
    source_encoding: str


def _validate_archive_entries(entries: list[zipfile.ZipInfo], filename: str) -> None:
    if len(entries) > MAX_ARCHIVE_FILES:
        raise ValueError(f"{filename} 内部文件数量过多")
    if sum(entry.file_size for entry in entries) > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        raise ValueError(f"{filename} 解压后超过 100 MB")
    if any(entry.flag_bits & 0x1 for entry in entries):
        raise ValueError(f"{filename} 已加密，暂不支持解析")



def _decode_text(raw: bytes) -> tuple[str, str]:
    if not raw:
        raise ValueError("不允许上传空文件")
    if b"\x00" in raw:
        raise ValueError("文件包含二进制内容，请检查文件格式")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文件编码，请转换为 UTF-8 文本后上传")


def _validate_office_archive(raw: bytes, filename: str) -> None:
    """Reject malformed/encrypted/oversized Office ZIP containers early."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            _validate_archive_entries(entries, filename)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{filename} 不是有效的 Office 文件") from exc


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _finish_markdown(text: str, filename: str) -> str:
    text = _normalize_newlines(text).strip("\ufeff\n")
    if not re.sub(r"\s+", "", text):
        raise ValueError(f"{filename} 转换后没有可用文本")
    if len(text) > MAX_NORMALIZED_DOCUMENT_CHARS:
        raise ValueError(f"{filename} 转换后的文本超过 500 万字符，请拆分后上传")
    return text + "\n"


def _source_heading(filename: str) -> str:
    title = Path(filename).stem.replace("\n", " ").strip() or "知识文档"
    return f"# {title}"


def _markdown_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    else:
        text = str(value)
    return _normalize_newlines(text).replace("|", "\\|").replace("\n", "<br>").strip()


def _markdown_table(rows: list[list[object]]) -> str:
    cleaned: list[list[str]] = []
    width = 0
    for row in rows:
        cells = [_markdown_cell(value) for value in row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        cleaned.append(cells)
        width = max(width, len(cells))
    if not cleaned or width == 0:
        return ""

    padded = [row + [""] * (width - len(row)) for row in cleaned]
    header = padded[0]
    # Markdown requires a header row.  Empty source headers get stable labels,
    # while non-empty values are kept exactly as they appeared in the source.
    header = [value or f"列 {index + 1}" for index, value in enumerate(header)]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
    return "\n".join(lines)


def _word_to_markdown(raw: bytes, filename: str) -> str:
    _validate_office_archive(raw, filename)
    try:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - exercised by deployment only
        raise ValueError("服务器缺少 Word 解析组件，请重新安装项目依赖") from exc

    try:
        document = Document(io.BytesIO(raw))
    except Exception as exc:
        raise ValueError(f"{filename} 不是有效的 DOCX 文件") from exc

    parts = [_source_heading(filename)]
    content_count = 0
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph = Paragraph(child, document)
            text = paragraph.text.strip()
            if not text:
                continue
            content_count += 1
            style = paragraph.style
            style_name = (getattr(style, "name", "") or "").strip()
            style_id = (getattr(style, "style_id", "") or "").strip()
            heading_match = re.search(r"(?:heading|标题)\s*([1-9])", f"{style_name} {style_id}", re.I)
            if style_name.casefold() == "title" or style_id.casefold() == "title":
                parts.append(f"## {text}")
            elif heading_match:
                level = min(6, int(heading_match.group(1)) + 1)
                parts.append(f"{'#' * level} {text}")
            elif paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None:
                parts.append(f"- {text}")
            else:
                parts.append(text)
        elif isinstance(child, CT_Tbl):
            table = Table(child, document)
            rendered = _markdown_table([
                [cell.text for cell in row.cells]
                for row in table.rows
            ])
            if rendered:
                parts.append(rendered)
                content_count += 1
    if not content_count:
        raise ValueError(f"{filename} 转换后没有可用文本")
    return _finish_markdown("\n\n".join(parts), filename)


def _excel_to_markdown(raw: bytes, filename: str) -> str:
    _validate_office_archive(raw, filename)
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - exercised by deployment only
        raise ValueError("服务器缺少 Excel 解析组件，请重新安装项目依赖") from exc

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=False)
    except Exception as exc:
        raise ValueError(f"{filename} 不是有效的 XLSX 文件") from exc

    parts = [_source_heading(filename)]
    cell_count = 0
    content_count = 0
    try:
        for sheet in workbook.worksheets:
            rows: list[list[object]] = []
            for row in sheet.iter_rows(values_only=True):
                cell_count += len(row)
                if cell_count > MAX_SPREADSHEET_CELLS:
                    raise ValueError(f"{filename} 超过 20 万个单元格，请拆分后上传")
                values = list(row)
                while values and values[-1] is None:
                    values.pop()
                if values and any(value is not None and str(value).strip() for value in values):
                    rows.append(values)
            rendered = _markdown_table(rows)
            if rendered:
                parts.append(f"## 工作表：{sheet.title}\n\n{rendered}")
                content_count += 1
    finally:
        workbook.close()
    if not content_count:
        raise ValueError(f"{filename} 转换后没有可用文本")
    return _finish_markdown("\n\n".join(parts), filename)


def _pdf_to_markdown(raw: bytes, filename: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - exercised by deployment only
        raise ValueError("服务器缺少 PDF 解析组件，请重新安装项目依赖") from exc

    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError(f"{filename} 已加密，暂不支持解析")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError(f"{filename} 超过 2000 页，请拆分后上传")
        page_texts = [
            _normalize_newlines(page.extract_text() or "").strip()
            for page in reader.pages
        ]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{filename} 不是有效的 PDF 文件") from exc

    extracted = re.sub(r"\s+", "", "".join(page_texts))
    if not extracted:
        raise ValueError(f"{filename} 未检测到文本层，可能是扫描版 PDF，请先进行 OCR 后再上传")

    parts = [_source_heading(filename)]
    for index, text in enumerate(page_texts, start=1):
        if text:
            parts.append(f"## 第 {index} 页\n\n{text}")
    return _finish_markdown("\n\n".join(parts), filename)


def _csv_to_markdown(raw: bytes, filename: str) -> tuple[str, str]:
    text, encoding = _decode_text(raw)
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise ValueError(f"{filename} 不是有效的 CSV 文件") from exc
    if sum(len(row) for row in rows) > MAX_SPREADSHEET_CELLS:
        raise ValueError(f"{filename} 超过 20 万个单元格，请拆分后上传")
    table = _markdown_table(rows)
    if not table:
        raise ValueError(f"{filename} 转换后没有可用文本")
    return _finish_markdown(f"{_source_heading(filename)}\n\n{table}", filename), encoding


def _pptx_to_markdown(raw: bytes, filename: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            _validate_archive_entries(entries, filename)
            slide_names = sorted(
                (
                    entry.filename
                    for entry in entries
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", entry.filename)
                ),
                key=lambda value: int(re.search(r"(\d+)", Path(value).stem).group(1)),
            )
            slides: list[list[str]] = []
            for slide_name in slide_names:
                root = ElementTree.fromstring(archive.read(slide_name))
                slides.append([
                    str(node.text or "").strip()
                    for node in root.iter()
                    if node.tag.endswith("}t") and str(node.text or "").strip()
                ])
    except ValueError:
        raise
    except (zipfile.BadZipFile, ElementTree.ParseError, AttributeError) as exc:
        raise ValueError(f"{filename} 不是有效的 PPTX 文件") from exc

    parts = [_source_heading(filename)]
    for index, texts in enumerate(slides, start=1):
        if texts:
            parts.append(f"## 第 {index} 页\n\n" + "\n\n".join(texts))
    if len(parts) == 1:
        raise ValueError(f"{filename} 转换后没有可用文本")
    return _finish_markdown("\n\n".join(parts), filename)


def convert_knowledge_upload(filename: str, raw: bytes) -> ConvertedKnowledgeUpload:
    """Normalize a supported browser upload to one Markdown document."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        allowed = "、".join(sorted(SUPPORTED_UPLOAD_SUFFIXES))
        raise ValueError(f"不支持 {suffix or '无扩展名'} 文件；支持：{allowed}")

    if suffix == ".csv":
        markdown, encoding = _csv_to_markdown(raw, filename)
        converted = ConvertedKnowledgeUpload(markdown, "csv", encoding)
    elif suffix == ".pptx":
        converted = ConvertedKnowledgeUpload(_pptx_to_markdown(raw, filename), "pptx", "binary")
    elif suffix in {".md", ".markdown", ".txt"}:
        text, encoding = _decode_text(raw)
        if suffix == ".txt":
            if not re.sub(r"\s+", "", text):
                raise ValueError(f"{filename} 转换后没有可用文本")
            text = f"{_source_heading(filename)}\n\n{text}"
        converted = ConvertedKnowledgeUpload(
            _finish_markdown(text, filename), suffix.removeprefix("."), encoding
        )
    else:
        if not raw:
            raise ValueError("不允许上传空文件")
        convert_office = {".docx": _word_to_markdown, ".xlsx": _excel_to_markdown, ".pdf": _pdf_to_markdown}
        converted = ConvertedKnowledgeUpload(convert_office[suffix](raw, filename), suffix.removeprefix("."), "binary")

    if suffix in {".md", ".markdown"}:
        return converted
    metadata = (
        "---\n"
        f"source_file: {json.dumps(Path(filename).name, ensure_ascii=False)}\n"
        f"source_format: {converted.source_format}\n"
        f"converted_at: {datetime.now(timezone.utc).isoformat()}\n"
        "---\n\n"
    )
    return ConvertedKnowledgeUpload(
        metadata + converted.markdown,
        converted.source_format,
        converted.source_encoding,
    )
