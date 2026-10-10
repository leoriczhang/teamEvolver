"""OpenViking-backed semantic patterns compiled from Session experiences."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from teamEvolver.config import resolve_viking_endpoint
from teamEvolver.storage import is_not_found_error
from teamEvolver.storage.viking import OpenVikingObjectStore

from .experience_library import session_experiences

PATTERN_ROOT = (
    "viking://resources/"
    "agent_knowledge_workspace/input/proven_experiences/patterns"
)
_SCHEMA_VERSION = 1
_COMPILE_LOCKS: dict[str, threading.Lock] = {}
_COMPILE_LOCKS_GUARD = threading.Lock()


class PatternStoreUnavailable(RuntimeError):
    """Raised when the strict OpenViking Pattern store is not configured."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_segment(value: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", text):
        return text
    return f"sha256-{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _pattern_key(value: Any, fallback: str) -> str:
    key = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return (key[:120] or f"pattern_{fallback[:16]}")


def _unique_strings(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value or "").strip()
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _compile_lock(skill_name: str) -> threading.Lock:
    with _COMPILE_LOCKS_GUARD:
        lock = _COMPILE_LOCKS.get(skill_name)
        if lock is None:
            lock = threading.Lock()
            _COMPILE_LOCKS[skill_name] = lock
        return lock


def _target_root(config: Any) -> str:
    explicit = str(getattr(config, "experience_pattern_root", "") or "").rstrip("/")
    if explicit:
        if not explicit.startswith("viking://resources/"):
            raise PatternStoreUnavailable("invalid OpenViking experience target")
        return (
            explicit
            if explicit.endswith("/patterns")
            else f"{explicit}/patterns"
        )
    raw = getattr(config, "experience_sync", {}) or {}
    base = (
        str(raw.get("target_directory") or "").rstrip("/")
        if isinstance(raw, dict)
        else ""
    )
    if not base:
        return PATTERN_ROOT
    if not base.startswith("viking://resources/"):
        raise PatternStoreUnavailable("invalid OpenViking experience target")
    return base if base.endswith("/patterns") else f"{base}/patterns"


def _config_value(config: Any, engine_name: str, host_name: str, default: str = "") -> str:
    return str(
        getattr(config, engine_name, "")
        or getattr(config, host_name, "")
        or default
    ).strip()


class ExperiencePatternStore:
    """Strict OV store with one independently updated document per Skill."""

    def __init__(
        self,
        bucket: Any,
        *,
        location: str = PATTERN_ROOT,
        prefix: str = "skills/",
    ) -> None:
        self._bucket = bucket
        self.location = location.rstrip("/")
        self._prefix = str(prefix or "").strip("/") + "/"

    @classmethod
    def from_config(cls, config: Any) -> "ExperiencePatternStore":
        endpoint = _config_value(
            config,
            "viking_endpoint",
            "sharing_viking_endpoint",
        )
        if not endpoint:
            endpoint = resolve_viking_endpoint(
                str(getattr(config, "sharing_viking_deployment", "") or ""),
                "",
            )
        api_key = str(
            getattr(config, "viking_api_key", "")
            or getattr(config, "sharing_viking_api_key", "")
            or ""
        ).strip()
        if not endpoint or not api_key:
            raise PatternStoreUnavailable(
                "OpenViking endpoint and Root Key are required for experience patterns"
            )
        account = _config_value(
            config,
            "viking_account",
            "sharing_viking_account",
            "default",
        )
        user = _config_value(
            config,
            "viking_user",
            "sharing_viking_user",
            "team",
        )
        location = _target_root(config)
        root_prefix = location[len("viking://resources/") :]
        bucket = OpenVikingObjectStore(
            endpoint=endpoint,
            api_key=api_key,
            account=account,
            user=user,
            agent="team-skill-evolver",
            root_prefix=root_prefix,
            namespace="resources",
        )
        return cls(bucket, location=location)

    def _key(self, skill_name: str) -> str:
        return f"{self._prefix}{_safe_segment(skill_name)}.json"

    def load_skill(self, skill_name: str) -> dict[str, Any] | None:
        try:
            payload = json.loads(
                self._bucket.get_object(self._key(skill_name))
                .read()
                .decode("utf-8")
            )
        except Exception as exc:
            if is_not_found_error(exc):
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def save_skill(self, document: dict[str, Any]) -> dict[str, Any]:
        skill_name = str(document.get("skill_name") or "").strip()
        if not skill_name:
            raise ValueError("pattern document requires skill_name")
        payload = dict(document)
        payload["schema_version"] = _SCHEMA_VERSION
        payload["updated_at"] = _utc_now_iso()
        self._bucket.put_object(
            self._key(skill_name),
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        return payload

    def list_patterns(
        self,
        *,
        kind: str = "",
        skill: str = "",
        status: str = "",
        search: str = "",
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        skill_counts: Counter[str] = Counter()
        for obj in self._bucket.iter_objects(prefix=self._prefix):
            if not obj.key.endswith(".json"):
                continue
            try:
                document = json.loads(
                    self._bucket.get_object(obj.key).read().decode("utf-8")
                )
            except Exception:
                continue
            if not isinstance(document, dict):
                continue
            skill_name = str(document.get("skill_name") or "").strip()
            for raw in document.get("patterns") or []:
                if not isinstance(raw, dict):
                    continue
                item = {
                    **raw,
                    "skill_name": skill_name,
                    "compiled_at": document.get("compiled_at"),
                    "updated_at": document.get("updated_at"),
                }
                items.append(item)
                skill_counts[skill_name] += 1

        stats = {
            "total_patterns": len(items),
            "ready_patterns": sum(
                str(item.get("status") or "") == "ready" for item in items
            ),
            "observing_patterns": sum(
                str(item.get("status") or "") == "observing" for item in items
            ),
            "defect_patterns": sum(
                str(item.get("kind") or "") == "defect" for item in items
            ),
            "exemplary_patterns": sum(
                str(item.get("kind") or "") == "exemplary" for item in items
            ),
            "source_experiences": sum(
                int(item.get("evidence_count") or 0) for item in items
            ),
            "skills": len(skill_counts),
        }
        needle = search.strip().casefold()
        items = [
            item
            for item in items
            if (not kind or item.get("kind") == kind)
            and (not skill or item.get("skill_name") == skill)
            and (not status or item.get("status") == status)
            and (
                not needle
                or needle
                in " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "skill_name",
                        "title",
                        "problem",
                        "root_cause",
                        "guidance",
                    )
                ).casefold()
            )
        ]
        items.sort(
            key=lambda item: (
                bool(item.get("team_ready")),
                int(item.get("occurrence_count") or 0),
                str(item.get("updated_at") or ""),
                str(item.get("pattern_id") or ""),
            ),
            reverse=True,
        )
        return {
            "items": items,
            "stats": stats,
            "skill_counts": dict(sorted(skill_counts.items())),
            "location": self.location,
        }

    def record_candidate_outcome(
        self,
        pattern_refs: list[dict[str, Any]],
        *,
        job_id: str,
        status: str,
        candidate_revision: int | None = None,
        version: int | None = None,
        reason: str = "",
    ) -> int:
        refs_by_skill: dict[str, set[str]] = {}
        for ref in pattern_refs or []:
            if not isinstance(ref, dict):
                continue
            skill_name = str(ref.get("skill_name") or "").strip()
            pattern_id = str(ref.get("pattern_id") or "").strip()
            if skill_name and pattern_id:
                refs_by_skill.setdefault(skill_name, set()).add(pattern_id)
        updated = 0
        for skill_name, pattern_ids in refs_by_skill.items():
            with _compile_lock(skill_name):
                document = self.load_skill(skill_name)
                if not document:
                    continue
                changed = False
                for pattern in document.get("patterns") or []:
                    if (
                        not isinstance(pattern, dict)
                        or pattern.get("pattern_id") not in pattern_ids
                    ):
                        continue
                    outcomes = [
                        dict(item)
                        for item in pattern.get("candidate_outcomes") or []
                        if isinstance(item, dict)
                    ]
                    outcome = {
                        "job_id": str(job_id or ""),
                        "status": str(status or ""),
                        "recorded_at": _utc_now_iso(),
                    }
                    if candidate_revision is not None:
                        outcome["candidate_revision"] = int(candidate_revision)
                    if version is not None:
                        outcome["version"] = int(version)
                    if str(reason or "").strip():
                        outcome["reason"] = str(reason).strip()[:1000]
                    position = next(
                        (
                            index
                            for index, item in enumerate(outcomes)
                            if str(item.get("job_id") or "") == outcome["job_id"]
                        ),
                        None,
                    )
                    if position is None:
                        outcomes.append(outcome)
                    else:
                        outcomes[position] = outcome
                    pattern["candidate_outcomes"] = outcomes[-50:]
                    changed = True
                    updated += 1
                if changed:
                    self.save_skill(document)
        return updated


def aggregate_session_experiences(
    sessions: list[dict[str, Any]],
    skill_name: str,
) -> list[dict[str, Any]]:
    """Aggregate named lessons from current and restored Evidence sessions."""
    groups: dict[str, dict[str, Any]] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        session_id = str(session.get("session_id") or "").strip()
        if not session_id:
            continue
        for lesson in session_experiences(session):
            if lesson["skill_name"] != skill_name:
                continue
            experience_id = hashlib.sha256(
                json.dumps(
                    (
                        lesson["skill_name"],
                        lesson["kind"],
                        lesson["experience_key"],
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            group = groups.setdefault(
                experience_id,
                {
                    **lesson,
                    "id": experience_id,
                    "occurrences": {},
                    "user_aliases": set(),
                },
            )
            group["occurrences"][session_id] = lesson["description"]
            user_alias = str(session.get("user_alias") or "").strip()
            if user_alias:
                group["user_aliases"].add(user_alias)
    items: list[dict[str, Any]] = []
    for group in groups.values():
        occurrences = dict(group["occurrences"])
        session_ids = sorted(occurrences)
        items.append(
            {
                key: value
                for key, value in group.items()
                if key not in {"occurrences", "user_aliases"}
            }
            | {
                "description": occurrences[session_ids[-1]],
                "occurrence_count": len(session_ids),
                "session_ids": session_ids,
                "user_aliases": sorted(group["user_aliases"]),
            }
        )
    items.sort(key=lambda item: (item["kind"], item["experience_key"], item["id"]))
    return items


def source_fingerprint(experiences: list[dict[str, Any]]) -> str:
    source = [
        {
            "id": str(item.get("id") or ""),
            "skill_name": str(item.get("skill_name") or ""),
            "kind": str(item.get("kind") or ""),
            "experience_key": str(item.get("experience_key") or ""),
            "description": str(item.get("description") or ""),
            "occurrence_count": int(item.get("occurrence_count") or 0),
            "session_ids": sorted(_unique_strings(item.get("session_ids"))),
            "user_aliases": sorted(_unique_strings(item.get("user_aliases"))),
        }
        for item in experiences
    ]
    source.sort(key=lambda item: item["id"])
    return _digest(source)


def _extract_json(text: str) -> Any:
    clean = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?\s*", "", clean).strip().rstrip("`").strip()
    try:
        return json.loads(clean)
    except (TypeError, ValueError):
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(clean):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(clean[index:])
            return value
        except ValueError:
            continue
    raise ValueError("pattern compiler returned invalid JSON")


class ExperiencePatternCompiler:
    """Use an LLM to merge semantically equivalent Experience records."""

    def __init__(self, llm: Any, store: ExperiencePatternStore) -> None:
        self._llm = llm
        self._store = store

    async def compile(
        self,
        skill_name: str,
        experiences: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        relevant = [
            dict(item)
            for item in experiences
            if isinstance(item, dict)
            and str(item.get("skill_name") or "").strip() == skill_name
            and str(item.get("id") or "").strip()
        ]
        if not relevant:
            raise ValueError(f"no experiences available for Skill {skill_name}")
        fingerprint = source_fingerprint(relevant)

        def load() -> dict[str, Any] | None:
            with _compile_lock(skill_name):
                return self._store.load_skill(skill_name)

        existing = await asyncio.to_thread(load)
        if (
            not force
            and existing
            and existing.get("source_fingerprint") == fingerprint
        ):
            return {"compiled": False, "document": existing}

        payload = [
            {
                "experience_id": item["id"],
                "kind": item.get("kind"),
                "experience_key": item.get("experience_key"),
                "description": item.get("description"),
                "occurrence_count": int(item.get("occurrence_count") or 0),
                "session_ids": _unique_strings(item.get("session_ids")),
                "user_aliases": _unique_strings(item.get("user_aliases")),
            }
            for item in relevant
        ]
        response = await self._llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 Skill 经验模式归纳器。将语义等价、即使 "
                        "experience_key 不同的经验合并为可执行模式；错误经验和优秀"
                        "实践不要混合。只能引用输入中的 experience_id，不得补造证据。"
                        "输出严格 JSON：{\"patterns\":[{\"pattern_key\":"
                        "\"stable_snake_case\",\"title\":\"...\",\"problem\":\"...\","
                        "\"root_cause\":\"...\",\"guidance\":\"...\","
                        "\"experience_ids\":[\"...\"]}]}。同一输入下 pattern_key 应稳定。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"skill_name": skill_name, "experiences": payload},
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.1,
            max_tokens=12000,
            trace_name="skill_experience_pattern.compile",
            trace_metadata={
                "component": "team_skills.evolution.patterns",
                "skill_name": skill_name,
                "experience_count": len(payload),
            },
        )
        parsed = _extract_json(response)
        raw_patterns = (
            parsed.get("patterns")
            if isinstance(parsed, dict)
            else parsed
        )
        if not isinstance(raw_patterns, list):
            raise ValueError("pattern compiler response requires a patterns list")
        patterns = self._normalize_patterns(
            skill_name,
            raw_patterns,
            relevant,
            existing,
        )
        if not patterns:
            raise ValueError("pattern compiler produced no grounded patterns")
        now = _utc_now_iso()
        document = {
            "schema_version": _SCHEMA_VERSION,
            "skill_name": skill_name,
            "source_fingerprint": fingerprint,
            "source_experience_count": len(relevant),
            "compiled_at": now,
            "updated_at": now,
            "patterns": patterns,
        }

        def save() -> dict[str, Any]:
            with _compile_lock(skill_name):
                latest = self._store.load_skill(skill_name)
                if (
                    not force
                    and latest
                    and latest.get("source_fingerprint") == fingerprint
                ):
                    return latest
                latest_patterns = {
                    str(item.get("pattern_id") or item.get("pattern_key") or ""): item
                    for item in (latest or {}).get("patterns") or []
                    if isinstance(item, dict)
                }
                for pattern in document["patterns"]:
                    prior = latest_patterns.get(
                        str(
                            pattern.get("pattern_id")
                            or pattern.get("pattern_key")
                            or ""
                        )
                    )
                    if not prior:
                        continue
                    outcomes = {
                        str(item.get("job_id") or ""): dict(item)
                        for item in prior.get("candidate_outcomes") or []
                        if isinstance(item, dict)
                        and str(item.get("job_id") or "")
                    }
                    outcomes.update(
                        {
                            str(item.get("job_id") or ""): dict(item)
                            for item in pattern.get("candidate_outcomes") or []
                            if isinstance(item, dict)
                            and str(item.get("job_id") or "")
                        }
                    )
                    pattern["candidate_outcomes"] = list(outcomes.values())[-50:]
                return self._store.save_skill(document)

        saved = await asyncio.to_thread(save)
        return {
            "compiled": saved.get("compiled_at") == now,
            "document": saved,
        }

    @staticmethod
    def _normalize_patterns(
        skill_name: str,
        raw_patterns: list[Any],
        experiences: list[dict[str, Any]],
        existing: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        by_id = {str(item["id"]): item for item in experiences}
        previous = [
            item
            for item in (existing or {}).get("patterns") or []
            if isinstance(item, dict)
        ]
        previous_by_key = {
            str(item.get("pattern_key") or ""): item
            for item in previous
            if str(item.get("pattern_key") or "")
        }
        used_previous: set[str] = set()
        patterns: list[dict[str, Any]] = []
        used_keys: set[str] = set()
        for raw in raw_patterns:
            if not isinstance(raw, dict):
                continue
            ids = [
                item
                for item in _unique_strings(
                    raw.get("experience_ids")
                    or raw.get("source_experience_ids")
                )
                if item in by_id
            ]
            if not ids:
                continue
            ids.sort()
            kinds = {str(by_id[item].get("kind") or "") for item in ids}
            if len(kinds) != 1 or next(iter(kinds)) not in {
                "defect",
                "exemplary",
            }:
                continue
            kind = next(iter(kinds))
            fallback = _digest([skill_name, kind, ids])
            key = _pattern_key(raw.get("pattern_key"), fallback)
            if key in used_keys:
                key = f"{key[:100]}_{fallback[:12]}"
            used_keys.add(key)
            prior = previous_by_key.get(key)
            if prior is None:
                source_set = set(ids)
                candidates = [
                    item
                    for item in previous
                    if str(item.get("pattern_id") or "") not in used_previous
                    and source_set.intersection(item.get("experience_ids") or [])
                ]
                prior = max(
                    candidates,
                    key=lambda item: len(
                        source_set.intersection(item.get("experience_ids") or [])
                    ),
                    default=None,
                )
            if prior:
                used_previous.add(str(prior.get("pattern_id") or ""))
            pattern_id = str(
                (prior or {}).get("pattern_id")
                or f"pat_{_digest([skill_name, key])[:24]}"
            )
            session_ids = sorted(
                {
                    sid
                    for experience_id in ids
                    for sid in _unique_strings(
                        by_id[experience_id].get("session_ids")
                    )
                }
            )
            user_aliases = sorted(
                {
                    user
                    for experience_id in ids
                    for user in _unique_strings(
                        by_id[experience_id].get("user_aliases")
                    )
                }
            )
            occurrence_count = sum(
                max(1, int(by_id[item].get("occurrence_count") or 0))
                for item in ids
            )
            team_ready = len(session_ids) >= 2 and len(user_aliases) >= 2
            patterns.append(
                {
                    "pattern_id": pattern_id,
                    "pattern_key": key,
                    "skill_name": skill_name,
                    "title": str(raw.get("title") or key).strip()[:240],
                    "kind": kind,
                    "problem": str(raw.get("problem") or "").strip()[:4000],
                    "root_cause": str(raw.get("root_cause") or "").strip()[:4000],
                    "guidance": str(raw.get("guidance") or "").strip()[:6000],
                    "experience_ids": ids,
                    "session_ids": session_ids,
                    "user_aliases": user_aliases,
                    "evidence_count": len(ids),
                    "occurrence_count": occurrence_count,
                    "session_count": len(session_ids),
                    "user_count": len(user_aliases),
                    "team_ready": team_ready,
                    "status": "ready" if team_ready else "observing",
                    "candidate_outcomes": [
                        dict(item)
                        for item in (prior or {}).get("candidate_outcomes") or []
                        if isinstance(item, dict)
                    ][-50:],
                }
            )
        patterns.sort(
            key=lambda item: (
                item["kind"],
                item["pattern_key"],
                item["pattern_id"],
            )
        )
        return patterns


def compact_pattern_context(
    patterns: list[dict[str, Any]],
    *,
    limit: int = 8,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Return prompt-sized ready patterns and their durable references."""
    ready = [
        item
        for item in patterns
        if isinstance(item, dict) and bool(item.get("team_ready"))
    ]
    ready.sort(
        key=lambda item: (
            int(item.get("occurrence_count") or 0),
            int(item.get("evidence_count") or 0),
        ),
        reverse=True,
    )
    context = [
        {
            key: item.get(key)
            for key in (
                "pattern_id",
                "pattern_key",
                "skill_name",
                "title",
                "kind",
                "problem",
                "root_cause",
                "guidance",
                "evidence_count",
                "occurrence_count",
                "session_count",
                "user_count",
                "experience_ids",
                "session_ids",
            )
        }
        for item in ready[: max(0, int(limit))]
    ]
    refs = [
        {
            "skill_name": str(item.get("skill_name") or ""),
            "pattern_id": str(item.get("pattern_id") or ""),
            "pattern_key": str(item.get("pattern_key") or ""),
        }
        for item in context
    ]
    return context, refs
