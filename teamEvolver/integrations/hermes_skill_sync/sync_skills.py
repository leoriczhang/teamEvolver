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
import json
import os
import select
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_MIN_INTERVAL_SECONDS = 60


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
        "team_api_key": "sharing_viking_team_api_key",
        "viking_team_api_key": "sharing_viking_team_api_key",
        "viking_account": "sharing_viking_account",
        "viking_user": "sharing_viking_user",
        "viking_agent": "sharing_viking_agent",
        "viking_agent_id": "sharing_viking_agent_id",
        "viking_customer_id": "sharing_viking_customer_id",
        "viking_root_prefix": "sharing_viking_root_prefix",
        "viking_group_id": "sharing_viking_group_id",
        "local_root": "sharing_local_root",
    }
    for source, target in mapping.items():
        if source in cfg and cfg[source] not in (None, ""):
            setattr(config, target, cfg[source])
    if any(
        cfg.get(key)
        for key in ("backend", "sharing_backend", "endpoint", "viking_endpoint", "viking_api_key", "team_api_key")
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
        os.environ.get("TEAMEVOLVER_SYNC_USER", "").strip()
        or os.environ.get("TEAMEVOLVER_USER", "").strip()
        or str(cfg.get("user_alias") or cfg.get("user") or "").strip()
    )


def _service_api_key(cfg: dict[str, Any]) -> str:
    return (
        os.environ.get("TEAMEVOLVER_SYNC_API_KEY", "").strip()
        or str(cfg.get("api_key") or cfg.get("service_api_key") or "").strip()
    )


def _safe_rel_path(raw: str) -> Path | None:
    clean = str(raw or "").strip().replace("\\", "/").lstrip("/")
    if not clean or clean.startswith("../") or "/../" in f"/{clean}":
        return None
    return Path(clean)


def _write_service_bundle(target_dir: Path, skill: dict[str, Any]) -> bool:
    name = str(skill.get("name") or "").strip()
    if not name or "/" in name or name in {".", ".."}:
        return False
    files = skill.get("files")
    if not isinstance(files, list) or not files:
        return False
    skill_dir = target_dir / name
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
    for item in files:
        if not isinstance(item, dict):
            continue
        rel = _safe_rel_path(str(item.get("path") or ""))
        if rel is None:
            continue
        try:
            content = base64.b64decode(str(item.get("content_b64") or ""), validate=True)
        except Exception:
            continue
        dest = skill_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    return (skill_dir / "SKILL.md").is_file()


def _pull_from_service(target_dir: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    base_url = _service_base_url(cfg)
    if not base_url:
        return {"status": "skipped", "reason": "missing_service_url"}
    params = {}
    user = _service_user(cfg)
    if user:
        params["user"] = user
    url = f"{base_url}/sync/skills"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    api_key = _service_api_key(cfg)
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"teamEvolver service returned HTTP {exc.code}: {body}") from exc

    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, list):
        raise RuntimeError("teamEvolver service response missing skills list")

    target_dir.mkdir(parents=True, exist_ok=True)
    remote_names = {str(item.get("name") or "").strip() for item in skills if isinstance(item, dict)}
    existing_names = {p.name for p in target_dir.iterdir() if p.is_dir()}
    downloaded = 0
    skipped = 0
    for skill in skills:
        if not isinstance(skill, dict):
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
    if backend_override in {"service", "teamEvolver", "http", "https"}:
        return _pull_from_service(target_dir, cfg)

    from teamEvolver.skills.hub import SkillHub

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
        return {"status": "skipped", "reason": "missing_team_api_key"}
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
