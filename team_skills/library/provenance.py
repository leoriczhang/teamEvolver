"""Version origins derived from an exact committed mutation or cloud receipt."""
from __future__ import annotations


def version_provenance(*, source="", actor="", action="", base_version=None, metadata=None, record_id="") -> dict:
    metadata = metadata or {}
    kind = "te" if source == "candidate-review" else "manual" if source in {
        "console", "transfer", "workbench",
    } else "unknown"
    return {
        "kind": kind, "source": source, "actor": actor, "action": action,
        "base_version": int(base_version) if base_version is not None else None,
        "candidate_id": str(metadata.get("candidate_id") or ""),
        "candidate_revision": metadata.get("revision"),
        "user_modified": bool(metadata.get("user_modified")),
        "record_id": record_id,
    }


def recorded_version(row: dict) -> dict:
    """Enrich a PG version with its precisely joined mutation."""
    return {
        **{k: v for k, v in row.items() if k not in {"mutation_source", "mutation_metadata", "previous_version"}},
        "provenance": version_provenance(
            source=row.get("mutation_source") or "", actor=row.get("actor") or "",
            action=row.get("action") or "", base_version=row.get("previous_version"),
            metadata=row.get("mutation_metadata"), record_id=row.get("mutation_id") or "",
        ),
    }
