"""Versioned, Root-authored external evaluator adapters using existing PG storage."""
from __future__ import annotations

import ast
import copy
import hashlib
import re
from dataclasses import dataclass, field
from types import ModuleType

from .datasets.repository import DatasetConflict, DatasetNotFound, lock

_PREFIX = "evaluator:"


def validate_evaluator_source(source, filename):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.py", filename):
        raise ValueError("invalid evaluator adapter filename")
    if not isinstance(source, str) or not source.strip() or len(source.encode()) > 256 * 1024:
        raise ValueError("evaluator adapter source must contain 1..262144 bytes")
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        raise ValueError(f"invalid evaluator Python at line {exc.lineno}") from None
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "evaluate"]
    if len(functions) != 1:
        raise ValueError("one synchronous evaluate(request, config, *, secret='') function is required")
    declarations = [n for n in tree.body if isinstance(n, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "EVALUATOR_ADAPTER" for target in n.targets
    )]
    if len(declarations) != 1:
        raise ValueError("one literal EVALUATOR_ADAPTER metadata dictionary is required")
    try:
        meta = ast.literal_eval(declarations[0].value)
    except (ValueError, TypeError):
        raise ValueError("EVALUATOR_ADAPTER must be a literal dictionary") from None
    if (not isinstance(meta, dict) or type(meta.get("enabled", True)) is not bool
            or not isinstance(meta.get("label", filename), str)):
        raise ValueError("invalid EVALUATOR_ADAPTER label or enabled field")
    return {"enabled": meta.get("enabled", True), "label": meta.get("label", filename)[:160]}


@dataclass(frozen=True)
class BoundEvaluator:
    evaluate_function: object
    config: dict
    secret: str = field(repr=False)

    def evaluate(self, request):
        try:
            return self.evaluate_function(copy.deepcopy(request), copy.deepcopy(self.config), secret=self.secret)
        except Exception:
            # Arbitrary provider and adapter exceptions may include credentials or input text.
            raise RuntimeError("external evaluator adapter execution failed") from None


def build_evaluator(source, filename, config, *, tenant_id, secret=""):
    meta = validate_evaluator_source(source, filename)
    if not meta["enabled"]:
        raise ValueError("external evaluator adapter is disabled")
    module = ModuleType(f"_replay_evaluator_{tenant_id}_{hashlib.sha256(source.encode()).hexdigest()}")
    try:
        exec(compile(source, filename, "exec"), module.__dict__)
    except Exception:
        raise ValueError("external evaluator adapter could not be loaded") from None
    return BoundEvaluator(module.evaluate, copy.deepcopy(config), secret)


class EvaluatorAdapterRepository:
    """The namespace prevents evaluator and business adapters sharing a filename."""

    def __init__(self, tx):
        self.schema = tx.pool.schema

    async def source(self, conn, filename, revision=None):
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.py", filename):
            raise ValueError("invalid evaluator adapter filename")
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ValueError("evaluator adapter revision must be a positive integer")
        row = await conn.fetchrow(
            f"SELECT v.* FROM {self.schema}.adapter_versions v JOIN {self.schema}.adapters a "
            "ON a.tenant_id=v.tenant_id AND a.adapter_id=v.adapter_id "
            "WHERE a.kind='replay_evaluator' AND a.adapter_id=$1 AND ($2::bigint IS NULL OR v.revision=$2) "
            "ORDER BY v.revision DESC LIMIT 1", _PREFIX + filename, revision,
        )
        if row is None:
            raise DatasetNotFound("external evaluator adapter not found")
        return {**dict(row), "file": filename}

    async def require(self, conn, reference):
        source = await self.source(conn, reference["file"], reference["revision"])
        actual_hash = hashlib.sha256(source["source"].encode()).hexdigest()
        if source["source_hash"] != reference["source_hash"] or actual_hash != reference["source_hash"]:
            raise ValueError("external evaluator adapter source hash mismatch")
        if not validate_evaluator_source(source["source"], reference["file"])["enabled"]:
            raise ValueError("external evaluator adapter is disabled")
        return source

    async def save(self, conn, *, tenant_id, filename, source, expected_revision):
        meta = validate_evaluator_source(source, filename)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected_revision must be a positive integer or null")
        adapter_id = _PREFIX + filename
        await lock(conn, tenant_id, "adapter:" + adapter_id)
        row = await conn.fetchrow(
            f"SELECT revision FROM {self.schema}.adapter_versions WHERE adapter_id=$1 "
            "ORDER BY revision DESC LIMIT 1", adapter_id,
        )
        current = row["revision"] if row else 0
        if (expected_revision or 0) != current:
            raise DatasetConflict("external evaluator adapter revision changed")
        digest = hashlib.sha256(source.encode()).hexdigest()
        await conn.execute(
            f"INSERT INTO {self.schema}.adapters(tenant_id,adapter_id,kind) VALUES($1,$2,'replay_evaluator') "
            "ON CONFLICT DO NOTHING", tenant_id, adapter_id,
        )
        kind = await conn.fetchval(f"SELECT kind FROM {self.schema}.adapters WHERE adapter_id=$1", adapter_id)
        if kind != "replay_evaluator":
            raise DatasetConflict("adapter filename belongs to another adapter type")
        await conn.execute(
            f"INSERT INTO {self.schema}.adapter_versions(tenant_id,adapter_id,revision,source_hash,source) "
            "VALUES($1,$2,$3,$4,$5)", tenant_id, adapter_id, current + 1, digest, source,
        )
        return {"file": filename, "revision": current + 1, "source_hash": digest, **meta}

    async def list(self, conn):
        rows = await conn.fetch(
            f"SELECT adapter_id FROM {self.schema}.adapters WHERE kind='replay_evaluator' ORDER BY adapter_id",
        )
        result = []
        for row in rows:
            filename = row["adapter_id"].removeprefix(_PREFIX)
            source = await self.source(conn, filename)
            result.append({"file": filename, "revision": source["revision"], "source_hash": source["source_hash"],
                           **validate_evaluator_source(source["source"], filename)})
        return result
