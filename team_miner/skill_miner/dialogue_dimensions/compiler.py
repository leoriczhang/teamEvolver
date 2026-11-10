"""Editable initial dialogue compiler, containing its entire contract."""

from pathlib import Path

ROOT = Path(__file__).parent.parent / "compiler_skills" / "dialogue-dimensions-v1"
SKILL_BODY = (ROOT / "SKILL.md").read_text(encoding="utf-8")


def instruction(stage, quote_mode):
    if stage not in {"extract", "merge"}:
        raise ValueError("invalid dialogue stage")
    return (
        f"execution_protocol=te-dialogue-dimensions.v1; stage={stage}; quote_mode={quote_mode}。\n"
        "使用当前保存的编译 Skill 中对应 stage 的步骤与交付契约。"
    )
