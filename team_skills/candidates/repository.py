"""Skill candidates repository.

Candidates live in ``skill_candidates`` / ``skill_candidate_files`` /
``skill_candidate_events`` (``003_skills.sql``). Every change is an explicit,
audited transition — never a whole-document overwrite:

- ``status`` follows a closed state machine (:data:`TRANSITIONS`)::

   open ──► validating ──► open    (validation finished, verdict stored)
    │      │
    │      ├──► published    (accepted publish)
    │      └──► rejected
    ├──► published          (force / replay-based publish)
    └──► rejected           (manual reject)

 ``published`` and ``rejected`` are terminal.
- ``verdict`` is the single central validation result: ``accept`` / ``reject``
 / ``inconclusive`` (no quorum, no vote aggregation).
- Human marks: ``reviewed`` is manual; publish auto-sets ``adopted``; reject
 auto-sets ``rejected``.
- Editing content bumps ``revision`` and clears the stale verdict/evaluation;
 a transition guarded by ``expected_revision`` refuses to act on a revision the
 caller did not see.

Each transition takes the candidate row lock, writes the new state and appends
one ``skill_candidate_events`` row in the caller's transaction. Only this
domain's tables are touched.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from team_skills.config import BundleLimits
from team_skills.evolution.repository import lock_key
from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import (
    SkillLibraryError,
    SkillNotFound,
    assemble_bundle,
    bundle_metadata,
    put_blobs,
    validate_bundle,
    validate_skill_name,
)
from teamEvolver.database.transaction import TransactionManager

STATUSES = ("open", "validating", "published", "rejected", "archived")
TERMINAL = frozenset({"published", "rejected", "archived"})
VERDICTS = ("accept", "reject", "inconclusive")
MARKS = ("reviewed", "adopted", "rejected")

TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"validating", "published", "rejected", "archived"}),
    "validating": frozenset({"open", "published", "rejected", "archived"}),
    "published": frozenset(),
    "rejected": frozenset(),
    "archived": frozenset(),
}


class CandidateStateError(SkillLibraryError):
    """An illegal transition or a stale revision (maps to HTTP 409)."""

    status_code = 409


def new_candidate_id(skill_name: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{stamp}-{skill_name.lower()}-{uuid.uuid4().hex[:8]}"


def _loads(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


class CandidateRepository:
    """Persistence for candidates and their audited state transitions."""

    def __init__(self, tx: TransactionManager) -> None:
        self._tx = tx

    @property
    def _schema(self) -> str:
        return self._tx.pool.schema

    _COLUMNS = (
        "candidate_id, skill_name, proposed_action, status, verdict, revision, base_version, "
        "tree_sha256, reviewed, adopted, rejected, published_version, mutation_id, "
        "validate_task_id, detail, evaluation, decision, created_at, updated_at, decided_at"
    )
    # Exclude evidence and bundles in PostgreSQL, before asyncpg transfers the
    # JSON. Removing them from the HTTP payload alone still loads megabytes per
    # candidate on every queue refresh.
    _COMPACT_COLUMNS = _COLUMNS.replace(
        "detail,",
        "(detail - ARRAY['session_evidence', 'replay_cases', 'evolution_context', "
        "'bundle_diff', 'skill_diff', 'file_changes', 'candidate_skill', 'current_skill', "
        "'candidate_skill_md', 'current_skill_md']::text[]) AS detail,",
    )

    @staticmethod
    def _row(row: Any) -> dict[str, Any]:
        item = dict(row)
        for key in ("detail", "evaluation", "decision"):
            item[key] = _loads(item.get(key))
        return item

    # -- reads ------------------------------------------------------------ #

    async def get(self, conn: Any, candidate_id: str, *, for_update: bool = False) -> dict[str, Any] | None:
        lock = " FOR UPDATE" if for_update else ""
        row = await conn.fetchrow(
            f"SELECT {self._COLUMNS} FROM {self._schema}.skill_candidates WHERE candidate_id = $1{lock}",
            candidate_id,
        )
        return self._row(row) if row is not None else None

    async def require(self, conn: Any, candidate_id: str, *, for_update: bool = False) -> dict[str, Any]:
        item = await self.get(conn, candidate_id, for_update=for_update)
        if item is None:
            raise SkillNotFound(f"candidate not found: {candidate_id}")
        return item

    async def list(
        self, conn: Any, *, scope: str = "open", limit: int = 20, offset: int = 0, compact: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """Bounded page of candidates; ``scope`` is ``open`` / ``processed`` / ``all``."""
        statuses = {
            "open": ["open", "validating"],
            "processed": ["published", "rejected", "archived"],
            "all": list(STATUSES),
        }.get(scope)
        if statuses is None:
            raise SkillLibraryError(f"invalid candidate scope: {scope!r}")
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        total = await conn.fetchval(
            f"SELECT count(*) FROM {self._schema}.skill_candidates WHERE status = ANY($1::text[])",
            statuses,
        )
        columns = self._COMPACT_COLUMNS if compact else self._COLUMNS
        rows = await conn.fetch(
            f"SELECT {columns} FROM {self._schema}.skill_candidates "
            "WHERE status = ANY($1::text[]) ORDER BY updated_at DESC, candidate_id "
            "LIMIT $2 OFFSET $3",
            statuses,
            limit,
            offset,
        )
        return [self._row(row) for row in rows], int(total or 0)

    async def live_for_skill(self, conn: Any, skill_name: str) -> dict[str, Any] | None:
        row = await conn.fetchrow(
            f"SELECT {self._COLUMNS} FROM {self._schema}.skill_candidates "
            "WHERE skill_name = $1 AND status IN ('open', 'validating') FOR UPDATE",
            skill_name,
        )
        return self._row(row) if row is not None else None

    async def read_bundle(self, conn: Any, candidate_id: str) -> dict[str, bytes]:
        rows = await conn.fetch(
            f"SELECT f.path, f.sha256, b.content FROM {self._schema}.skill_candidate_files f "
            f"JOIN {self._schema}.skill_blobs b ON b.tenant_id = f.tenant_id AND b.sha256 = f.sha256 "
            "WHERE f.candidate_id = $1 ORDER BY f.path",
            candidate_id,
        )
        bundle = assemble_bundle(rows)
        item = await self.require(conn, candidate_id)
        if bundle_tree_sha256(bundle) != item["tree_sha256"]:
            raise RuntimeError("candidate bundle tree hash mismatch")
        return bundle

    async def events(self, conn: Any, candidate_id: str) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            "SELECT seq, kind, from_status, to_status, revision, actor, detail, created_at "
            f"FROM {self._schema}.skill_candidate_events WHERE candidate_id = $1 ORDER BY seq",
            candidate_id,
        )
        out = []
        for row in rows:
            item = dict(row)
            item["detail"] = _loads(item.get("detail"))
            out.append(item)
        return out

    # -- write helpers ----------------------------------------------------- #

    async def _event(
        self,
        conn: Any,
        *,
        tenant_id: str,
        candidate_id: str,
        kind: str,
        from_status: str = "",
        to_status: str = "",
        revision: int,
        actor: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        await conn.execute(
            f"INSERT INTO {self._schema}.skill_candidate_events "
            "(tenant_id, candidate_id, seq, kind, from_status, to_status, revision, actor, detail) "
            "SELECT $1, $2, COALESCE(MAX(seq), 0) + 1, $3, $4, $5, $6, $7, $8::jsonb "
            f"FROM {self._schema}.skill_candidate_events WHERE candidate_id = $2",
            tenant_id,
            candidate_id,
            kind,
            from_status,
            to_status,
            int(revision),
            actor,
            json.dumps(detail or {}),
        )

    async def _write_files(
        self, conn: Any, *, tenant_id: str, candidate_id: str, bundle: Mapping[str, bytes]
    ) -> None:
        records = await put_blobs(conn, self._schema, tenant_id, bundle)
        await conn.execute(
            f"DELETE FROM {self._schema}.skill_candidate_files WHERE candidate_id = $1", candidate_id
        )
        await conn.executemany(
            f"INSERT INTO {self._schema}.skill_candidate_files "
            "(tenant_id, candidate_id, path, sha256, size) VALUES ($1, $2, $3, $4, $5)",
            [(tenant_id, candidate_id, r["path"], r["sha256"], int(r["size"])) for r in records],
        )

    @staticmethod
    def _check_revision(item: dict[str, Any], expected_revision: int | None) -> None:
        if expected_revision is not None and int(expected_revision) != int(item["revision"]):
            raise CandidateStateError("candidate revision changed; reload before acting")

    # -- mutations --------------------------------------------------------- #

    async def lock_merge_inputs(self, conn, *, tenant_id, inputs):
        """Lock and compare every frozen input before writing anything in the caller's transaction."""
        if not inputs or len({item["candidate_id"] for item in inputs}) != len(inputs):
            raise CandidateStateError("merge inputs must be distinct")
        for name in sorted({item["skill_name"] for item in inputs}):
            await lock_key(conn, tenant_id, f"candidate-skill:{name}")
        for expected in sorted(inputs, key=lambda item: item["candidate_id"]):
            actual = await self.require(conn, expected["candidate_id"], for_update=True)
            if actual["status"] != "open" or any(
                actual[key] != expected[key] for key in ("skill_name", "revision", "base_version", "tree_sha256")
            ):
                raise CandidateStateError("merge input changed while planning; reload Candidates")

    async def finish_merge(self, conn, *, tenant_id, target, inputs):
        """Retain source Bundles and evaluations; archive only after target revision succeeds."""
        if target["candidate_id"] != inputs[0]["candidate_id"] or target["revision"] != inputs[0]["revision"] + 1:
            raise CandidateStateError("merge target revision does not match frozen input")
        for source in inputs[1:]:
            await self.transition(
                conn, tenant_id=tenant_id, candidate_id=source["candidate_id"], to_status="archived",
                actor="evolution", expected_revision=source["revision"],
                reason=f"Merged into {target['candidate_id']} r{target['revision']}",
                decision={"status": "merged", "merged_into": target["candidate_id"],
                          "target_revision": target["revision"], "source": source},
            )
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=target["candidate_id"],
            kind="feedback", from_status="open", to_status="open", revision=target["revision"],
            actor="evolution", detail={"consolidation": {"inputs": inputs}},
        )

    async def archive_for_skill(self, conn, *, tenant_id, skill_name, actor, reason):
        """Retire the live package; retain files, evaluations and event history."""
        await lock_key(conn, tenant_id, f"candidate-skill:{skill_name}")
        live = await self.live_for_skill(conn, skill_name)
        if live is None:
            return []
        await self.transition(
            conn, tenant_id=tenant_id, candidate_id=live["candidate_id"],
            to_status="archived", actor=actor, reason=reason,
            decision={"status": "archived", "reason": reason},
        )
        return [live["candidate_id"]]

    async def create(
        self,
        conn: Any,
        *,
        tenant_id: str,
        skill_name: str,
        bundle: Mapping[str, bytes | bytearray | str],
        limits: BundleLimits,
        proposed_action: str = "update",
        base_version: int = 0,
        detail: dict[str, Any] | None = None,
        candidate_id: str = "",
        actor: str = "evolution",
    ) -> dict[str, Any]:
        """Insert a new ``open`` candidate, or revise the live one for the skill.

        At most one live candidate per skill exists (partial unique index); a
        later evolution cycle revises it in place, bumping ``revision`` and
        clearing the stale verdict.
        """
        skill_name = validate_skill_name(skill_name)
        if proposed_action not in {"create", "update"}:
            raise SkillLibraryError(f"invalid proposed_action: {proposed_action!r}")
        clean = validate_bundle(skill_name, bundle, limits)
        tree = bundle_metadata(skill_name, clean)["tree_sha256"]
        await lock_key(conn, tenant_id, f"candidate-skill:{skill_name}")
        live = await self.live_for_skill(conn, skill_name)
        if live is not None:
            return await self._revise(
                conn, tenant_id=tenant_id, item=live, bundle=clean, tree=tree,
                detail=detail, base_version=base_version, actor=actor,
            )
        candidate_id = candidate_id or new_candidate_id(skill_name)
        await conn.execute(
            f"INSERT INTO {self._schema}.skill_candidates "
            "(tenant_id, candidate_id, skill_name, proposed_action, base_version, tree_sha256, detail) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
            tenant_id,
            candidate_id,
            skill_name,
            proposed_action,
            int(base_version),
            tree,
            json.dumps(detail or {}),
        )
        await self._write_files(conn, tenant_id=tenant_id, candidate_id=candidate_id, bundle=clean)
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="created",
            to_status="open", revision=1, actor=actor, detail={"tree_sha256": tree},
        )
        return await self.require(conn, candidate_id)

    async def _revise(
        self,
        conn: Any,
        *,
        tenant_id: str,
        item: dict[str, Any],
        bundle: dict[str, bytes],
        tree: str,
        detail: dict[str, Any] | None,
        base_version: int,
        actor: str,
    ) -> dict[str, Any]:
        candidate_id = item["candidate_id"]
        if actor == "evolution" and int(item["base_version"]) != int(base_version):
            raise CandidateStateError("candidate baseline changed; archive the old package before evolving")
        if item["status"] == "validating" and actor == "evolution":
            raise CandidateStateError("candidate is validating; wait for Replay to finish before revising")
        revision = int(item["revision"]) + 1
        merged = {**item["detail"], **(detail or {})}
        if actor != "evolution":
            merged["user_modified"] = True
        await conn.execute(
            f"UPDATE {self._schema}.skill_candidates SET revision = $2, tree_sha256 = $3, "
            "  detail = $4::jsonb, base_version = $5, status = 'open', verdict = '', "
            "  proposed_action = CASE WHEN $5>0 THEN 'update' ELSE 'create' END, "
            "  evaluation = '{}'::jsonb, decision = '{}'::jsonb, validate_task_id = '', updated_at = now() "
            "WHERE candidate_id = $1",
            candidate_id,
            revision,
            tree,
            json.dumps(merged),
            int(base_version),
        )
        await self._write_files(conn, tenant_id=tenant_id, candidate_id=candidate_id, bundle=bundle)
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="revised",
            from_status=item["status"], to_status="open", revision=revision, actor=actor,
            detail={"tree_sha256": tree},
        )
        return await self.require(conn, candidate_id)

    async def revise_content(
        self,
        conn: Any,
        *,
        tenant_id: str,
        candidate_id: str,
        bundle: Mapping[str, bytes | bytearray | str],
        limits: BundleLimits,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Human edit of a live candidate before publish (bumps revision)."""
        item = await self.require(conn, candidate_id, for_update=True)
        if item["status"] in TERMINAL:
            raise CandidateStateError("processed candidates are read-only")
        self._check_revision(item, expected_revision)
        clean = validate_bundle(item["skill_name"], bundle, limits)
        tree = bundle_metadata(item["skill_name"], clean)["tree_sha256"]
        return await self._revise(
            conn, tenant_id=tenant_id, item=item, bundle=clean, tree=tree, detail=None,
            base_version=int(item["base_version"]), actor=actor,
        )

    async def transition(
        self,
        conn: Any,
        *,
        tenant_id: str,
        candidate_id: str,
        to_status: str,
        actor: str,
        expected_revision: int | None = None,
        verdict: str | None = None,
        evaluation: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
        published_version: int | None = None,
        mutation_id: str | None = None,
        validate_task_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Move a candidate to ``to_status`` along :data:`TRANSITIONS` (audited)."""
        item = await self.require(conn, candidate_id, for_update=True)
        from_status = item["status"]
        if to_status not in TRANSITIONS.get(from_status, frozenset()):
            raise CandidateStateError(f"illegal candidate transition {from_status} -> {to_status}")
        self._check_revision(item, expected_revision)
        if verdict is not None and verdict not in VERDICTS:
            raise SkillLibraryError(f"invalid verdict: {verdict!r}")
        sets = ["status = $2", "updated_at = now()"]
        args: list[Any] = [candidate_id, to_status]

        def _set(column: str, value: Any, cast: str = "") -> None:
            args.append(value)
            sets.append(f"{column} = ${len(args)}{cast}")

        if verdict is not None:
            _set("verdict", verdict)
        if evaluation is not None:
            _set("evaluation", json.dumps(evaluation), "::jsonb")
        if decision is not None:
            _set("decision", json.dumps(decision), "::jsonb")
        if published_version is not None:
            _set("published_version", int(published_version))
        if mutation_id is not None:
            _set("mutation_id", mutation_id)
        if validate_task_id is not None:
            _set("validate_task_id", validate_task_id)
        if to_status == "published":
            sets.append("adopted = TRUE")
            sets.append("rejected = FALSE")
        if to_status == "rejected":
            sets.append("rejected = TRUE")
            sets.append("adopted = FALSE")
        if to_status in TERMINAL:
            sets.append("decided_at = now()")
        await conn.execute(
            f"UPDATE {self._schema}.skill_candidates SET {', '.join(sets)} WHERE candidate_id = $1",
            *args,
        )
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="transition",
            from_status=from_status, to_status=to_status, revision=int(item["revision"]),
            actor=actor,
            detail={
                k: v for k, v in {
                    "verdict": verdict, "reason": reason, "published_version": published_version,
                    "mutation_id": mutation_id, "validate_task_id": validate_task_id,
                }.items() if v not in (None, "")
            },
        )
        return await self.require(conn, candidate_id)

    async def record_validation_blocked(
        self, conn: Any, *, tenant_id: str, candidate_id: str, revision: int, reason: str,
    ) -> None:
        """Audit an acceptance failure without inventing a Replay verdict."""
        item = await self.require(conn, candidate_id, for_update=True)
        self._check_revision(item, revision)
        if item["status"] != "open":
            raise CandidateStateError("blocked validation requires an open candidate")
        evaluation = {
            "status": "blocked", "candidate_revision": revision, "recommended_publish": False,
            "static_validation": item["detail"].get("static_validation") or {},
            "replay": {"status": "blocked", "reason": reason,
                       "runtime_gate": {"status": "inconclusive", "reason": reason}},
        }
        await conn.execute(
            f"UPDATE {self._schema}.skill_candidates SET evaluation=$2::jsonb, updated_at=now() "
            "WHERE candidate_id=$1",
            candidate_id, json.dumps(evaluation),
        )
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="feedback",
            from_status="open", to_status="open", revision=revision, actor="evolution",
            detail={"validation_acceptance": {"status": "blocked", "reason": reason}},
        )

    async def record_verdict(
        self,
        conn: Any,
        *,
        tenant_id: str,
        candidate_id: str,
        revision: int,
        verdict: str,
        evaluation: dict[str, Any],
        actor: str = "validator",
        task_id: str = "",
    ) -> dict[str, Any] | None:
        """Store a central validation verdict for ``revision`` (validating -> open).

        Returns ``None`` (and writes nothing) when the candidate moved on — a
        newer revision or a terminal state — so a late validation result cannot
        overwrite fresher state.
        """
        if verdict not in VERDICTS:
            raise SkillLibraryError(f"invalid verdict: {verdict!r}")
        item = await self.require(conn, candidate_id, for_update=True)
        if int(item["revision"]) != int(revision) or item["status"] in TERMINAL:
            return None
        if task_id and item["validate_task_id"] != task_id:
            return None
        to_status = "open"
        await conn.execute(
            f"UPDATE {self._schema}.skill_candidates SET status = $2, verdict = $3, "
            "  evaluation = $4::jsonb, updated_at = now() WHERE candidate_id = $1",
            candidate_id,
            to_status,
            verdict,
            json.dumps(evaluation),
        )
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="verdict",
            from_status=item["status"], to_status=to_status, revision=int(revision), actor=actor,
            detail={"verdict": verdict},
        )
        return await self.require(conn, candidate_id)

    async def set_marks(
        self,
        conn: Any,
        *,
        tenant_id: str,
        candidate_id: str,
        changes: dict[str, bool],
        actor: str,
    ) -> dict[str, Any]:
        """Manual human marks. Only ``reviewed`` is user-settable; processed
        candidates are read-only (adopted/rejected follow publish/reject)."""
        if set(changes) != {"reviewed"} or type(changes.get("reviewed")) is not bool:
            raise SkillLibraryError("only reviewed can be marked manually")
        item = await self.require(conn, candidate_id, for_update=True)
        if item["status"] in TERMINAL:
            raise CandidateStateError("processed candidates are read-only")
        if item["reviewed"] == changes["reviewed"]:
            return item
        await conn.execute(
            f"UPDATE {self._schema}.skill_candidates SET reviewed = $2, updated_at = now() "
            "WHERE candidate_id = $1",
            candidate_id,
            changes["reviewed"],
        )
        await self._event(
            conn, tenant_id=tenant_id, candidate_id=candidate_id, kind="feedback",
            from_status=item["status"], to_status=item["status"], revision=int(item["revision"]),
            actor=actor, detail={"reviewed": changes["reviewed"]},
        )
        return await self.require(conn, candidate_id)


__all__ = [
    "STATUSES",
    "TERMINAL",
    "TRANSITIONS",
    "VERDICTS",
    "MARKS",
    "CandidateStateError",
    "CandidateRepository",
    "new_candidate_id",
]
