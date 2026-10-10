"""Deterministic export and offline validation. Never ask a model to rewrite approved conditions."""

import hashlib
import html
import json
import re
from pathlib import Path

from .contracts import ProcedurePack, digest, verify_evidence


def plain(value):
    # Source text is never interpreted as a Markdown link, image, or HTML tag.
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(value))


def render(pack: ProcedurePack):
    pages = {}
    backlinks = {m.method_id: set() for m in pack.methods}
    for method in pack.methods:
        for key in method.related_methods:
            backlinks[key].add(method.method_id)
    titles = {m.method_id: m.title for m in pack.methods}
    pages["index.md"] = (
        "# 方法 Wiki\n\n" + "\n".join(f"- [{plain(m.title)}](methods/{m.method_id}.md)" for m in pack.methods) + "\n"
    )
    for method in pack.methods:
        lines = ["# " + plain(method.title), "", plain(method.purpose), ""]
        for title, values in [
            ("适用条件", method.conditions),
            ("禁止与否定", method.prohibitions),
            ("例外", method.exceptions),
            ("步骤", method.steps),
            ("完成标准", method.completion),
        ]:
            lines += ["## " + title, ""] + ["- " + plain(v) for v in values] + [""]
        lines += ["## 来源与证据", ""]
        for evidence in method.evidence:
            # Link to the immutable source page, not an unfiltered mutable/private original URI.
            key = digest({"source_id": evidence.source_id, "revision": evidence.revision})
            lines += [
                f"- [来源 {evidence.source_id}](../sources/{key}.md) · 字符 {evidence.start}—{evidence.end}",
                "",
                "> " + plain(evidence.quote).replace("\n", "\n> "),
                "",
            ]
        lines += ["## 能力引用（编制信息）", ""]
        lines += [f"- {plain(b.skill_id)} @ {plain(b.version)}：{plain(b.description)}" for b in method.capabilities]
        related = set(method.related_methods) | backlinks[method.method_id]
        lines += ["", "## 相关方法与反链", ""]
        lines += [f"- [{plain(titles[key])}]({key}.md)" for key in sorted(related)]
        pages[f"methods/{method.method_id}.md"] = "\n".join(lines) + "\n"
    refs = {}
    for method in pack.methods:
        for ref in method.evidence:
            key = digest({"source_id": ref.source_id, "revision": ref.revision})
            refs.setdefault(key, []).append((method, ref))
    for key, values in refs.items():
        ref = values[0][1]
        lines = [
            f"# 来源 {plain(ref.source_id)}",
            "",
            "Revision: " + ref.revision,
            "",
            "正文摘要: " + ref.digest,
            "",
            "## 批准的引文",
            "",
        ]
        lines += list(dict.fromkeys("> " + plain(r.quote).replace("\n", "\n> ") for _, r in values))
        lines += ["", "## 被引用方法", ""]
        lines += [
            f"- [{plain(m.title)}](../methods/{m.method_id}.md)" for m in {m.method_id: m for m, _ in values}.values()
        ]
        pages[f"sources/{key}.md"] = "\n".join(lines) + "\n"
    pages["procedure-pack.json"] = json.dumps(pack.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    return pages


def page_hashes(pages):
    return {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sorted(pages.items())}


def validate_directory(directory, sources=None):
    folder = Path(directory).resolve()
    pack = ProcedurePack.model_validate_json((folder / "procedure-pack.json").read_text())
    if sources is not None:
        verify_evidence(pack, sources)
    expected = render(pack)
    failures = []
    for path, text in expected.items():
        actual = folder / path
        if actual.is_symlink() or not actual.is_file() or actual.read_text() != text:
            failures.append(path)
    for file in folder.rglob("*"):
        if file.is_symlink() or (
            file.is_file() and str(file.relative_to(folder)) not in expected and file.name != "validation.json"
        ):
            failures.append(str(file.relative_to(folder)))
    return {
        "contract": "kee.wiki-validation.v1",
        "valid": not failures,
        "failures": sorted(set(failures)),
        "pack_digest": digest(pack.model_dump(mode="json")),
        "pages": page_hashes(expected),
        "management": "NOT_MANAGED",
        "evidence_checked": sources is not None,
    }
