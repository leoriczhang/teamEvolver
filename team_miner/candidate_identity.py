"""Local candidate identity retrieval and strictly nonfactual grouping contracts."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

GROUP_SCHEMA = "skill-miner.identity-groups.v1"
IDENTITY_GROUP_SCHEMA_V2 = "skill-miner.identity-groups.v2"
IDENTITY_GROUP_PROTOCOL_V2 = "te-candidate-identity-groups.v2"
IDENTITY_PROTOCOL = "te-candidate-identity.v1"
IDENTITY_PARTITION_PROTOCOL = "te-candidate-identity-partition.v2"
CONSOLIDATING_PARTITION_PROTOCOL = "te-candidate-identity-partition.v3"
LEGACY_IDENTITY_PARTITION_PROTOCOL = "te-candidate-identity-partition.v1"
SUPPORTED_IDENTITY_PARTITION_PROTOCOLS = {
    IDENTITY_PARTITION_PROTOCOL, LEGACY_IDENTITY_PARTITION_PROTOCOL, CONSOLIDATING_PARTITION_PROTOCOL,
}
MAX_PARTITION_IDENTITIES = 32
IDENTITY_FIELDS = ("name", "task", "trigger", "inputs", "outputs", "boundary", "rationale")
MAX_RETRIEVAL_PASSES = 2

LEGACY_PARTITION_RULE = '''Compare the complete assigned Candidate identity collection in ONE bounded partition.
Return exactly {"schema_version":"skill-miner.identity-groups.v2","candidate_ids":[all assigned IDs],
"groups":[{"candidate_ids":[known IDs],"survivor_candidate_id":"one exact ID in this group",
"reason":"nonempty concise reason, at most 160 characters"}]}.
There are at most 32 identities. Every assigned identity appears exactly once across the groups;
include singleton groups. Emit the complete JSON via the output tool, without prose, analysis or repeated
pair-by-pair explanations. The entire JSON must fit 12000 characters. Never invent, rewrite or rename
Candidate metadata. Choose an EXISTING complete workflow as the survivor, including when its parent
identity occurs later. Preserve the smallest authority_priority whenever a prior authority is present.
Different prior authorities remain separate. Candidate metadata is identity context, NOT factual Evidence.
Merge variations when they implement the same independently executable purpose and terminal result;
channels, personas, examples and conditional branches can be parameters. Also merge a narrower Candidate
when it is only a substep of a supplied broader phase workflow and has no distinct trigger/output requiring
independent invocation. In that case nominate the existing broader workflow as survivor if its canonical
fields already cover every member. Examples include a single introduction, technique, data field or handoff
step already contained in a complete role-play phase. Preserve necessary inputs, terminal outputs, mandatory
applicability boundaries and all source-backed branches. Keep independent lifecycle phases, user goals and
normative boundaries separate. A shared source, word, audience, number or supporting substep alone does not
establish that relationship. Do not hide a distinct purpose under a narrower survivor or invent a broad parent
that is absent from the supplied identities. When the metadata does not establish coverage and equivalence,
retain singleton groups and record the exact distinction. A local batch is not the whole corpus. Do not claim
unavailable Evidence or missing business rules from this metadata. The host preserves canonical business fields,
full original scopes, previous Case IDs and merge audit.
'''

PARTITION_RULE = '''Compare the complete assigned Candidate identity collection in ONE bounded partition.
Return exactly {"schema_version":"skill-miner.identity-groups.v2","candidate_ids":[all assigned IDs],
"groups":[{"candidate_ids":[known IDs],"survivor_candidate_id":"one exact ID in this group",
"reason":"nonempty concise reason, at most 160 characters"}]}.
There are at most 32 identities. Every assigned identity appears exactly once; include singleton groups. Emit the
complete JSON without prose, analysis or pair-by-pair explanations; the entire JSON must fit 12000 characters.
Never invent or rewrite Candidate metadata. Choose an EXISTING complete workflow as survivor. An authority_priority
starting with 0 denotes a reviewed prior identity: preserve it and keep different prior authorities separate. A value
starting with 1 is a fresh proposal, not an authority; its ordinal must not favor an earlier narrower workflow over
a later complete workflow. Candidate metadata is identity context, not factual
Evidence. Compare EVERY assigned identity with EVERY other assigned identity before deciding groups. Separately
planned rows are not presumed independent. Check same-journey pairs, subset containment, duplicate terminal results,
and introductions, collections, referrals, handoffs or closing actions already included in a broader phase. Merge
variations with the same purpose and terminal result; channels, personas, examples and branches are parameters.
Merge a narrower Candidate into a supplied broader phase when it has no distinct source-supported trigger,
independently needed terminal result AND procedure reusable without the parent. Select the most complete existing
workflow as survivor when its canonical fields cover every member; preserve all source-backed ranges. A singleton is
allowed only after the pairwise audit establishes a concrete distinction in trigger, standalone terminal result,
independent reuse or mandatory applicability boundary. Its reason must name the closest overlapping Candidate and
state that distinction; a generic claim such as "独立工作流" is insufficient. Keep truly different lifecycle phases,
goals and normative boundaries separate. Shared source, topic, audience or tool alone is insufficient. Do not invent
a standalone use from a narrower trigger: a condition arising inside a parent phase, or an output filling one of its
fields, is a branch unless the supplied metadata establishes independent use outside that phase and a needed result
not already covered by it. Different levels of detail do not establish different terminal results. Do not invent
a parent, expand a narrow survivor, or hide a distinct purpose. If coverage is clear, merge despite different headings
or wording; if still unclear after comparison, name the exact uncertainty in the singleton reason. Preserve all
original scopes and previous Case IDs. A local batch is not the whole corpus; do not claim unavailable Evidence.
'''


def render_identity_partition_compiler(base_body: str, *, protocol: str = IDENTITY_PARTITION_PROTOCOL) -> str:
    """Render a frozen identity partition policy without changing existing rounds."""
    from .compile_strategy import render_single_stage_compiler
    if protocol == LEGACY_IDENTITY_PARTITION_PROTOCOL:
        rule = LEGACY_PARTITION_RULE
    elif protocol == IDENTITY_PARTITION_PROTOCOL:
        rule = PARTITION_RULE
    elif protocol == CONSOLIDATING_PARTITION_PROTOCOL:
        rule = PARTITION_RULE.replace(
            "An authority_priority\nstarting with 0 denotes a reviewed prior identity: "
            "preserve it and keep different prior authorities separate.",
            "Prior identity IDs are immutable lineage, not proof of independent workflows. "
            "Prior identities may consolidate when the chosen existing parent covers their trigger, terminal "
            "result and mandatory boundaries; all member contracts and source scopes remain archived."
        )
        rule += "\nConsolidation policy te-candidate-consolidation.v1. Preserve every accepted refinement as " \
                "a source-supported parent branch. Different wording is not a conflict. Keep genuine " \
                "incompatible normative boundaries separate; never merge only to reach a count."
    else:
        raise ValueError("Unknown candidate identity partition protocol")
    return render_single_stage_compiler(base_body, "identity_group", rule)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def candidate_id(job_id: str, name: str) -> str:
    return "candidate-" + hashlib.sha256(_json([job_id, name]).encode()).hexdigest()[:24]


def identity_version(row: dict) -> str:
    """Invalidate a review when canonical business metadata or its exact scope changes."""
    return hashlib.sha256(_json({
        "candidate": row["candidate"], "scope": row.get("scope", {}), "prior": bool(row.get("prior")),
        **({"consolidated_members": row["consolidated_members"]} if row.get("consolidated_members") else {}),
    }).encode()).hexdigest()


def validate_identity_groups(value, *, allowed_ids: list[str], allow_survivor: bool = False) -> list[dict]:
    schema = IDENTITY_GROUP_SCHEMA_V2 if allow_survivor else GROUP_SCHEMA
    if (
        not isinstance(value, dict) or len(_json(value)) > 12000
        or set(value) != {"schema_version", "candidate_ids", "groups"}
        or value.get("schema_version") != schema
    ):
        raise ValueError("Candidate grouping requires only the assigned identity partition and reasons")
    header = value["candidate_ids"]
    if (
        not isinstance(header, list) or any(not isinstance(item, str) for item in header)
        or len(header) != len(set(header)) or set(header) != set(allowed_ids)
    ):
        raise ValueError("Candidate grouping must preserve every exact assigned identity")
    groups, consumed = value["groups"], []
    if not isinstance(groups, list):
        raise ValueError("Candidate identity groups must be an array")
    row_keys = {"candidate_ids", "reason"}
    if allow_survivor:
        row_keys.add("survivor_candidate_id")
    for row in groups:
        if not isinstance(row, dict) or set(row) != row_keys:
            raise ValueError("Candidate grouping cannot rewrite business fields or invent identities")
        ids, reason = row["candidate_ids"], row["reason"]
        if (
            not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids)
            or len(ids) != len(set(ids)) or not set(ids) <= set(allowed_ids)
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000
        ):
            raise ValueError("Candidate group needs known unique identities and a bounded nonempty reason")
        if allow_survivor and (
            not isinstance(row["survivor_candidate_id"], str) or row["survivor_candidate_id"] not in ids
        ):
            raise ValueError("The nominated survivor must be an exact existing identity in its own group")
        consumed.extend(ids)
    if len(consumed) != len(set(consumed)) or set(consumed) != set(allowed_ids):
        raise ValueError("Candidate groups must form a complete disjoint identity partition")
    return groups


def identity_brief(row: dict, *, include_authority: bool = False) -> dict:
    refs = row["candidate"].get("source_refs", [])
    result = {
        "candidate_id": row["candidate_id"],
        **{key: row["candidate"][key] for key in IDENTITY_FIELDS if key != "rationale"},
        "source_file_ids": ["file-" + hashlib.sha256(uri.encode()).hexdigest()[:24] for uri in refs[:8]],
        "source_scope_count": len(refs),
        "source_scope_sha256": hashlib.sha256(_json(sorted(refs)).encode()).hexdigest(),
        "source_file_ids_are_representative": len(refs) > 8,
        "candidate_metadata_is_evidence": False,
    }
    if include_authority:
        result["authority_priority"] = [0 if row["prior"] else 1, row["ordinal"]]
    return result


def _tokens(candidate: dict) -> list[str]:
    text = unicodedata.normalize("NFKC", " ".join(
        str(item) for key in IDENTITY_FIELDS if key != "rationale"
        for item in (candidate[key] if isinstance(candidate[key], list) else [candidate[key]])
    )).casefold()
    words = set(re.findall(r"[a-z][a-z0-9_-]{1,}", text))
    compact = "".join(character for character in text if character.isalnum())
    words.update(compact[index:index + 3] for index in range(max(0, len(compact) - 2)))
    # This bounds a retrieval feature vector, never source text or candidate
    # metadata. Omitted semantic pairs are explicitly reported in the audit.
    return sorted(words, key=lambda value: hashlib.sha256(value.encode()).digest())[:256]


class IdentityCatalog:
    """Disk-backed global retrieval; each model window remains independent of collection size."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS candidates (identity TEXT PRIMARY KEY, name TEXT UNIQUE, "
            "ordinal INTEGER, prior INTEGER, active INTEGER, payload TEXT);"
            "CREATE TABLE IF NOT EXISTS tokens (token TEXT, identity TEXT, PRIMARY KEY(token, identity));"
            "CREATE INDEX IF NOT EXISTS token_identity ON tokens(identity);"
            "CREATE TABLE IF NOT EXISTS sources (source TEXT, identity TEXT, PRIMARY KEY(source, identity));"
            "CREATE INDEX IF NOT EXISTS source_identity ON sources(identity);"
            "CREATE TABLE IF NOT EXISTS comparisons (left_id TEXT, right_id TEXT, "
            "PRIMARY KEY(left_id, right_id));"
            "CREATE TABLE IF NOT EXISTS versioned_comparisons (left_id TEXT, right_id TEXT, "
            "left_version TEXT, right_version TEXT, "
            "PRIMARY KEY(left_id, right_id, left_version, right_version));"
            "CREATE TABLE IF NOT EXISTS partition_origins (identity TEXT, batch INTEGER, "
            "PRIMARY KEY(identity,batch));"
        )

    def close(self):
        self.db.close()

    def put(self, row: dict):
        identity, candidate = row["candidate_id"], row["candidate"]
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?,?)", (
                identity, candidate["name"], row["ordinal"], int(row["prior"]), 1, _json(row),
            ))
            self.db.execute("DELETE FROM tokens WHERE identity=?", (identity,))
            self.db.execute("DELETE FROM sources WHERE identity=?", (identity,))
            self.db.executemany("INSERT INTO tokens VALUES (?,?)", ((token, identity) for token in _tokens(candidate)))
            self.db.executemany("INSERT INTO sources VALUES (?,?)", (
                (source, identity) for source in dict.fromkeys(candidate.get("source_refs", []))
            ))
            if "identity_partition_origins" in row:
                self.db.execute("DELETE FROM partition_origins WHERE identity=?", (identity,))
                self.db.executemany("INSERT INTO partition_origins VALUES (?,?)", (
                    (identity, batch) for batch in row["identity_partition_origins"]
                ))

    def get(self, identity: str) -> dict:
        result = self.db.execute("SELECT payload FROM candidates WHERE identity=?", (identity,)).fetchone()
        if result is None:
            raise ValueError("Unknown local candidate identity")
        return json.loads(result[0])

    def named(self, name: str) -> dict | None:
        result = self.db.execute("SELECT payload FROM candidates WHERE name=? AND active=1", (name,)).fetchone()
        return json.loads(result[0]) if result else None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM candidates WHERE active=1").fetchone()[0]

    def partition_rows(self, *, cross_batch: bool = False):
        """Stream nonoverlapping representatives; source URI overlap is deliberately irrelevant."""
        if not cross_batch:
            query = "SELECT payload FROM candidates WHERE active=1 ORDER BY ordinal"
        else:
            # Group by a rare shared workflow feature without constructing all
            # candidate pairs. Only representatives related across initial
            # batches enter the optional final layer. This is retrieval, not a
            # semantic assertion that these identities should merge.
            query = (
                "WITH shared AS (SELECT t.token,COUNT(DISTINCT t.identity) AS frequency "
                "FROM tokens t JOIN partition_origins o ON o.identity=t.identity "
                "JOIN candidates c ON c.identity=t.identity WHERE c.active=1 "
                "GROUP BY t.token HAVING COUNT(DISTINCT o.batch)>1), "
                "ranked AS (SELECT t.identity,MIN(printf('%09d:%s',s.frequency,s.token)) AS ordering "
                "FROM tokens t JOIN shared s ON t.token=s.token GROUP BY t.identity) "
                "SELECT c.payload FROM candidates c JOIN ranked r ON r.identity=c.identity "
                "WHERE c.active=1 ORDER BY r.ordering,c.prior DESC,c.ordinal"
            )
        for (payload,) in self.db.execute(query):
            yield json.loads(payload)

    def collection_version(self) -> str:
        digest = hashlib.sha256()
        for identity, payload in self.db.execute(
            "SELECT identity,payload FROM candidates WHERE active=1 ORDER BY identity"
        ):
            digest.update(_json([identity, identity_version(json.loads(payload))]).encode())
        return digest.hexdigest()

    def at(self, cursor: int) -> dict | None:
        result = self.db.execute(
            "SELECT payload FROM candidates WHERE ordinal>=? ORDER BY ordinal LIMIT 1", (cursor,)
        ).fetchone()
        return json.loads(result[0]) if result else None

    def deactivate(self, identity: str):
        with self.db:
            self.db.execute("UPDATE candidates SET active=0 WHERE identity=?", (identity,))

    def is_active(self, identity: str) -> bool:
        row = self.db.execute("SELECT active FROM candidates WHERE identity=?", (identity,)).fetchone()
        return bool(row and row[0])

    def related(self, identity: str, *, limit: int = 7, include_reviewed: bool = False) -> list[dict]:
        # Rank from the complete catalog, including early candidates and
        # candidates from other files. Source overlap is only a retrieval signal.
        comparison_filter = "" if include_reviewed else (
            " AND NOT EXISTS (SELECT 1 FROM comparisons p WHERE"
            " p.left_id=MIN(?,c.identity) AND p.right_id=MAX(?,c.identity))"
        )
        rows = self.db.execute(
            "WITH matches AS ("
            " SELECT b.identity, 1.0 AS weight FROM tokens a JOIN tokens b ON a.token=b.token"
            " WHERE a.identity=? AND b.identity<>?"
            " UNION ALL SELECT b.identity, 8.0 AS weight FROM sources a JOIN sources b ON a.source=b.source"
            " WHERE a.identity=? AND b.identity<>?)"
            " SELECT c.payload, SUM(m.weight) AS score FROM matches m JOIN candidates c ON c.identity=m.identity"
            " WHERE c.active=1" + comparison_filter +
            " GROUP BY c.identity ORDER BY score DESC, c.prior DESC, c.ordinal LIMIT ?",
            (identity, identity, identity, identity, *(() if include_reviewed else (identity, identity)), limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def reviewed(self, left: dict, right: dict) -> bool:
        ordered = sorted((left, right), key=lambda row: row["candidate_id"])
        return self.db.execute(
            "SELECT 1 FROM versioned_comparisons WHERE left_id=? AND right_id=? "
            "AND left_version=? AND right_version=?",
            (ordered[0]["candidate_id"], ordered[1]["candidate_id"],
             identity_version(ordered[0]), identity_version(ordered[1])),
        ).fetchone() is not None

    def compared(self, identities: list[str], *, versions: dict[str, str] | None = None):
        versions = versions or {identity: identity_version(self.get(identity)) for identity in identities}
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO comparisons VALUES (?,?)", (
                tuple(sorted((left, right)))
                for index, left in enumerate(identities) for right in identities[index + 1:]
            ))
            self.db.executemany("INSERT OR IGNORE INTO versioned_comparisons VALUES (?,?,?,?)", (
                (*pair, versions[pair[0]], versions[pair[1]])
                for index, left in enumerate(identities) for right in identities[index + 1:]
                for pair in [tuple(sorted((left, right)))]
            ))

    def survivor(self, identities: list[str]) -> dict:
        return min(
            (self.get(identity) for identity in identities), key=lambda row: (not row["prior"], row["ordinal"])
        )
