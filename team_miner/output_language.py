"""Choose a stable artifact language from frozen original Evidence only."""

from __future__ import annotations

import json
import re
from pathlib import Path

MAX_LANGUAGE_FILES = 128
MAX_LANGUAGE_FILE_CHARS = 16_384
MAX_LANGUAGE_TOTAL_CHARS = 262_144

OUTPUT_LANGUAGE_GUIDANCE = (
    "Follow the TE output_language_policy for all generated human-readable text. "
    "This policy takes priority over the language of compiler instructions, previous candidates "
    "and human review context; none of these determines the source language. "
    "For zh-CN write Simplified Chinese; for en write English; for source follow the primary "
    "natural language of the frozen original source Evidence, not technical tokens. "
    "Apply this to every generated title and Markdown heading, section.title, Skill description and body, "
    "shared analysis and EVALUATION, plan task, trigger, "
    "inputs, outputs, boundary and rationale, deferred.reason, review question and reason, "
    "and Test Dataset human-readable name, description, query and check.text. "
    "Keep machine keys, schema versions, enum values, Skill name slugs, dataset_id, case_id, qid, "
    "paths, source URIs, code, numbers and units unchanged. Preserve direct source quotations, "
    "Evidence excerpts and original material text faithfully; generate surrounding explanations "
    "in the target language. Translation must not invent facts, change conditions or calculations."
)

_ENGLISH_MARKERS = frozenset(
    "the and with from this that must should before after when only then within without shall "
    "is are not will verify check policy return returns shipping parcel refund receipt user "
    "request approved source evidence accept weight fee deadline apply".split()
)


def _natural_text(text: str) -> str:
    """Exclude code and machine identifiers, retaining Markdown link labels."""
    lines = []
    fence: str | None = None
    fence_size = 0
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence, fence_size = value[0], len(value)
            elif value[0] == fence and len(value) >= fence_size:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    text = "\n".join(lines)
    text = re.sub(r"`+[^`\n]*(?:`+|$)", " ", text, flags=re.MULTILINE)
    text = re.sub(r"(!?\[[^\]\n]*\])\([^\)\n]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s*\[[^\]\n]+\]:\s*\S+.*$", " ", text)
    text = re.sub(r"\b(?:[a-z][a-z0-9+.-]*://|mailto:)[^\s<>]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[\"'][A-Za-z_][A-Za-z0-9_.-]*[\"']\s*:", " ", text)
    text = re.sub(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_.-]*\s*:", " ", text)
    # Capitals alone can be ordinary English prose. Remove tokens only when
    # digits or explicit identifier separators distinguish them from words.
    text = re.sub(
        r"\b(?=[A-Za-z0-9_-]*[0-9_-])[A-Za-z0-9][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*\b", " ", text
    )
    text = re.sub(r"\b[A-Za-z][A-Za-z0-9]*(?:[_./-][A-Za-z0-9]+)+\b", " ", text)
    return re.sub(r"\b[a-f0-9]{16,}\b", " ", text, flags=re.IGNORECASE)


def _han(character: str) -> bool:
    return "\u3400" <= character <= "\u9fff" or "\U00020000" <= character <= "\U0003134f"


def validate_artifact_headings(
    content: str, policy: dict, *, title: str = "", machine_name: str = "",
) -> None:
    """Reject untranslated generated headings; fenced examples and source blockquotes stay intact."""
    if policy.get("language") != "zh-CN":
        return
    headings = [title] if title else []
    fence, fence_size = None, 0
    previous = ""
    for line in content.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence, fence_size = value[0], len(value)
            elif value[0] == fence and len(value) >= fence_size:
                fence = None
            previous = ""
            continue
        if fence is not None or line.lstrip().startswith(">"):
            previous = ""
            continue
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+)", line)
        if heading:
            headings.append(heading.group(1))
        elif previous and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line):
            headings.append(previous)
        previous = line.strip()
    if any(text.strip() and not any(_han(char) for char in text) for text in headings):
        raise ValueError("生成的标题和小节标题必须使用中文；内部标识保留在正文，原文引述使用引用块或代码块。")
    if machine_name and any(machine_name in text for text in headings):
        raise ValueError("展示标题应说明中文方法名称，不能以内部英文标识作为标题；标识可保留在正文。")


def resolve_output_language_policy(entries: list[dict]) -> dict:
    """Use a bounded sample of cached original files; ambiguity follows source language."""
    remaining = MAX_LANGUAGE_TOTAL_CHARS
    samples = []
    original = sorted(
        (row for row in entries if row.get("kind") == "source"),
        key=lambda row: str(row.get("relative_path") or ""),
    )
    for row in original[:MAX_LANGUAGE_FILES]:
        if remaining <= 0:
            break
        try:
            with Path(row["local_path"]).open(encoding="utf-8") as stream:
                text = stream.read(min(remaining, MAX_LANGUAGE_FILE_CHARS))
        except (OSError, UnicodeError, KeyError) as exc:
            raise ValueError("无法读取已冻结的原始来源文本") from exc
        remaining -= len(text)
        samples.append(_natural_text(text))
    text = "\n".join(samples)
    han_count = sum(_han(character) for character in text)
    words = re.findall(r"[A-Za-z]+", text.lower())
    latin_count = sum(map(len, words))
    # Japanese and Korean sources also use Han characters. Keep their language
    # undecided rather than silently converting them to Chinese.
    has_kana_or_hangul = bool(re.search(r"[\u3040-\u30ff\uac00-\ud7af]", text))
    other_letters = sum(character.isalpha() and not _han(character) and not character.isascii() for character in text)
    language = "source"
    if not has_kana_or_hangul and other_letters < max(3, han_count // 10):
        if han_count >= 4 and han_count * 2 >= latin_count:
            language = "zh-CN"
        else:
            english = [word for word in words if word in _ENGLISH_MARKERS]
            if (
                han_count * 2 < latin_count
                and len(english) >= 4
                and len(set(english)) >= 3
                and len(english) >= len(words) * 0.15
            ):
                language = "en"
    return {"version": 1, "basis": "frozen_original_source", "language": language}


def output_language_instruction(policy: dict) -> str:
    """Put the system's language decision before untrusted revision context."""
    if (
        not isinstance(policy, dict)
        or type(policy.get("version")) is not int
        or policy.get("version") != 1
        or policy.get("basis") != "frozen_original_source"
        or not isinstance(policy.get("language"), str)
        or policy.get("language") not in {"zh-CN", "en", "source"}
    ):
        raise ValueError("产物语言策略无效")
    target = {
        "zh-CN": "本任务所有轮次新生成的面向人文字必须使用简体中文，英文上一轮产物或编译指令不改变此要求。",
        "en": "All rounds must generate human-readable artifact text in English.",
        "source": "All rounds must follow the primary natural language of the frozen original source Evidence.",
    }[policy["language"]]
    return (
        "\nTE output_language_policy: "
        + json.dumps(policy, ensure_ascii=False, sort_keys=True)
        + "\n"
        + target
        + "\n"
        + OUTPUT_LANGUAGE_GUIDANCE
        + "\n"
    )
