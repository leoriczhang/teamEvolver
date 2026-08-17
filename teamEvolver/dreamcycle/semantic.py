"""Semantic similarity for duplication detection.

DreamCycle judges duplication by *meaning*, not surface tokens. This module
wraps an embedding backend behind a small, injectable interface so the tools
stay testable offline and so a missing embedding endpoint degrades gracefully
(never silently falling back to lexical token overlap).
"""

from __future__ import annotations

import hashlib
import logging
import math
from typing import Callable, Dict, List, Optional

from ..observability import (
    langfuse_observation,
    update_langfuse_observation,
)

logger = logging.getLogger(__name__)

# Embedding function contract: given texts, return one vector per text.
EmbedFn = Callable[[List[str]], List[List[float]]]


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


class SemanticMatcher:
    """Grade a candidate memory against existing docs by embedding similarity.

    Verdicts:
      - ``merge``:    cosine >= merge_threshold  -> a real semantic duplicate.
      - ``warn``:     cosine >= warn_threshold   -> related; allow but surface it.
      - ``distinct``: below warn_threshold       -> genuinely new.
      - ``unknown``:  no embedding backend / call failed -> caller must not block
                      on lexical fallback; defer the judgement to the LLM.
    """

    def __init__(
        self,
        embed_fn: Optional[EmbedFn] = None,
        *,
        merge_threshold: float = 0.86,
        warn_threshold: float = 0.72,
        max_chars: int = 4000,
    ) -> None:
        self._embed_fn = embed_fn
        self._merge = merge_threshold
        self._warn = warn_threshold
        self._max_chars = max_chars
        self._cache: Dict[str, List[float]] = {}

    @property
    def enabled(self) -> bool:
        return self._embed_fn is not None

    def _embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self._embed_fn:
            return None
        prepared = [t[: self._max_chars] for t in texts]
        missing = [t for t in prepared if _key(t) not in self._cache]
        if missing:
            try:
                vectors = self._embed_fn(missing)
            except Exception as exc:  # noqa: BLE001 - backend errors degrade to unknown
                logger.warning("[Semantic] embedding call failed: %s", exc)
                return None
            if len(vectors) != len(missing):
                logger.warning("[Semantic] embedding count mismatch; ignoring result")
                return None
            for text, vec in zip(missing, vectors):
                self._cache[_key(text)] = vec
        return [self._cache[_key(t)] for t in prepared]

    def assess(self, text: str, existing: Dict[str, str]) -> Dict[str, object]:
        """Return ``{verdict, best_uri, score, enabled}`` for a candidate.

        ``existing`` maps URI -> comparable text (title + body excerpt).
        """
        result: Dict[str, object] = {
            "verdict": "unknown",
            "best_uri": "",
            "score": 0.0,
            "enabled": self.enabled,
        }
        text = (text or "").strip()
        if not self.enabled:
            return result  # no backend -> defer to the LLM, never lexical fallback
        if not text:
            return result
        if not existing:
            result["verdict"] = "distinct"  # nothing to collide with
            return result

        uris = list(existing.keys())
        vectors = self._embed([text] + [existing[u] for u in uris])
        if vectors is None:
            return result  # call failed -> unknown

        cand_vec, existing_vecs = vectors[0], vectors[1:]
        best_uri, best_score = "", 0.0
        for uri, vec in zip(uris, existing_vecs):
            score = _cosine(cand_vec, vec)
            if score > best_score:
                best_uri, best_score = uri, score

        if best_score >= self._merge:
            verdict = "merge"
        elif best_score >= self._warn:
            verdict = "warn"
        else:
            verdict = "distinct"
        result.update(verdict=verdict, best_uri=best_uri, score=round(best_score, 4))
        return result


def make_openai_embedder(
    *, base_url: str, api_key: str, model: str
) -> Optional[EmbedFn]:
    """Build an embedding function backed by an OpenAI-compatible endpoint.

    Returns ``None`` when the model or key is unset, so callers can construct a
    disabled :class:`SemanticMatcher` without special-casing configuration.
    """
    if not model or not api_key:
        return None

    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url.rstrip("/"))

    def embed(texts: List[str]) -> List[List[float]]:
        with langfuse_observation(
            name="teamEvolver.dreamcycle.embedding",
            as_type="embedding",
            input=texts,
            metadata={
                "component": "teamEvolver.dreamcycle",
                "operation": "semantic_dedup",
                "input_count": len(texts),
            },
            model=model,
            trace_name="teamEvolver.dreamcycle.semantic_dedup",
            tags=["dreamcycle", "embedding", "semantic-dedup"],
        ) as observation:
            resp = client.embeddings.create(model=model, input=texts)
            vectors = [item.embedding for item in resp.data]
            usage = getattr(resp, "usage", None)
            total_tokens = getattr(usage, "total_tokens", None)
            update_langfuse_observation(
                observation,
                output={
                    "vector_count": len(vectors),
                    "dimensions": len(vectors[0]) if vectors else 0,
                },
                usage_details=(
                    {"total_tokens": int(total_tokens)}
                    if isinstance(total_tokens, (int, float))
                    else None
                ),
            )
            return vectors

    return embed
