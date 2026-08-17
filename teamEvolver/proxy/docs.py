"""Docs browser API for serving markdown documentation through the console.

Provides endpoints to list the documentation tree, fetch markdown content,
and perform full-text search across docs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class DocsMixin:
    """Documentation browsing and search endpoints."""

    def _docs_root(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / "docs"

    def _should_skip(self, rel_path: str) -> bool:
        parts = rel_path.replace("\\", "/").split("/")
        skip_dirs = {
            "node_modules", ".vitepress", "__pycache__", ".git",
            "public", "scripts",
        }
        skip_prefixes = (".",)
        for p in parts:
            if p in skip_dirs:
                return True
            if any(p.startswith(prefix) for prefix in skip_prefixes):
                return True
        return False

    def _title_from_markdown(self, content: str, fallback: str) -> str:
        for line in content.splitlines():
            m = re.match(r"^#\s+(.+)$", line.strip())
            if m:
                return m.group(1).strip()
        return fallback

    def _strip_md(self, name: str) -> str:
        if name.endswith(".md"):
            return name[:-3]
        return name

    def _humanize(self, name: str) -> str:
        name = self._strip_md(name)
        name = re.sub(r"^\d+[-_]", "", name)
        name = name.replace("-", " ").replace("_", " ")
        return name.strip() or name

    def _section_label(self, dirname: str, lang: str = "zh") -> str:
        labels_zh = {
            "getting-started": "开始使用",
            "concepts": "核心概念",
            "guides": "使用指南",
            "agent-integrations": "Agent 接入",
            "api": "API 参考",
            "faq": "常见问题",
            "about": "关于",
            "design": "设计文档",
            "agents": "贡献者指南",
            "schemas": "Schema",
        }
        labels_en = {
            "getting-started": "Getting Started",
            "concepts": "Concepts",
            "guides": "Guides",
            "agent-integrations": "Agent Integrations",
            "api": "API Reference",
            "faq": "FAQ",
            "about": "About",
            "design": "Design Notes",
            "agents": "Contributor Guide",
            "schemas": "Schemas",
        }
        labels = labels_zh if lang == "zh" else labels_en
        return labels.get(dirname, self._humanize(dirname))

    def _build_docs_tree(self, lang: str = "zh") -> list[dict]:
        docs_root = self._docs_root()
        result: list[dict] = []

        section_order = [
            "getting-started", "concepts", "guides",
            "agent-integrations", "api", "faq", "about",
        ]

        lang_dir = docs_root / lang
        if lang_dir.is_dir():
            present_sections = {
                d.name for d in lang_dir.iterdir()
                if d.is_dir() and not self._should_skip(d.name)
            }
            for section_name in section_order:
                if section_name not in present_sections:
                    continue
                section_dir = lang_dir / section_name
                section_label = self._section_label(section_name, lang)
                pages: list[dict] = []
                for f in sorted(section_dir.glob("*.md")):
                    if self._should_skip(f.name):
                        continue
                    try:
                        content = f.read_text(encoding="utf-8", errors="replace")
                        title = self._title_from_markdown(content, self._humanize(f.name))
                    except Exception:
                        title = self._humanize(f.name)
                    pages.append({
                        "id": f"{lang}/{section_name}/{f.name}",
                        "path": f"{lang}/{section_name}/{f.name}",
                        "title": title,
                        "filename": f.name,
                    })
                if pages:
                    result.append({
                        "id": f"{lang}/{section_name}/",
                        "label": section_label,
                        "section": section_name,
                        "pages": pages,
                    })

        design_dir = docs_root / "design"
        if design_dir.is_dir():
            design_pages = []
            for f in sorted(design_dir.glob("*.md")):
                if self._should_skip(f.name):
                    continue
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    title = self._title_from_markdown(content, self._humanize(f.name))
                except Exception:
                    title = self._humanize(f.name)
                design_pages.append({
                    "id": f"design/{f.name}",
                    "path": f"design/{f.name}",
                    "title": title,
                    "filename": f.name,
                })
            if design_pages:
                result.append({
                    "id": "design/",
                    "label": self._section_label("design", lang),
                    "section": "design",
                    "pages": design_pages,
                })

        return result

    def _read_doc(self, doc_path: str) -> Optional[dict]:
        docs_root = self._docs_root()
        safe_path = doc_path.replace("\\", "/").lstrip("/")
        if ".." in safe_path:
            return None
        abs_path = docs_root / safe_path
        try:
            abs_path = abs_path.resolve()
            docs_resolved = docs_root.resolve()
            if not str(abs_path).startswith(str(docs_resolved)):
                return None
        except Exception:
            return None
        if not abs_path.is_file() or abs_path.suffix != ".md":
            return None
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("[Docs] failed to read %s: %s", abs_path, e)
            return None
        rel = str(abs_path.relative_to(docs_root)).replace("\\", "/")
        title = self._title_from_markdown(content, self._humanize(abs_path.name))
        return {
            "path": rel,
            "title": title,
            "content": content,
        }

    def _search_docs(self, query: str, lang: str = "zh", limit: int = 20) -> list[dict]:
        docs_root = self._docs_root()
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        terms = [t for t in re.split(r"\s+", query_lower) if t]
        results: list[dict] = []

        search_dirs: list[Path] = []
        lang_dir = docs_root / lang
        if lang_dir.is_dir():
            search_dirs.append(lang_dir)
        design_dir = docs_root / "design"
        if design_dir.is_dir():
            search_dirs.append(design_dir)

        for base_dir in search_dirs:
            for md_file in base_dir.rglob("*.md"):
                if self._should_skip(str(md_file.relative_to(docs_root))):
                    continue
                try:
                    content = md_file.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                title = self._title_from_markdown(content, self._humanize(md_file.name))
                content_lower = content.lower()
                title_lower = title.lower()

                score = 0
                for term in terms:
                    title_hits = title_lower.count(term) * 10
                    content_hits = content_lower.count(term)
                    if content_hits == 0 and title_hits == 0:
                        score = -1
                        break
                    score += title_hits + min(content_hits, 50)
                if score < 0:
                    continue

                idx = content_lower.find(terms[0]) if terms else -1
                snippet = ""
                if idx >= 0:
                    start = max(0, idx - 60)
                    end = min(len(content), idx + 140)
                    snippet = content[start:end].replace("\n", " ").strip()
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(content):
                        snippet = snippet + "..."

                rel = str(md_file.relative_to(docs_root)).replace("\\", "/")
                results.append({
                    "path": rel,
                    "title": title,
                    "snippet": snippet,
                    "score": score,
                })

        results.sort(key=lambda x: -x["score"])
        return results[:limit]

    def _register_docs_routes(self, app: FastAPI) -> None:

        @app.get("/api/docs/tree")
        async def api_docs_tree(
            lang: str = Query("zh", description="Language: zh or en"),
        ):
            self._mark_request_activity()
            lang = lang if lang in ("zh", "en") else "zh"
            return JSONResponse({"lang": lang, "sections": self._build_docs_tree(lang)})

        @app.get("/api/docs/page")
        async def api_docs_page(
            path: str = Query(..., description="Doc path relative to docs/"),
        ):
            self._mark_request_activity()
            doc = self._read_doc(path)
            if doc is None:
                raise HTTPException(status_code=404, detail="Document not found")
            return JSONResponse(doc)

        @app.get("/api/docs/search")
        async def api_docs_search(
            q: str = Query(..., min_length=1, description="Search query"),
            lang: str = Query("zh", description="Language: zh or en"),
            limit: int = Query(20, ge=1, le=50),
        ):
            self._mark_request_activity()
            lang = lang if lang in ("zh", "en") else "zh"
            results = self._search_docs(q, lang=lang, limit=limit)
            return JSONResponse({"query": q, "lang": lang, "count": len(results), "results": results})
