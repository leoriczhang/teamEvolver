"""Deterministic export for tools and Worker; immutable model/host manifests."""

from pathlib import Path

from .input import PREPARER, canonical, render, sha
from .partition import batches, options

PAGE_SIZE = 128


def export(records, directory, limits=None):
    """Explicit export/cache only. Runtime business state belongs to PG/OV."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    limits = options(limits)
    pages, current, origins, cases, issues, seen = [], [], [], [], [], {}
    count = rows = duplicates = dialogue_chars = 0

    def write(relative, text):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        if path.exists() and path.read_bytes() != data:
            raise ValueError("existing_export_differs:" + relative)
        path.write_bytes(data)
        return {"path": relative, "sha256": sha(data), "bytes": len(data), "chars": len(text)}

    def flush():
        if not current and not origins and not issues:
            return
        number = len(pages) + 1
        page = {}
        for key, values in (("manifest", current), ("cases", cases), ("origins", origins), ("issues", issues)):
            page[key] = write(f"{key}/part-{number:06d}.jsonl",
                              "".join(canonical(value) + "\n" for value in values))
        pages.append(page)
        current.clear()
        origins.clear()
        cases.clear()
        issues.clear()

    for case, origin, problems in records:
        rows += 1
        if case is None:
            issues.append({"origin": origin, "reasons": problems, "status": "input_pending"})
        else:
            identity = (case["case_ref"], case["case_version"])
            origins.append({"case_ref": identity[0], "case_version": identity[1], "origin": origin})
            if identity in seen:
                duplicates += 1
            else:
                # Same shortened identity must never accidentally join different origins.
                prior = seen.get(case["case_ref"])
                logical = (origin.get("dialogue_id"), origin.get("instance_id")) if isinstance(origin, dict) else None
                if prior and logical and prior != logical:
                    raise ValueError("short_case_identity_collision")
                seen[identity] = True
                seen[case["case_ref"]] = logical
                model = render(case)
                entry = write(f"model_cases/{identity[0]}/{identity[1]}.md", model)
                current.append({**entry, "case_ref": identity[0], "case_version": identity[1],
                                "dialogue_chars": len(case["dialogue_text"])})
                cases.append(case)
                count += 1
                dialogue_chars += len(case["dialogue_text"])
            if problems:
                issues.append({"case_ref": identity[0], "origin": origin, "reasons": problems, "status": "warning"})
        if rows % PAGE_SIZE == 0:
            flush()
    flush()
    plan_page, plan_entries, unit_count, capacity = 0, [], 0, 0

    def items():
        import json
        for page in pages:
            with (directory / page["manifest"]["path"]).open(encoding="utf-8") as stream:
                for line in stream:
                    entry = json.loads(line)
                    yield {**entry, "content": (directory / entry["path"]).read_bytes().decode("utf-8")}

    plans = []
    for batch, pending in batches(items(), limits):
        if pending:
            capacity += len(pending)
        else:
            unit_count += 1
        plan_entries.append({"unit": unit_count if batch else None,
                             "status": "pending" if batch else "capacity_pending",
                             "files": [{k: v for k, v in item.items() if k != "content"} for item in batch or pending]})
        if len(plan_entries) == PAGE_SIZE:
            plan_page += 1
            plans.append(write(f"plans/part-{plan_page:06d}.jsonl",
                               "".join(canonical(v) + "\n" for v in plan_entries)))
            plan_entries.clear()
    if plan_entries:
        plans.append(write(f"plans/part-{plan_page + 1:06d}.jsonl",
                           "".join(canonical(v) + "\n" for v in plan_entries)))
    manifest = {"schema_version": "taikang.dialogue-dataset.v1", "preparer_version": PREPARER,
                "row_count": rows, "case_count": count, "duplicate_origin_count": duplicates,
                "dialogue_chars": dialogue_chars, "pages": pages, "plan_pages": plans,
                "extract_unit_count": unit_count, "capacity_pending_count": capacity, "options": limits}
    manifest["dataset_sha256"] = sha(canonical(manifest))
    write("dataset_manifest.json", canonical(manifest) + "\n")
    return manifest
