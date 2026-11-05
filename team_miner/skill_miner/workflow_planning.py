"""Global bounded method retrieval; semantics and provenance remain in the existing planning contract."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

WORKFLOW_PLANNING_PROTOCOL = "te-workflow-planning.v2"
COMPACT_PLANNING_PROTOCOL = "te-compact-planning.v1"
WORKFLOW_PLANNING_RULE = """Choose the smallest complete set of independently invokable workflow methods. Treat names,
ages, life stages, scenario IDs, contact origins and example numbers as parameters or branches, not workflows. For
coherent multi-document or role-play corpora, plan complete phases, not one candidate per scene, table row, customer,
turn or substep. Extra detail or examples do not justify a split. Use no target candidate count; never force mergers
to meet a quota. Every candidate needs a source-supported trigger, independently needed result and procedure reusable
without its neighboring phase. Perform a pairwise audit for same-journey pairs,
subset steps, duplicate results, handoffs and closings already covered by a phase. Merge when an existing complete
workflow covers both within its declared scope; keep separate only when the pair differs in purpose, terminal result,
independent invocation or mandatory applicability boundary. Do not create one candidate per source, heading or row.
A narrower trigger inside a phase and an output that fills one of the phase's fields are not independent goals.
Before retaining a nested method, identify a source-supported use outside the parent workflow and an independently
needed result not already covered by that parent. Without that evidence, keep it as a branch of the complete method.
Do not invent a parent, expand a narrow workflow, or merge only because topics or sources match. Preserve supported
procedures, conditions, exceptions, conflicts and precise source ranges; never invent facts or omit support. For
regulated role-play, mine the communication/training method, not eligibility or purchase decisions. Related metadata is
not Evidence."""


def canonical_selection_reuse(value, controls: dict):
    """Remove only exact redundant authority copies; the raw response and strict contract stay intact."""
    from .bounded_compile_plan import SELECTION_SCHEMA_V2
    from .candidate_identity import IDENTITY_FIELDS

    checked = copy.deepcopy(value)
    if not isinstance(checked, dict) or checked.get("schema_version") != SELECTION_SCHEMA_V2:
        return checked
    if not isinstance(checked.get("skills"), list):
        return checked
    for row in checked["skills"]:
        if not isinstance(row, dict) or not isinstance(row.get("reuse_candidate_id"), str):
            continue
        authorities = [known for known in controls.get("known_candidates", [])
                       if known.get("candidate_id") == row["reuse_candidate_id"]]
        extras = set(row) - {"reuse_candidate_id", "reason", "derived_from"}
        if (len(authorities) == 1 and extras <= set(IDENTITY_FIELDS)
                and all(key in authorities[0] and row[key] == authorities[0][key] for key in extras)):
            for key in extras:
                del row[key]
    return checked


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _features(task: str) -> list[str]:
    # Retrieval only: the untouched original task/boundary stays in its catalog.
    text = unicodedata.normalize("NFKC", task).casefold()
    text = re.sub(r"(?:item|unit|scope|file|src|source|task|scenario|case)-[a-z0-9_-]+", " ", text)
    text = re.sub(r"\d+(?:[.,]\d+)*", " ", text)
    words = set(re.findall(r"[a-z]{3,}", text))
    compact = "".join(c for c in text if c.isalpha())
    words.update(compact[i:i + 3] for i in range(max(0, len(compact) - 2)))
    return sorted(words, key=lambda word: hashlib.sha256(word.encode()).digest())[:128]


def prepare_workflow_order(root: Path, count: int) -> None:
    """Freeze an exact-once permutation; bounded posting probes never merge semantics."""
    if type(count) is not int or count < 0:
        raise ValueError("Workflow method count must be a nonnegative integer")
    folder = root / "workflow-order"
    marker = folder / "ready.json"
    if marker.exists():
        if _read(marker) != {"protocol": WORKFLOW_PLANNING_PROTOCOL, "count": count}:
            raise ValueError("Frozen workflow order belongs to a different method assignment")
        return
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "catalog.sqlite3")
    try:
        db.executescript(
            "CREATE TABLE IF NOT EXISTS methods (ordinal INTEGER PRIMARY KEY, identity TEXT UNIQUE, digest TEXT);"
            "CREATE TABLE IF NOT EXISTS postings (token TEXT, ordinal INTEGER, PRIMARY KEY(token,ordinal));"
            "CREATE INDEX IF NOT EXISTS posting_ordinal ON postings(ordinal);"
            "CREATE TABLE IF NOT EXISTS frequencies (token TEXT PRIMARY KEY, total INTEGER);"
            "CREATE TABLE IF NOT EXISTS schedule (position INTEGER PRIMARY KEY, ordinal INTEGER UNIQUE, digest TEXT);"
        )
        # A crash restarts ranking from the immutable complete catalog. Existing
        # schedule rows are compared, never silently replaced.
        with db:
            db.execute("DELETE FROM postings")
            db.execute("DELETE FROM frequencies")
            for index in range(count):
                path = root / "catalog" / "method" / f"{index:08d}.json"
                if path.is_symlink():
                    raise ValueError("Workflow method assignment cannot be a symlink")
                record = _read(path)
                item, digest = record["item"], _digest(record)
                expected = (index, item["item_id"], digest)
                old = db.execute("SELECT ordinal,identity,digest FROM methods WHERE ordinal=?", (index,)).fetchone()
                if old is not None and old != expected:
                    raise ValueError("Frozen workflow method or exact source lineage changed")
                db.execute("INSERT OR IGNORE INTO methods VALUES (?,?,?)", expected)
                for token in _features(item["task"]):
                    db.execute("INSERT INTO postings VALUES (?,?)", (token, index))
                    db.execute("INSERT INTO frequencies VALUES (?,1) ON CONFLICT(token) DO UPDATE SET total=total+1",
                               (token,))
        if db.execute("SELECT COUNT(*) FROM methods").fetchone()[0] != count:
            raise ValueError("Workflow method count differs from the immutable catalog")
        position = 0
        for index in range(count):
            if db.execute("SELECT 1 FROM schedule WHERE ordinal=? AND position<?", (index, position)).fetchone():
                continue
            # At most 16 features x 8 active postings form a fixed-size shortlist.
            # Assigned postings are deleted, avoiding an ever-growing scan of
            # old anchors or an all-pairs/common-token aggregation.
            tokens = db.execute(
                "SELECT p.token FROM postings p JOIN frequencies f ON f.token=p.token "
                "WHERE p.ordinal=? AND f.total>1 ORDER BY f.total,p.token LIMIT 16", (index,)
            ).fetchall()
            scores = {}
            for (token,) in tokens:
                for (other,) in db.execute(
                    "SELECT ordinal FROM postings WHERE token=? AND ordinal<>? ORDER BY ordinal LIMIT 8",
                    (token, index),
                ):
                    scores[other] = scores.get(other, 0) + 1
            rows = [index, *sorted(scores, key=lambda other: (-scores[other], other))[:7]]
            with db:
                for ordinal in rows:
                    if db.execute("SELECT 1 FROM schedule WHERE ordinal=? AND position<?",
                                  (ordinal, position)).fetchone():
                        continue
                    digest = db.execute("SELECT digest FROM methods WHERE ordinal=?", (ordinal,)).fetchone()[0]
                    expected = (position, ordinal, digest)
                    old = db.execute("SELECT position,ordinal,digest FROM schedule WHERE position=?",
                                     (position,)).fetchone()
                    if old is not None and old != expected:
                        raise ValueError("Interrupted workflow order differs from its immutable assignment")
                    db.execute("INSERT OR IGNORE INTO schedule VALUES (?,?,?)", expected)
                    db.execute("DELETE FROM postings WHERE ordinal=?", (ordinal,))
                    position += 1
        if position != count or db.execute("SELECT COUNT(*) FROM schedule").fetchone()[0] != count:
            raise ValueError("Workflow order must preserve every method exactly once")
    finally:
        db.close()
    _save(marker, {"protocol": WORKFLOW_PLANNING_PROTOCOL, "count": count})


def workflow_method_index(root: Path, cursor: int, count: int) -> int:
    """Resolve one frozen position, verifying the original metadata and lineage."""
    if type(cursor) is not int or not 0 <= cursor < count:
        raise ValueError("Workflow cursor is outside the assigned method ledger")
    folder = root / "workflow-order"
    if _read(folder / "ready.json") != {"protocol": WORKFLOW_PLANNING_PROTOCOL, "count": count}:
        raise ValueError("Frozen workflow order belongs to a different method assignment")
    db = sqlite3.connect(f"file:{folder / 'catalog.sqlite3'}?mode=ro", uri=True)
    try:
        value = db.execute("SELECT ordinal,digest FROM schedule WHERE position=?", (cursor,)).fetchone()
    finally:
        db.close()
    if value is None or not 0 <= value[0] < count:
        raise ValueError("Workflow order references an unassigned method")
    path = root / "catalog" / "method" / f"{value[0]:08d}.json"
    if path.is_symlink() or _digest(_read(path)) != value[1]:
        raise ValueError("Frozen workflow method or exact source lineage changed")
    return value[0]


def coalesce_exact_authority_reuse(
    value: object, controls: dict, *, _max_output_chars: int = 12000,
) -> tuple[dict, dict]:
    """Projection only; raw output/reasons remain immutable and strict validation still follows.

    Apply after frozen alias decoding and existing exact redundant-field normalization.
    Never merge new rows/refinements/names or overlapping method dispositions.
    """
    from . import bounded_compile_plan as planning

    planning._bounded_output(value, _max_output_chars=_max_output_chars)
    expected = {"schema_version", "input_ids", "skills", "knowledge", "deferred", "resolved"}
    if (
        set(value) != expected
        or value["schema_version"] not in {planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3}
        or value["schema_version"] != controls["selection_schema"]
        or not isinstance(value["skills"], list)
    ):
        raise ValueError("Reuse projection requires the exact authorized normal selection schema")
    planning._coverage(value["input_ids"], controls["input_ids"])
    items = controls["items"]
    if (not isinstance(items, list) or not 1 <= len(items) <= 24
            or any(not isinstance(item, dict) or item.get("kind") != "method" for item in items)):
        raise ValueError("Reuse projection requires one through 24 frozen assigned methods")
    if len({item["item_id"] for item in items}) != len(items):
        raise ValueError("Reuse projection cannot repair duplicate frozen method identities")
    allowed = controls["allowed_source_refs"]
    for item in items:
        planning._refs(item.get("source_refs"), allowed)
    planning._coverage(sorted({ref for item in items for ref in item["source_refs"]}), allowed)
    diagnostic = planning.planning_coverage_diagnostics(value, [row["item_id"] for row in items])
    if diagnostic:
        raise ValueError(planning._planning_coverage_error(diagnostic))
    authorities = controls["known_candidates"]
    known = {row["candidate_id"]: row for row in authorities}
    if len(known) != len(authorities):
        raise ValueError("Frozen reuse authority IDs must be unique")
    if len(value["skills"]) > len(items):
        raise ValueError("Reuse projection cannot exceed its finite assigned-method count")
    checked, first, merged, originals = copy.deepcopy(value), {}, [], {}
    checked["skills"] = []
    for index, raw_row in enumerate(value["skills"]):
        row = copy.deepcopy(raw_row)
        if not isinstance(row, dict) or "reuse_candidate_id" not in row:
            checked["skills"].append(row)
            continue
        if set(row) != {"reuse_candidate_id", "reason", "derived_from"}:
            raise ValueError("Reuse projection cannot rewrite or ignore canonical business fields")
        identity = row["reuse_candidate_id"]
        if not isinstance(identity, str) or identity not in known:
            raise ValueError("Reuse projection requires the same exact frozen known authority")
        planning._text(row["reason"], 1000, "reuse reason")
        planning._refs(row["derived_from"], [item["item_id"] for item in items])
        originals.setdefault(identity, []).append(
            {"row_index": index, "reason": row["reason"], "derived_from": copy.deepcopy(row["derived_from"])}
        )
        if identity not in first:
            first[identity] = row
            checked["skills"].append(row)
        else:
            # Exact-once preflight proved these lists disjoint. No deduplication hides an error.
            first[identity]["derived_from"].extend(row["derived_from"])
    for identity, rows in originals.items():
        if len(rows) > 1:
            merged.append(
                {"candidate_id": identity, "original_rows": rows, "reason_kept_in_projection": rows[0]["reason"]}
            )
    # Identity/source/business/refinement rules are not replaced by this deterministic projection.
    planning.validate_selection(
        checked,
        items=items,
        input_ids=controls["input_ids"],
        known_candidates=known,
        refinement_enabled=value["schema_version"] == planning.SELECTION_SCHEMA_V3,
        max_input_items=24,
        _max_output_chars=_max_output_chars,
    )
    audit = {
        "schema_version": "te-selection-reuse-projection.v1",
        "merged_rows": merged,
        "raw_canonical_sha256": hashlib.sha256(planning._json(value).encode()).hexdigest(),
        "projected_canonical_sha256": hashlib.sha256(planning._json(checked).encode()).hexdigest(),
        "original_reasons_retained_in_raw": True,
        "all_exact_dispositions_preserved": True,
    }
    return checked, audit
