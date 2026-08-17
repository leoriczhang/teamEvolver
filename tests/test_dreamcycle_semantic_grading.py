from __future__ import annotations

from teamEvolver.dreamcycle.semantic import SemanticMatcher


def _fake_embedder(vectors: dict[str, list[float]]):
    def embed(texts):
        return [
            vectors.get(text.strip(), [0.0, 0.0, 0.0])
            for text in texts
        ]

    return embed


def test_disabled_matcher_returns_unknown_not_lexical() -> None:
    matcher = SemanticMatcher(embed_fn=None)

    result = matcher.assess(
        "请假审批流程",
        {"viking://u/a.md": "请假审批流程"},
    )

    assert matcher.enabled is False
    assert result["verdict"] == "unknown"


def test_semantic_duplicate_detected_across_different_wording() -> None:
    matcher = SemanticMatcher(
        embed_fn=_fake_embedder(
            {
                "上线步骤": [1.0, 0.0, 0.0],
                "部署流程": [0.98, 0.02, 0.0],
            }
        ),
        merge_threshold=0.86,
        warn_threshold=0.72,
    )

    result = matcher.assess(
        "上线步骤",
        {"viking://u/deploy.md": "部署流程"},
    )

    assert result["verdict"] == "merge"
    assert str(result["best_uri"]).endswith("deploy.md")


def test_unrelated_meaning_is_distinct() -> None:
    matcher = SemanticMatcher(
        embed_fn=_fake_embedder(
            {
                "季度预算模型": [0.0, 1.0, 0.0],
                "请假审批流程": [1.0, 0.0, 0.0],
            }
        )
    )

    result = matcher.assess(
        "季度预算模型",
        {"viking://u/leave.md": "请假审批流程"},
    )

    assert result["verdict"] == "distinct"


def test_related_but_not_duplicate_warns() -> None:
    matcher = SemanticMatcher(
        embed_fn=_fake_embedder(
            {
                "canary 发布注意事项": [1.0, 0.6, 0.0],
                "部署流程": [1.0, 0.0, 0.0],
            }
        ),
        merge_threshold=0.95,
        warn_threshold=0.7,
    )

    result = matcher.assess(
        "canary 发布注意事项",
        {"viking://u/deploy.md": "部署流程"},
    )

    assert result["verdict"] == "warn"


def test_no_existing_docs_is_distinct() -> None:
    matcher = SemanticMatcher(embed_fn=_fake_embedder({}))

    assert matcher.assess("anything", {})["verdict"] == "distinct"
