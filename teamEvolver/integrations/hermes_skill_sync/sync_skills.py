#!/usr/bin/env python3
"""pre_llm_call hook: pull team teamEvolver skills into Hermes.

The hook does not proxy model traffic and does not inject context. It only keeps
a local team-skill directory fresh so Hermes' native ``skills_list`` and
``skill_view`` tools can discover team skills through ``skills.external_dirs``.

Configuration precedence:

1. explicit env vars (``TEAMEVOLVER_SYNC_*``),
2. ``sync.json`` next to this script,
3. local ``~/.teamEvolver/config.yaml`` if the ``teamEvolver`` package is installed.

If sharing credentials are not configured the hook exits successfully and
silently, matching Hermes shell-hook expectations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MIN_INTERVAL_SECONDS = 15


def _log(message: str) -> None:
    print(f"[teamEvolver-sync] {message}", file=sys.stderr)


def _config_path() -> Path:
    override = os.environ.get("TEAMEVOLVER_SYNC_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().with_name("sync.json")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text("utf-8") or "{}")
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _hermes_home(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("HERMES_HOME", "").strip() or str(cfg.get("hermes_home") or "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".hermes"


def _target_dir(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("TEAMEVOLVER_SYNC_TARGET_DIR", "").strip() or str(cfg.get("target_dir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _hermes_home(cfg) / "team_skills" / "teamEvolver"


def _lock_path(target_dir: Path) -> Path:
    return target_dir.parent / ".teamEvolver-sync.lock"


def _stamp_path(target_dir: Path) -> Path:
    return target_dir.parent / ".teamEvolver-sync.stamp"


def _interval_seconds(cfg: dict[str, Any]) -> int:
    raw = os.environ.get("TEAMEVOLVER_SYNC_MIN_INTERVAL_SECONDS", "") or cfg.get("min_interval_seconds")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MIN_INTERVAL_SECONDS


def _should_skip(target_dir: Path, cfg: dict[str, Any]) -> bool:
    interval = _interval_seconds(cfg)
    if interval <= 0:
        return False
    stamp = _stamp_path(target_dir)
    try:
        age = time.time() - stamp.stat().st_mtime
    except OSError:
        return False
    return age < interval


def _touch_stamp(target_dir: Path) -> None:
    stamp = _stamp_path(target_dir)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()


def _try_lock(target_dir: Path):
    lock = _lock_path(target_dir)
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = lock.open("a+", encoding="utf-8")
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
    except ImportError:
        pass
    return handle


def _load_teamEvolver_config():
    from teamEvolver.config_store import ConfigStore

    return ConfigStore().to_config()


def _apply_overrides(config, cfg: dict[str, Any]):
    # Keep names aligned with TeamEvolverConfig fields, but accept a small set of
    # installer-friendly aliases for remote machines that do not have a full
    # ~/.teamEvolver/config.yaml yet.
    mapping = {
        "sharing_enabled": "sharing_enabled",
        "backend": "sharing_backend",
        "sharing_backend": "sharing_backend",
        "endpoint": "sharing_viking_endpoint",
        "viking_endpoint": "sharing_viking_endpoint",
        "viking_api_key": "sharing_viking_api_key",
        "service_api_key": "sharing_viking_team_api_key",
        "team_api_key": "sharing_viking_team_api_key",
        "viking_team_api_key": "sharing_viking_team_api_key",
        "viking_account": "sharing_viking_account",
        "viking_user": "sharing_viking_user",
        "viking_agent": "sharing_viking_agent",
        "viking_agent_id": "sharing_viking_agent_id",
        "viking_customer_id": "sharing_viking_customer_id",
        "viking_root_prefix": "sharing_viking_root_prefix",
        "viking_group_id": "sharing_viking_group_id",
        "viking_deployment": "sharing_viking_deployment",
    }
    for source, target in mapping.items():
        if source in cfg and cfg[source] not in (None, ""):
            setattr(config, target, cfg[source])
    # Derive the effective OpenViking endpoint from the deployment mode when no
    # explicit endpoint was provided (cloud vs local self-hosted server).
    if not str(getattr(config, "sharing_viking_endpoint", "") or "").strip():
        from teamEvolver.config import resolve_viking_endpoint

        config.sharing_viking_endpoint = resolve_viking_endpoint(
            str(getattr(config, "sharing_viking_deployment", "") or "cloud")
        )
    if any(
        cfg.get(key)
        for key in (
            "backend",
            "sharing_backend",
            "endpoint",
            "viking_endpoint",
            "viking_api_key",
            "service_api_key",
            "team_api_key",
        )
    ):
        config.sharing_enabled = bool(cfg.get("sharing_enabled", True))
    return config


def _service_base_url(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("TEAMEVOLVER_SYNC_URL", "").strip()
        or os.environ.get("TEAMEVOLVER_URL", "").strip()
        or str(cfg.get("base_url") or cfg.get("service_url") or cfg.get("url") or "").strip()
    ).rstrip("/")


def _service_user(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("TEAMEVOLVER_USER_ID", "").strip()
        or os.environ.get("TEAMEVOLVER_SYNC_USER", "").strip()
        or os.environ.get("TEAMEVOLVER_USER", "").strip()
        or str(cfg.get("user_id") or cfg.get("user_alias") or cfg.get("user") or "").strip()
    )


def _service_api_key(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("TEAMEVOLVER_TENANT_TOKEN", "").strip()
        or os.environ.get("TEAMEVOLVER_SYNC_API_KEY", "").strip()
        or str(cfg.get("tenant_token") or cfg.get("api_key") or "").strip()
    )


def _safe_rel_path(raw: str) -> Path | None:
    clean = str(raw or "").strip().replace("\\", "/")
    if (
        not clean or clean.startswith("/") or ":" in clean or "\0" in clean
        or any(part in {"", ".", ".."} for part in clean.split("/"))
    ):
        return None
    return Path(clean)


def _tree_sha256(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, content in sorted(files.items()):
        digest.update(path.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii") + b"\0")
        digest.update(str(len(content)).encode("ascii") + b"\n")
    return digest.hexdigest()


def _bundle_files(skill: dict[str, Any]) -> dict[str, bytes]:
    name = str(skill.get("name") or "")
    if _safe_rel_path(name) is None or "/" in name or "\\" in name:
        raise ValueError("unsafe Skill name")
    files = {}
    for item in skill.get("files") or []:
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel is None or rel.as_posix() in files:
            raise ValueError("unsafe or duplicate Skill file")
        files[rel.as_posix()] = base64.b64decode(str(item.get("content_b64") or ""), validate=True)
    if "SKILL.md" not in files:
        raise ValueError("Skill bundle missing SKILL.md")
    if hashlib.sha256(files["SKILL.md"]).hexdigest() != skill.get("sha256"):
        raise ValueError("Skill checksum mismatch")
    if _tree_sha256(files) != skill.get("tree_sha256"):
        raise ValueError("Skill tree checksum mismatch")
    return files


def _local_matches(target_dir: Path, name: str, fingerprint: dict[str, Any]) -> bool:
    if _safe_rel_path(name) is None or "/" in name or "\\" in name:
        return False
    root = target_dir / name
    if root.is_symlink() or not (root / "SKILL.md").is_file():
        return False
    try:
        paths = list(root.rglob("*"))
        if any(path.is_symlink() for path in paths):
            return False
        files = {path.relative_to(root).as_posix(): path.read_bytes() for path in paths if path.is_file()}
        return _tree_sha256(files) == fingerprint.get("tree_sha256")
    except OSError:
        return False


def _write_service_bundle(target_dir: Path, skill: dict[str, Any]) -> bool:
    files = _bundle_files(skill)
    skill_dir = target_dir / str(skill["name"])
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".teamEvolver-bundle-", dir=target_dir.parent) as staging:
        stage = Path(staging)
        ready, previous = stage / "ready", stage / "previous"
        for rel, content in files.items():
            dest = ready / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
        if skill_dir.exists() or skill_dir.is_symlink():
            skill_dir.rename(previous)
        try:
            ready.rename(skill_dir)
        except OSError:
            if previous.exists() or previous.is_symlink():
                previous.rename(skill_dir)
            raise
    return True


def _pull_from_service(target_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    base_url = _service_base_url(cfg)
    if not base_url:
        return {"status": "skipped", "reason": "missing_service_url"}
    user = _service_user(cfg)
    api_key = _service_api_key(cfg)
    if not user:
        return {"status": "skipped", "reason": "missing_user_id"}
    if not api_key.startswith("tevt_"):
        return {"status": "skipped", "reason": "missing_tenant_token"}
    url = f"{base_url}/sync/skills?" + urllib.parse.urlencode({"user_id": user})
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    request.add_header("Authorization", f"Bearer {api_key}")
    cache_path = target_dir.parent / ".teamEvolver-sync.cache.json"
    scope = hashlib.sha256(json.dumps([base_url, user, api_key]).encode("utf-8")).hexdigest()
    cache = _load_json(cache_path)
    if cache.get("scope") != scope:
        cache = {}
    cached_skills = cache.get("skills") or {}
    if cache.get("etag") and target_dir.is_dir() and all(
        _local_matches(target_dir, name, fingerprint) for name, fingerprint in cached_skills.items()
    ):
        request.add_header("If-None-Match", cache["etag"])
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            etag = response.headers.get("ETag", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return {"status": "ok", "downloaded": 0, "skipped": len(cached_skills), "deleted": 0, "backend": "service"}
        body = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"teamEvolver service returned HTTP {exc.code}: {body}") from exc

    skills = payload.get("skills") if isinstance(payload, dict) else None
    if payload.get("status") != "ok" or not isinstance(skills, list):
        raise RuntimeError("teamEvolver service response missing skills list")

    # Validate the entire snapshot before changing any existing Skill.
    for skill in skills:
        _bundle_files(skill)
    if len({item["name"] for item in skills}) != len(skills):
        raise ValueError("duplicate Skill names")
    target_dir.mkdir(parents=True, exist_ok=True)
    remote_names = {str(item.get("name") or "").strip() for item in skills if isinstance(item, dict)}
    existing_names = {p.name for p in target_dir.iterdir() if p.is_dir()}
    downloaded = 0
    skipped = 0
    fingerprints = {}
    for skill in skills:
        name = skill["name"]
        fingerprint = {key: skill.get(key) for key in ("version", "sha256", "tree_sha256")}
        fingerprints[name] = fingerprint
        if cached_skills.get(name) == fingerprint and _local_matches(target_dir, name, fingerprint):
            skipped += 1
            continue
        if _write_service_bundle(target_dir, skill):
            downloaded += 1
        else:
            skipped += 1
    deleted = 0
    if bool(cfg.get("mirror", False)):
        for name in sorted(existing_names - remote_names):
            shutil.rmtree(target_dir / name, ignore_errors=True)
            deleted += 1
    temporary = cache_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"scope": scope, "etag": etag, "skills": fingerprints}), "utf-8")
    os.replace(temporary, cache_path)
    return {
        "status": "ok",
        "downloaded": downloaded,
        "skipped": skipped,
        "deleted": deleted,
        "total_remote": len(skills),
        "backend": "service",
    }


def _pull(target_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    backend_override = str(
        os.environ.get("TEAMEVOLVER_SYNC_BACKEND", "") or cfg.get("backend") or cfg.get("sharing_backend") or ""
    ).strip().lower()
    if backend_override in {"", "service", "teamevolver", "http", "https"}:
        return _pull_from_service(target_dir, cfg)

    from team_skills.library.hub import SkillHub

    config = _apply_overrides(_load_teamEvolver_config(), cfg)
    if not getattr(config, "sharing_enabled", False):
        return {"status": "skipped", "reason": "sharing_disabled"}
    backend = str(getattr(config, "sharing_backend", "") or "").strip().lower()
    if not backend and getattr(config, "sharing_viking_endpoint", ""):
        backend = "viking"
        config.sharing_backend = "viking"
    if backend == "viking" and not (
        getattr(config, "sharing_viking_team_api_key", "") or getattr(config, "sharing_viking_api_key", "")
    ):
        return {"status": "skipped", "reason": "missing_service_api_key"}
    hub = SkillHub.team_from_config(config)
    result = hub.pull_skills(str(target_dir), mirror=bool(cfg.get("mirror", False)))
    return {"status": "ok", **result}


def main() -> int:
    # Consume stdin so Hermes can pipe the hook payload without blocking. The
    # current implementation does not need fields from the payload.
    try:
        if select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.read()
    except Exception:
        pass

    cfg = _load_json(_config_path())
    target = _target_dir(cfg)
    if _should_skip(target, cfg):
        print(json.dumps({"action": "allow", "status": "skipped", "reason": "interval"}))
        return 0
    lock = _try_lock(target)
    if lock is None:
        print(json.dumps({"action": "allow", "status": "skipped", "reason": "locked"}))
        return 0
    try:
        try:
            result = _pull(target, cfg)
            if result.get("status") == "ok":
                _touch_stamp(target)
            else:
                _log(f"skipped: {result.get('reason', 'unknown')}")
        except Exception as exc:  # noqa: BLE001 - hooks must not fail Hermes turns
            result = {"status": "error", "error": str(exc)}
            _log(f"sync failed: {exc}")
        print(json.dumps({"action": "allow", **result}, ensure_ascii=False))
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
