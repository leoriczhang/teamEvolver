#!/usr/bin/env python3
"""
文档转换工具 - 将 Office/PDF/CSV 等格式转换为 Markdown

支持格式：
- Word: .docx, .doc
- Excel: .xlsx, .xls
- PowerPoint: .pptx, .ppt
- PDF: .pdf（文本型）
- CSV: .csv

用法：
    # 单文件转换
    python3 doc_converter.py --file /path/to/document.docx

    # 批量转换目录下所有文档
    python3 doc_converter.py --input /path/to/docs/

输出：
    转换后的 Markdown 文件输出到 wiki/raw/converted/ 目录
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

env_path = Path("/app/deploy/conf/.env")
load_dotenv(env_path, override=True)

# 支持的格式
SUPPORTED_FORMATS = {
    '.docx': 'word',
    '.doc': 'word',
    '.xlsx': 'excel',
    '.xls': 'excel',
    '.pptx': 'powerpoint',
    '.ppt': 'powerpoint',
    '.pdf': 'pdf',
    '.csv': 'csv',
}

# 输出目录：从环境变量读取（必填，未设则 fail），生产为 NAS 挂载路径
WIKI_BASE_DIR = os.environ.get("WIKI_BASE_DIR")
if not WIKI_BASE_DIR:
    raise RuntimeError("WIKI_BASE_DIR 未配置（见 /app/deploy/conf/.env）")
WIKI_DIR = Path(WIKI_BASE_DIR)
OUTPUT_DIR = WIKI_DIR / "raw" / "converted"


def ensure_output_dir():
    """确保输出目录存在"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def convert_file(input_path, output_path=None):
    """转换单个文件为 Markdown"""
    try:
        from markitdown import MarkItDown
    except ImportError:
        return {
            "success": False,
            "error": "markitdown 库未安装，请运行: pip install markitdown"
        }

    input_path = Path(input_path)
    if not input_path.exists():
        return {"success": False, "error": f"文件不存在: {input_path}"}

    ext = input_path.suffix.lower()
    if ext not in SUPPORTED_FORMATS:
        return {
            "success": False,
            "error": f"不支持的格式: {ext}",
            "supported": list(SUPPORTED_FORMATS.keys())
        }

    # 确定输出路径：外部传入或默认
    if output_path is None:
        output_path = OUTPUT_DIR / f"{input_path.stem}.md"
    else:
        output_path = Path(output_path)

    try:
        md = MarkItDown()
        result = md.convert(str(input_path))

        # 添加元数据头
        metadata = f"""---
source_file: {input_path.name}
source_format: {SUPPORTED_FORMATS[ext]}
converted_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
---

"""
        content = metadata + result.text_content

        # 写入输出文件
        output_path.write_text(content, encoding='utf-8')

        return {
            "success": True,
            "input_file": str(input_path),
            "output_file": str(output_path),
            "file_type": SUPPORTED_FORMATS[ext],
            "file_size": input_path.stat().st_size,
            "output_size": output_path.stat().st_size,
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"转换失败: {str(e)}",
            "input_file": str(input_path)
        }


def get_output_filename(file_path, input_root):
    """生成输出文件名：来源目录前缀 + 文件名，避免同名冲突。
    例如：1-产品说明书_顺丰特快产品说明书.md
    """
    rel_dir = file_path.parent.relative_to(input_root)
    # 取第一级目录名作为前缀
    prefix = str(rel_dir).split(os.sep)[0] if str(rel_dir) != "." else ""
    stem = file_path.stem
    if prefix:
        return f"{prefix}_{stem}.md"
    return f"{stem}.md"


def build_word_priority_set(input_dir):
    """扫描目录，找出有 word 版本的同名文件（pdf 将被跳过）。"""
    word_stems = set()
    for file_path in input_dir.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in ('.docx', '.doc'):
            word_stems.add(file_path.stem)
    return word_stems


def convert_directory(input_dir):
    """批量转换目录下所有支持的文档（递归遍历，word 优先）"""
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        print(json.dumps({
            "success": False,
            "error": f"目录不存在: {input_dir}"
        }, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)

    ensure_output_dir()

    # 构建 word 优先集合：有 word 版本的同名 pdf 将被跳过
    word_stems = build_word_priority_set(input_dir)

    results = {"success": 0, "failed": 0, "skipped": 0, "files": []}

    for file_path in sorted(input_dir.rglob("*")):
        if not file_path.is_file():
            continue

        ext = file_path.suffix.lower()
        if ext not in SUPPORTED_FORMATS:
            continue

        # word 优先：如果同名有 word 版本，跳过 pdf
        if ext == '.pdf' and file_path.stem in word_stems:
            results["skipped"] += 1
            results["files"].append({
                "file": str(file_path.relative_to(input_dir)),
                "status": "skipped",
                "reason": "同名 word 版本优先"
            })
            continue

        # 生成输出文件名（带来源目录前缀）
        output_filename = get_output_filename(file_path, input_dir)
        md_path = OUTPUT_DIR / output_filename

        # 检查是否已转换
        if md_path.exists():
            results["skipped"] += 1
            results["files"].append({
                "file": str(file_path.relative_to(input_dir)),
                "status": "skipped",
                "reason": "已存在转换文件"
            })
            continue

        # 转换文件
        result = convert_file(file_path, md_path)
        if result["success"]:
            results["success"] += 1
            results["files"].append({
                "file": str(file_path.relative_to(input_dir)),
                "status": "success",
                "output": str(md_path)
            })
        else:
            results["failed"] += 1
            results["files"].append({
                "file": str(file_path.relative_to(input_dir)),
                "status": "failed",
                "error": result.get("error", "unknown")
            })

    return results


def main():
    parser = argparse.ArgumentParser(
        description="文档转换工具 - 将 Office/PDF/CSV 转换为 Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    # 转换单个文件
    python3 doc_converter.py --file /path/to/document.docx

    # 批量转换目录
    python3 doc_converter.py --input /path/to/docs/
        """
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", "-f", help="单个文件路径")
    group.add_argument("--input", "-i", help="输入目录（批量转换）")

    args = parser.parse_args()

    ensure_output_dir()

    if args.file:
        result = convert_file(args.file)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 1)

    elif args.input:
        results = convert_directory(args.input)
        summary = {
            "output_dir": str(OUTPUT_DIR),
            "success": results["success"],
            "failed": results["failed"],
            "skipped": results["skipped"],
            "files": results["files"]
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
