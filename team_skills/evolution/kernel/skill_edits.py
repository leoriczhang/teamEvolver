"""Search/replace edit application for skill evolution.

The evolve stage asks the LLM for an ordered list of exact-match edits
instead of rewriting the whole skill body. This:

- keeps unchanged sections byte-identical — code fences, JSON escapes, and
  punctuation styles survive untouched (full-rewrite outputs historically
  stripped ```` ```json ```` fences and `` \\" `` escapes from examples);
- lets very long skills evolve within a small output budget (no need to
  re-emit thousands of lines just to change one section); and
- makes hallucinated anchors mechanically detectable: an edit whose
  ``old_string`` does not match exactly once simply fails to apply.

Supported operations (each references the content as it stands after all
preceding edits have been applied):

- ``{"operation": "replace", "old_string": ..., "new_string": ...}``
  ``old_string`` must appear exactly once; ``new_string`` may be empty
  (deletion).
- ``{"operation": "insert_after", "anchor": ..., "new_string": ...}``
  ``anchor`` must appear exactly once; ``new_string`` is inserted
  immediately after the anchor occurrence (leading newlines belong in
  ``new_string``).

Application is all-or-nothing per round: if any edit fails, nothing is
applied and the failures are reported so the caller can feed them back to
the LLM for one corrective round.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

REPLACE = "replace"
INSERT_AFTER = "insert_after"
OPERATIONS = (REPLACE, INSERT_AFTER)

_NOT_FOUND_HINT = (
    'old_string/anchor 未在当前正文中找到。必须从当前正文逐字节复制——'
    '包括反斜杠转义（如 JSON 示例中的 \\"）、缩进、空行与标点风格；'
    '凭记忆重写的锚点会匹配失败'
)


@dataclass
class EditFailure:
    """One edit that could not be applied (with a human-readable reason)."""

    index: int
    operation: str
    reason: str
    snippet: str = ""

    def describe(self) -> str:
        out = f"#{self.index + 1} ({self.operation or 'unknown'}): {self.reason}"
        if self.snippet:
            out += f"（片段：{self.snippet}）"
        return out


def _snippet_of(text: str, limit: int = 80) -> str:
    """Compact one-line preview of an anchor for LLM feedback."""
    compact = " ".join(str(text or "").split())
    return compact[: limit - 1] + "…" if len(compact) > limit else compact


@dataclass
class EditApplication:
    """Result of applying one round of edits."""

    ok: bool
    # New content on success; the untouched original on failure.
    content: str
    applied: int = 0
    failures: list[EditFailure] = field(default_factory=list)


def format_edit_failures(failures: list[EditFailure]) -> str:
    """Render failures as one multi-line string for logs / LLM feedback."""
    return "\n".join(f"- {failure.describe()}" for failure in failures)


def _match_once(work: str, needle: str) -> tuple[int, int]:
    """Return (occurrences, position of the first match)."""
    count = work.count(needle)
    if count == 0:
        return 0, -1
    return count, work.find(needle)


def apply_skill_edits(content: str, edits: Any) -> EditApplication:
    """Apply an ordered edit list to *content*; all-or-nothing per round."""
    original = str(content or "")
    if not isinstance(edits, list) or not edits:
        return EditApplication(
            ok=False,
            content=original,
            failures=[EditFailure(0, "", "edits 必须是非空的编辑列表")],
        )

    work = original
    failures: list[EditFailure] = []
    applied = 0

    for index, edit in enumerate(edits):
        if not isinstance(edit, Mapping):
            failures.append(EditFailure(index, "", "编辑必须是对象"))
            continue
        operation = str(edit.get("operation") or "").strip()
        if operation not in OPERATIONS:
            failures.append(
                EditFailure(
                    index,
                    operation,
                    f"不支持的 operation（必须是 {'/'.join(OPERATIONS)} 之一）",
                )
            )
            continue

        if operation == REPLACE:
            old = edit.get("old_string")
            new = edit.get("new_string", "")
            if not isinstance(old, str) or not old:
                failures.append(EditFailure(index, operation, "old_string 必须是非空字符串"))
                continue
            if not isinstance(new, str):
                # A non-string new_string silently coerced to "" would delete
                # content the model intended to change — reject instead.
                failures.append(EditFailure(index, operation, "new_string 必须是字符串（空字符串表示删除）"))
                continue
            count, pos = _match_once(work, old)
            if count == 0:
                failures.append(
                    EditFailure(index, operation, _NOT_FOUND_HINT, _snippet_of(old))
                )
                continue
            if count > 1:
                failures.append(
                    EditFailure(
                        index,
                        operation,
                        f"old_string 匹配了 {count} 次；请给出更长、更唯一的片段（可含上下文行）",
                        _snippet_of(old),
                    )
                )
                continue
            work = work[:pos] + new + work[pos + len(old) :]
            applied += 1
            continue

        # insert_after
        anchor = edit.get("anchor")
        new = edit.get("new_string")
        if not isinstance(anchor, str) or not anchor:
            failures.append(EditFailure(index, operation, "anchor 必须是非空字符串"))
            continue
        if not isinstance(new, str) or not new:
            failures.append(EditFailure(index, operation, "new_string 必须是非空字符串"))
            continue
        count, pos = _match_once(work, anchor)
        if count == 0:
            failures.append(
                EditFailure(index, operation, _NOT_FOUND_HINT, _snippet_of(anchor))
            )
            continue
        if count > 1:
            failures.append(
                EditFailure(
                    index,
                    operation,
                    f"anchor 匹配了 {count} 次；请给出更长、更唯一的锚点",
                    _snippet_of(anchor),
                )
            )
            continue
        insert_at = pos + len(anchor)
        work = work[:insert_at] + new + work[insert_at:]
        applied += 1

    if failures:
        # All-or-nothing: a partially applied batch could interleave with
        # dependent edits and produce incoherent content.
        return EditApplication(ok=False, content=original, applied=0, failures=failures)
    return EditApplication(ok=True, content=work, applied=applied)
