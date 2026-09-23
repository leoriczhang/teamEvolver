"""术语统计：jieba TF-IDF（zh）+ 词频短语（en）→ 候选概念术语表。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-_]{1,63}")
_MEANINGFUL_RE = re.compile(r"[\u3400-\u9fffA-Za-z]")


@dataclass
class TermStat:
    term: str
    weight: float
    doc_count: int

    def as_dict(self) -> dict:
        return {"term": self.term, "weight": round(self.weight, 4), "doc_count": self.doc_count}


def _zh_terms(text: str, top_k: int) -> dict[str, float]:
    import jieba.analyse

    terms: dict[str, float] = {}
    for word, weight in jieba.analyse.extract_tags(text, topK=top_k, withWeight=True):
        word = word.strip()
        if len(word) >= 2:
            terms[word] = terms.get(word, 0.0) + weight
    return terms


def _en_terms(text: str, top_k: int) -> dict[str, float]:
    from collections import Counter
    from itertools import pairwise

    words = _WORD_RE.findall(text.lower())
    counter = Counter(words)
    total = sum(counter.values()) or 1
    weights = {word: count / total for word, count in counter.items()}
    # 双词搭配（共现频率最高的作为复合术语候选）
    bigrams = Counter(pairwise(words))
    for (a, b), count in bigrams.most_common(top_k // 2):
        term = f"{a} {b}"
        weights[term] = count / total
    return weights


def _phrase_terms_zh(text: str, top_k: int) -> dict[str, float]:
    """基于词性的名词短语（连续 n* 序列），作为复合术语补充。"""
    import jieba.posseg as pseg

    phrases: dict[str, float] = {}
    buffer: list[str] = []
    for word, flag in pseg.cut(text):
        if flag.startswith("n") or flag == "eng":
            buffer.append(word)
        else:
            if len(buffer) >= 2:
                phrase = "".join(buffer)
                if len(phrase) <= 24:
                    phrases[phrase] = phrases.get(phrase, 0.0) + 1.0
            buffer = []
    if len(buffer) >= 2:
        phrase = "".join(buffer)
        if len(phrase) <= 24:
            phrases[phrase] = phrases.get(phrase, 0.0) + 1.0
    return dict(sorted(phrases.items(), key=lambda kv: -kv[1])[:top_k])


def add_user_terms(terms: Iterable[str], weight: int = 1000) -> None:
    """将既有术语（seed/概念名/别名）注入 jieba 用户词典，改善切词。"""
    import jieba

    for term in terms:
        term = term.strip()
        if term:
            jieba.add_word(term, freq=weight)


def extract_terms(
    texts: list[tuple[str, str]],
    *,
    languages: list[str] | None = None,
    min_frequency: int = 2,
    top_k: int = 200,
    user_terms: Iterable[str] = (),
    with_phrases: bool = True,
) -> list[TermStat]:
    """texts: [(doc_id, text)] → 术语统计（按聚合权重降序）。"""
    languages = languages or ["zh"]

    if user_terms:
        add_user_terms(user_terms)

    per_doc: list[dict[str, float]] = []
    for doc_id, text in texts:
        weights: dict[str, float] = {}
        if any(lang == "zh" for lang in languages):
            weights.update(_zh_terms(text, top_k))
            if with_phrases:
                weights.update(_phrase_terms_zh(text, top_k))
        if any(lang == "en" for lang in languages):
            weights.update(_en_terms(text, top_k))
        per_doc.append(weights)

    aggregated: dict[str, dict] = {}
    for weights in per_doc:
        for term, weight in weights.items():
            entry = aggregated.setdefault(term, {"weight": 0.0, "docs": 0})
            entry["weight"] += weight
            entry["docs"] += 1

    stats = [
        TermStat(term=term, weight=entry["weight"], doc_count=entry["docs"])
        for term, entry in aggregated.items()
        if entry["docs"] >= min_frequency and len(term) >= 2 and _MEANINGFUL_RE.search(term)
    ]
    stats.sort(key=lambda s: (-s.weight, -s.doc_count, s.term))
    return stats[:top_k]


def find_chunks(term: str, chunks: list[dict], limit: int = 3) -> list[dict]:
    """在 chunk 列表中查找包含 term 的 chunk（用于证据）。chunks 元素需含 chunk_id/doc/text。"""
    key = re.sub(r"\s+", "", term).lower()
    hits: list[dict] = []
    for chunk in chunks:
        if key and key in re.sub(r"\s+", "", chunk["text"]).lower():
            hits.append(chunk)
            if len(hits) >= limit:
                break
    return hits
