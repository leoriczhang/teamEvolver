"""Deterministic Wiki links and backlinks without transport or domain imports."""
import posixpath
import re
from urllib.parse import unquote


def build_index(documents, root, task_id=""):
    pages = {uri: {"uri": uri, "links": [], "backlinks": []} for uri in sorted(documents)}
    for source, content in documents.items():
        content = re.sub(r"```.*?```|~~~.*?~~~|`[^`]*`", "", content, flags=re.S)
        for label, href in re.findall(r"(?<!!)\[([^\]]+)\]\(([^ )]+)(?:[^)]*)\)", content):
            href = unquote(href).split("#", 1)[0].split("?", 1)[0]
            if not href or re.match(r"^(https?|mailto|data|javascript):", href):
                continue
            if href.startswith("viking://"):
                target = href
            else:
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(source[len(root)+1:]), href))
                if relative.startswith("../") or relative == "..":
                    continue
                target = root + "/" + relative
            if target not in pages and target + ".md" in pages:
                target += ".md"
            if target in pages and target != source:
                edge = {"source_uri": source, "target_uri": target, "label": label}
                if edge not in pages[source]["links"]:
                    pages[source]["links"].append(edge)
                    pages[target]["backlinks"].append(edge)
    return {"pages": pages, "page_count": len(pages), "edge_count": sum(len(p["links"]) for p in pages.values()),
            "wiki_root": root, "compile_task_id": task_id}
