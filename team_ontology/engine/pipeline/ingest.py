"""ingest 阶段（确定性，无 LLM）：语料 → 分块 → 术语统计。"""

from __future__ import annotations

from pathlib import Path

from ..config import ProjectConfig, Thresholds
from ..corpus.chunker import chunk_docs
from ..corpus.loader import load_corpus, load_tool_catalog
from ..corpus.terms import extract_terms
from .context import ProjectContext


def run_ingest(
    ctx: ProjectContext,
    *,
    corpus_path: Path,
    config: ProjectConfig,
) -> dict:
    thresholds: Thresholds = config.thresholds
    docs = load_corpus(corpus_path)
    chunks = chunk_docs(
        [(doc.doc_id, doc.path.name, doc.text) for doc in docs],
        max_chars=thresholds.chunk_max_chars,
        overlap=thresholds.chunk_overlap_chars,
    )
    ctx.write_jsonl("chunks", (c.as_dict() for c in chunks))
    stats = extract_terms(
        [(doc.doc_id, doc.text) for doc in docs],
        languages=config.languages,
        min_frequency=thresholds.min_term_frequency,
        top_k=thresholds.term_candidates,
    )
    ctx.write_jsonl("terms", (s.as_dict() for s in stats))
    tools: dict[str, dict] = {}
    if config.tool_catalog_path:
        tools = load_tool_catalog(Path(config.tool_catalog_path))
    return {
        "docs": len(docs),
        "chunks": len(chunks),
        "terms": len(stats),
        "tools": len(tools),
    }
