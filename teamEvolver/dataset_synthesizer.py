"""Synthesize progressive test datasets from sessions and team SOP evidence.

This module is intentionally standalone inside teamEvolver. It consumes the
same accumulated session/evidence context used to edit a Skill and emits test
datasets that become the candidate's True Replay contract.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .dataset_store import (
    SkillDatasetStore,
    dataset_material_integrity,
)
from .session_materials import collect_session_materials
from .storage import is_not_found_error

DATASET_FORMAT = "teamEvolver-progressive-test-v1"
_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*")
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.IGNORECASE)

_SYNTHESIZE_SYSTEM = """\
You generate held-out test datasets for Skill evolution.

The same accumulated sessions and team SOP evidence were used to produce a
candidate Skill. Build {case_count} realistic TEST cases that evaluate whether
the candidate internalized those reusable procedures without leaking the full
requirements in the initial query.

For every test case:
1. query: a self-contained initial user request. It MUST NOT enumerate or reveal
   the checklist.
2. requirements: {min_requirements}-{max_requirements} flat, independently
   verifiable output/content requirements. No nested items. Every requirement
   must be grounded in the supplied sessions or team SOP evidence.
3. trajectory_requirements: flat checks for how the task is executed, including
   material reads, tool operations, calculations, validation, and artifact
   writes when supported by evidence.
4. source_session_ids: the session ids that ground the case.
5. evidence_window: "recent" or "historical".
6. name: a concise human-readable test name.

Every generated case must be runnable from the dataset by itself. If the source
task used files or directories but their bytes are not present in the supplied
evidence, do not reference those paths. Instead, inline a compact realistic
fixture under a `材料：` section in query. Never invent a filename, archive,
input directory, or material that the replay environment cannot provide.

The replay protocol reveals only query on interaction 1. After each interaction
an independent checklist judge identifies unmet requirements; only the next
batch of unmet requirements is disclosed. Therefore requirements must be
specific enough to judge from the response, tool trace, and artifacts.

Do not add generic formatting rules unsupported by evidence. Do not include
train/test scoring, weights, or aggregate scores.

Return JSON only:
{
  "test_datasets": [
    {
      "name": "...",
      "query": "...",
      "requirements": ["..."],
      "trajectory_requirements": ["..."],
      "source_session_ids": ["..."],
      "evidence_window": "recent"
    }
  ]
}
"""


def _effective_synthesize_system() -> str:
    try:
        from .evolve.prompt_studio import effective_prompt

        return effective_prompt("dataset_synthesis", _SYNTHESIZE_SYSTEM)
    except Exception:
        return _SYNTHESIZE_SYSTEM


def _synthesis_call_options() -> dict[str, Any]:
    try:
        from .evolve.prompt_studio import stage_call_options

        return stage_call_options("dataset_synthesis")
    except Exception:  # noqa: BLE001 - retain stable stage defaults
        return {"max_tokens": 16_384, "temperature": 0.3}


def render_synthesis_prompt(
    template: str,
    *,
    case_count: int,
    min_requirements: int,
    max_requirements: int,
) -> str:
    return (
        str(template)
        .replace("{case_count}", str(case_count))
        .replace("{min_requirements}", str(min_requirements))
        .replace("{max_requirements}", str(max_requirements))
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def flatten_requirements(raw: Any) -> list[str]:
    """Normalize list/numbered Markdown into unique flat requirement strings."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("requirement") or ""
        for line in str(value or "").splitlines():
            text = _LIST_PREFIX_RE.sub("", line).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def checklist_items(
    requirements: Iterable[Any],
    trajectory_requirements: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    """Build stable checklist ids shared by synthesizer, replay, and UI."""
    output = flatten_requirements(list(requirements))
    trajectory = flatten_requirements(list(trajectory_requirements))
    items = [
        {
            "id": f"R{index:02d}",
            "text": text,
            "kind": "output",
        }
        for index, text in enumerate(output, start=1)
    ]
    items.extend(
        {
            "id": f"T{index:02d}",
            "text": text,
            "kind": "trajectory",
        }
        for index, text in enumerate(trajectory, start=1)
    )
    return items


def _first_prompt(session: Mapping[str, Any]) -> str:
    for turn in session.get("turns") or []:
        if isinstance(turn, Mapping):
            text = str(
                turn.get("prompt_text") or turn.get("instruction") or ""
            ).strip()
            if text:
                return text
    return ""


def _session_payload(session: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(session.get("session_id") or ""),
        "initial_query": _first_prompt(session)[:12_000],
        "summary": str(session.get("_summary") or "")[:16_000],
        "trajectory": str(session.get("_trajectory") or "")[:32_000],
        "evidence_window": str(session.get("_evidence_window") or "recent"),
        "has_tool_errors": bool(session.get("_has_tool_errors")),
    }


def _seed_cases(
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for window in ("recent", "historical"):
        for raw in (replay_windows or {}).get(window) or []:
            if not isinstance(raw, Mapping):
                continue
            seeds.append(
                {
                    "session_id": str(raw.get("session_id") or ""),
                    "turn_num": int(raw.get("turn_num") or 0),
                    "instruction": str(raw.get("instruction") or "")[:12_000],
                    "reference_response": str(
                        raw.get("reference_response") or ""
                    )[:16_000],
                    "evidence_window": window,
                }
            )
    return seeds


def _team_evidence_claims(candidate_skill: Mapping[str, Any]) -> list[str]:
    classification = (
        candidate_skill.get("_evidence_classification")
        if isinstance(candidate_skill.get("_evidence_classification"), Mapping)
        else candidate_skill.get("evidence_classification")
        if isinstance(candidate_skill.get("evidence_classification"), Mapping)
        else {}
    )
    claims: list[str] = []
    for raw in classification.get("team_skill") or []:
        value = (
            raw.get("claim") or raw.get("text") or raw.get("requirement")
            if isinstance(raw, Mapping)
            else raw
        )
        text = str(value or "").strip()
        if text and text not in claims:
            claims.append(text)
    return claims


def _parse_json_object(raw: str) -> Optional[dict[str, Any]]:
    clean = _FENCE_RE.sub("", str(raw or "").strip())
    candidates = [clean]
    start, end = clean.find("{"), clean.rfind("}")
    if start >= 0 and end > start:
        candidates.append(clean[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _dataset_id(
    *,
    skill_name: str,
    query: str,
    source_session_ids: list[str],
) -> str:
    digest = hashlib.sha256(
        "\0".join([skill_name, query, *source_session_ids]).encode("utf-8")
    ).hexdigest()
    return f"synth-{digest[:20]}"


def _normalize_dataset(
    raw: Mapping[str, Any],
    *,
    skill_name: str,
    min_requirements: int,
    max_requirements: int,
    batch_size: int,
    default_window: str,
    synthesis_mode: str,
    materials: Optional[list[dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    query = str(raw.get("query") or raw.get("instruction") or "").strip()
    if not query:
        return None
    requirements = flatten_requirements(raw.get("requirements"))[:max_requirements]
    trajectory = flatten_requirements(raw.get("trajectory_requirements"))
    if not requirements and not trajectory:
        return None
    if synthesis_mode == "model" and len(requirements) < min_requirements:
        return None
    source_ids = list(
        dict.fromkeys(
            str(item or "").strip()
            for item in raw.get("source_session_ids") or []
            if str(item or "").strip()
        )
    )
    window = str(raw.get("evidence_window") or default_window).strip().lower()
    if window not in {"recent", "historical"}:
        window = default_window
    checklist = checklist_items(requirements, trajectory)
    dataset_id = _dataset_id(
        skill_name=skill_name,
        query=query,
        source_session_ids=source_ids,
    )
    dataset_materials = [
        dict(item)
        for item in materials or []
        if isinstance(item, Mapping)
        and item.get("path")
        and (
            not source_ids
            or not item.get("source_session_id")
            or str(item.get("source_session_id")) in source_ids
        )
    ]
    dataset = {
        "dataset_id": dataset_id,
        "dataset_format": DATASET_FORMAT,
        "skill_name": skill_name,
        "split": "test",
        "name": str(raw.get("name") or "").strip() or query.splitlines()[0][:80],
        "query": query,
        "requirements": requirements,
        "trajectory_requirements": trajectory,
        "checklist": checklist,
        "source_session_ids": source_ids,
        "evidence_window": window,
        "synthesis_mode": synthesis_mode,
        "requirement_count": len(requirements),
        "minimum_requirement_target": min_requirements,
        "materials": dataset_materials,
        "progressive_disclosure": {
            "enabled": True,
            "initial_visibility": "query_only",
            "batch_size": max(1, int(batch_size)),
            "stop_when": "all_checklist_items_satisfied",
        },
        "created_at": _utc_now_iso(),
    }
    if not dataset_material_integrity(
        dataset,
        available_paths=[
            str(item.get("path") or "")
            for item in dataset_materials
            if item.get("content_b64")
        ],
    )["complete"]:
        return None
    return dataset


def _requirements_from_session(session: Mapping[str, Any]) -> list[str]:
    requirements: list[str] = []
    for turn in session.get("turns") or []:
        if not isinstance(turn, Mapping):
            continue
        prompt = str(turn.get("prompt_text") or turn.get("instruction") or "")
        match = re.search(
            r"(?ims)^###\s*要求\s*$\s*(.*?)(?=^###\s+|\Z)",
            prompt,
        )
        if match:
            requirements.extend(flatten_requirements(match.group(1)))
    return requirements


def _fallback_datasets(
    *,
    skill_name: str,
    sessions: list[dict[str, Any]],
    candidate_skill: Mapping[str, Any],
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]],
    min_requirements: int,
    max_requirements: int,
    batch_size: int,
    case_count: int,
    materials: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    grounded_requirements: list[str] = []
    for claim in _team_evidence_claims(candidate_skill):
        grounded_requirements.extend(flatten_requirements(claim))
    for session in sessions:
        grounded_requirements.extend(_requirements_from_session(session))
    grounded_requirements = list(dict.fromkeys(grounded_requirements))[
        :max_requirements
    ]

    seeds = _seed_cases(replay_windows)
    if not seeds:
        seeds = [
            {
                "instruction": _first_prompt(session),
                "session_id": str(session.get("session_id") or ""),
                "evidence_window": str(
                    session.get("_evidence_window") or "recent"
                ),
            }
            for session in sessions
            if _first_prompt(session)
        ]
    datasets: list[dict[str, Any]] = []
    for index, seed in enumerate(seeds[: max(1, case_count)]):
        dataset = _normalize_dataset(
            {
                "name": f"{skill_name} test {index + 1}",
                "query": seed.get("instruction"),
                "requirements": grounded_requirements,
                "trajectory_requirements": [],
                "source_session_ids": [seed.get("session_id")],
                "evidence_window": seed.get("evidence_window"),
            },
            skill_name=skill_name,
            min_requirements=min_requirements,
            max_requirements=max_requirements,
            batch_size=batch_size,
            default_window="recent" if index == 0 else "historical",
            synthesis_mode="grounded_fallback",
            materials=materials,
        )
        if dataset:
            datasets.append(dataset)
    return datasets


async def synthesize_evolution_datasets(
    llm: Any,
    *,
    skill_name: str,
    sessions: list[dict[str, Any]],
    candidate_skill: Mapping[str, Any],
    evidence_context: Optional[Mapping[str, Any]] = None,
    replay_windows: Optional[Mapping[str, list[dict[str, Any]]]] = None,
    case_count: int = 2,
    min_requirements: int = 12,
    max_requirements: int = 24,
    batch_size: int = 4,
) -> list[dict[str, Any]]:
    """Create test datasets from the exact evidence used for Skill evolution."""
    case_count = max(1, min(6, int(case_count or 2)))
    min_requirements = max(1, int(min_requirements or 1))
    max_requirements = max(min_requirements, int(max_requirements or 24))
    batch_size = max(1, int(batch_size or 1))
    compact_sessions = [_session_payload(session) for session in sessions[:50]]
    source_materials = collect_session_materials(sessions)
    payload = {
        "skill_name": skill_name,
        "candidate_skill": {
            "description": str(candidate_skill.get("description") or ""),
            "content": str(candidate_skill.get("content") or "")[:32_000],
            "edit_summary": candidate_skill.get("edit_summary") or {},
        },
        "team_sop_evidence": {
            "context": dict(evidence_context or {}),
            "claims": _team_evidence_claims(candidate_skill),
        },
        "sessions": compact_sessions,
        "replay_seeds": _seed_cases(replay_windows),
    }
    parsed: Optional[dict[str, Any]] = None
    try:
        raw = await llm.chat(
            [
                {
                    "role": "system",
                    "content": render_synthesis_prompt(
                        _effective_synthesize_system(),
                        case_count=case_count,
                        min_requirements=min_requirements,
                        max_requirements=max_requirements,
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            **_synthesis_call_options(),
            trace_name=f"team-skill-evolver:dataset_synthesis:{skill_name}",
            trace_tags=[
                "team-skill-evolver",
                "dataset-synthesis",
                f"skill:{skill_name}",
            ],
            trace_metadata={
                "skill_name": skill_name,
                "session_count": len(compact_sessions),
                "case_count": case_count,
            },
        )
        parsed = _parse_json_object(raw)
    except Exception:
        parsed = None

    datasets: list[dict[str, Any]] = []
    raw_datasets = (
        parsed.get("test_datasets")
        if isinstance(parsed, Mapping)
        and isinstance(parsed.get("test_datasets"), list)
        else []
    )
    for index, raw_dataset in enumerate(raw_datasets[:case_count]):
        if not isinstance(raw_dataset, Mapping):
            continue
        dataset = _normalize_dataset(
            raw_dataset,
            skill_name=skill_name,
            min_requirements=min_requirements,
            max_requirements=max_requirements,
            batch_size=batch_size,
            default_window="recent" if index == 0 else "historical",
            synthesis_mode="model",
            materials=source_materials,
        )
        if dataset:
            datasets.append(dataset)
    if datasets:
        return datasets
    return _fallback_datasets(
        skill_name=skill_name,
        sessions=sessions,
        candidate_skill=candidate_skill,
        replay_windows=replay_windows,
        min_requirements=min_requirements,
        max_requirements=max_requirements,
        batch_size=batch_size,
        case_count=case_count,
        materials=source_materials,
    )


def dataset_to_replay_case(dataset: Mapping[str, Any]) -> dict[str, Any]:
    """Project a synthesized dataset into the True Replay job contract."""
    source = (
        dataset.get("source")
        if isinstance(dataset.get("source"), Mapping)
        else {}
    )
    checklist = [
        dict(item)
        for item in dataset.get("checklist") or []
        if isinstance(item, Mapping) and item.get("text")
    ]
    if not checklist:
        checklist = checklist_items(
            flatten_requirements(dataset.get("requirements")),
            flatten_requirements(dataset.get("trajectory_requirements")),
        )
    source_ids = [
        str(item or "")
        for item in (
            dataset.get("source_session_ids")
            or source.get("source_session_ids")
            or []
        )
        if str(item or "")
    ]
    session_id = str(
        source.get("session_id")
        or (source_ids[0] if source_ids else "")
        or ""
    )
    return {
        "case_id": str(dataset.get("dataset_id") or ""),
        "dataset_id": str(dataset.get("dataset_id") or ""),
        "dataset_format": str(dataset.get("dataset_format") or DATASET_FORMAT),
        "skill_name": str(dataset.get("skill_name") or ""),
        "session_id": session_id,
        "source_session_ids": source_ids,
        "turn_num": int(source.get("turn_num") or 1),
        "instruction": str(dataset.get("query") or ""),
        "query": str(dataset.get("query") or ""),
        "requirements": flatten_requirements(dataset.get("requirements")),
        "trajectory_requirements": flatten_requirements(
            dataset.get("trajectory_requirements")
        ),
        "checklist": checklist,
        "progressive_disclosure": dict(
            dataset.get("progressive_disclosure") or {}
        ),
        "materials": [
            dict(item)
            for item in dataset.get("materials") or []
            if isinstance(item, Mapping) and item.get("path")
        ],
        "evidence_window": str(
            dataset.get("evidence_window")
            or source.get("evidence_window")
            or "recent"
        ),
    }


class SynthesizedDatasetStore:
    """Persist generated datasets independently from validation job indexes."""

    def __init__(self, bucket: Any, *, prefix: str = "") -> None:
        self._bucket = bucket
        self._prefix = str(prefix or "")

    def _key(self, skill_name: str, generation_id: str) -> str:
        digest = hashlib.sha256(skill_name.encode("utf-8")).hexdigest()[:12]
        safe_generation = re.sub(
            r"[^A-Za-z0-9._-]+", "-", str(generation_id or "")
        ).strip("-")
        return (
            f"{self._prefix}evolution_datasets/"
            f"{digest}/{safe_generation or 'generation'}.json"
        )

    def save_generation(
        self,
        *,
        skill_name: str,
        generation_id: str,
        datasets: list[dict[str, Any]],
        source_session_ids: list[str],
        candidate_revision: int,
    ) -> dict[str, Any]:
        now = _utc_now_iso()
        stored_datasets = []
        for raw in datasets:
            item = dict(raw)
            item["materials"] = [
                {
                    key: material.get(key)
                    for key in (
                        "path",
                        "size",
                        "sha256",
                        "source_session_id",
                    )
                    if material.get(key) is not None
                }
                for material in raw.get("materials") or []
                if isinstance(material, Mapping) and material.get("path")
            ]
            stored_datasets.append(item)
        payload = {
            "schema_version": 1,
            "dataset_format": DATASET_FORMAT,
            "skill_name": skill_name,
            "generation_id": generation_id,
            "candidate_revision": int(candidate_revision or 1),
            "source_session_ids": list(dict.fromkeys(source_session_ids)),
            "datasets": stored_datasets,
            "created_at": now,
        }
        self._bucket.put_object(
            self._key(skill_name, generation_id),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        repository = SkillDatasetStore(self._bucket, prefix=self._prefix)
        for raw in datasets:
            if not isinstance(raw, Mapping) or not raw.get("dataset_id"):
                continue
            dataset_id = str(raw.get("dataset_id") or "")
            existing = repository.load_dataset(
                skill_name=skill_name,
                dataset_id=dataset_id,
            )
            existing_source = (
                existing.get("source")
                if isinstance((existing or {}).get("source"), Mapping)
                else {}
            )
            # Editable lab datasets may be selected as fixed regressions. The
            # generation audit references them but must not make them read-only.
            if existing and (
                str(existing_source.get("kind") or "") != "evolution"
                or not bool(existing.get("read_only", True))
                or bool(existing_source.get("user_edited"))
            ):
                continue
            generation_ids = list(
                dict.fromkeys(
                    [
                        *(
                            existing_source.get("generation_ids")
                            if isinstance(
                                existing_source.get("generation_ids"),
                                list,
                            )
                            else []
                        ),
                        generation_id,
                    ]
                )
            )
            dataset_source_ids = [
                str(item or "")
                for item in raw.get("source_session_ids") or []
                if str(item or "")
            ]
            decoded_materials: list[tuple[str, bytes]] = []
            material_sources: dict[str, str] = {}
            for material in raw.get("materials") or []:
                if not isinstance(material, Mapping) or not material.get("path"):
                    continue
                rel_path = str(material.get("path") or "")
                try:
                    data = base64.b64decode(
                        str(material.get("content_b64") or ""),
                        validate=True,
                    )
                except (binascii.Error, ValueError):
                    continue
                decoded_materials.append((rel_path, data))
                material_sources[rel_path] = str(
                    material.get("source_session_id") or ""
                )
            material_records = (
                repository.replace_materials(
                    skill_name=skill_name,
                    dataset_id=dataset_id,
                    files=decoded_materials,
                )
                if decoded_materials
                else list((existing or {}).get("materials") or [])
            )
            for record in material_records:
                source_session_id = material_sources.get(
                    str(record.get("path") or "")
                )
                if source_session_id:
                    record["source_session_id"] = source_session_id
            dataset_payload = dict(raw)
            dataset_payload["materials"] = material_records
            repository.save_dataset(
                {
                    **dataset_payload,
                    "skill_name": skill_name,
                    "source": {
                        "kind": "evolution",
                        "job_id": generation_id,
                        "generation_ids": generation_ids,
                        "source_session_ids": dataset_source_ids,
                        "session_id": (
                            dataset_source_ids[0]
                            if dataset_source_ids
                            else ""
                        ),
                        "evidence_window": str(
                            raw.get("evidence_window") or "recent"
                        ),
                        "candidate_revision": int(candidate_revision or 1),
                    },
                    "read_only": True,
                    "enabled_for_evolution": False,
                    "created_at": str(
                        (existing or {}).get("created_at")
                        or raw.get("created_at")
                        or now
                    ),
                    "updated_at": now,
                }
            )
        return payload

    def load_generation(
        self,
        *,
        skill_name: str,
        generation_id: str,
    ) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(
                self._bucket.get_object(
                    self._key(skill_name, generation_id)
                ).read().decode("utf-8")
            )
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise
        return payload if isinstance(payload, dict) else None
