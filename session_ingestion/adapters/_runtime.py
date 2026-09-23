"""Resolve exactly one trusted adapter file for an authenticated tenant."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

MAX_CODE_BYTES = 256 * 1024
PERSISTENCE_WARNING = (
    "当前修改只写入本实例的运行目录，重新部署、替换容器或重新安装后可能丢失。"
    "验证通过后请联系项目 Owner，将适配器变更合入源码并重新发布。"
)


class AdapterError(ValueError):
    pass


class AdapterConflict(AdapterError):
    pass


class AdapterStorageError(AdapterError):
    pass


_cache: dict[Path, tuple[str, ModuleType]] = {}
_lock = threading.RLock()


def directory(config) -> Path:
    return (
        Path(config.datasource_adapters_dir).expanduser().resolve()
        if config.datasource_adapters_dir
        else Path(__file__).parent
    )


def _safe_filename(filename: str) -> str:
    if not filename or Path(filename).name != filename or not filename.endswith(".py") or filename.startswith("_"):
        raise AdapterError(
            "Select a tenant .py file in session_ingestion/adapters/"
        )
    return filename


def target_path(config, filename: str) -> Path:
    root = directory(config).resolve()
    path = root / _safe_filename(filename)
    if path.is_symlink() or path.resolve().parent != root:
        raise AdapterError(f"Invalid adapter path: {filename}")
    return path


def adapter_path(config, filename: str) -> Path:
    path = target_path(config, filename)
    if not path.is_file():
        raise AdapterError(f"Adapter file not found: {filename}")
    return path


def validate_content(content: str, filename: str) -> dict:
    _safe_filename(filename)
    if not isinstance(content, str) or not content.strip():
        raise AdapterError("Adapter content cannot be empty")
    if len(content.encode("utf-8")) > MAX_CODE_BYTES:
        raise AdapterError("Adapter source exceeds 256 KiB")
    tree = ast.parse(content, filename=filename)
    if not any(isinstance(n, ast.FunctionDef) and n.name == "build_adapter" for n in tree.body):
        raise AdapterError(f"{filename}: build_adapter() is required")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SOURCE" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (TypeError, ValueError) as exc:
                raise AdapterError("SOURCE must be a literal dictionary") from exc
            if not isinstance(value, dict):
                break
            if not isinstance(value.get("enabled", True), bool):
                raise AdapterError("SOURCE.enabled must be boolean")
            for field in ("supported_filters", "required_filters"):
                items = value.get(field, [])
                if not isinstance(items, list) or not all(isinstance(k, str) for k in items):
                    raise AdapterError(f"SOURCE.{field} must be a list of strings")
            patterns = value.get("exclude_session_id_patterns", [])
            if not isinstance(patterns, list) or not all(
                isinstance(pattern, str) and pattern for pattern in patterns
            ):
                raise AdapterError(
                    "SOURCE.exclude_session_id_patterns must be a list of non-empty strings"
                )
            if set(value.get("required_filters", [])) - set(value.get("supported_filters", [])):
                raise AdapterError("Required filters must be supported")
            cap = value.get("max_sessions", 100)
            if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 1000:
                raise AdapterError("SOURCE.max_sessions must be between 1 and 1000")
            # Explicit display fields only. Never expose arbitrary options or credentials.
            return {
                key: value[key]
                for key in (
                    "label",
                    "provider",
                    "host",
                    "project_id",
                    "enabled",
                    "supported_filters",
                    "required_filters",
                    "exclude_session_id_patterns",
                    "max_sessions",
                )
                if key in value
            }
    raise AdapterError(f"{filename}: missing literal SOURCE metadata")


def metadata(path: Path) -> dict:
    return validate_content(path.read_text(encoding="utf-8"), path.name)


def available(config) -> list[dict]:
    entries = []
    for path in sorted(directory(config).glob("*.py")):
        if path.name.startswith("_") or path.is_symlink():
            continue
        try:
            entries.append({"file": path.name, **metadata(path)})
        except (ValueError, SyntaxError) as exc:
            entries.append({"file": path.name, "error": str(exc)})
    return entries


def binding(config, tenant) -> str:
    # A non-default tenant must NEVER inherit the default tenant's binding.
    if tenant is not None and not tenant.is_default():
        return str((tenant.config_overrides or {}).get("datasource_adapter") or "")
    return str(getattr(config, "datasource_adapter", "") or "")


def describe(config, tenant) -> dict:
    filename = binding(config, tenant)
    result = {
        "tenant_id": tenant.tenant_id if tenant else "default",
        "file": filename,
        "configured": False,
        "enabled": False,
    }
    if not filename:
        result["error"] = "No adapter bound to this tenant"
        return result
    try:
        path = adapter_path(config, filename)
        source = metadata(path)
        result.update(source)
        result.update(configured=True, enabled=source.get("enabled", True))
        result["revision"] = hashlib.sha256(path.read_bytes()).hexdigest()
    except (ValueError, SyntaxError) as exc:
        result["error"] = str(exc)
    return result


def _build_adapter(content: str, filename: str):
    source = validate_content(content, filename)
    revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    with _lock:
        module_key = f"{filename}\0{revision}".encode("utf-8")
        module_name = f"tenant_adapter_{hashlib.sha256(module_key).hexdigest()}"
        module = ModuleType(module_name)
        module.__file__ = filename
        previous = sys.modules.get(module_name)
        sys.modules[module_name] = module
        try:
            exec(compile(content, filename, "exec"), module.__dict__)
        except BaseException:
            if previous is None:
                sys.modules.pop(module_name, None)
            else:
                sys.modules[module_name] = previous
            raise
    factory = getattr(module, "build_adapter", None)
    if not callable(factory):
        raise AdapterError(f"{filename}: build_adapter() is required")
    adapter = factory()
    for method in ("list_session_ids", "fetch_session", "convert_session", "health", "close"):
        if not callable(getattr(adapter, method, None)):
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
            raise AdapterError(f"{filename}: {method}() is required")
    return adapter, source, revision, module


def load_from_content(content: str, filename: str):
    adapter, source, revision, _module = _build_adapter(content, filename)
    return adapter, source, revision


def inspect_content(content: str, filename: str) -> dict:
    adapter, source, revision = load_from_content(content, filename)
    try:
        return {**source, "file": filename, "revision": revision}
    finally:
        adapter.close()


def load(config, tenant, *, revision=None):
    descriptor = describe(config, tenant)
    if not descriptor["configured"] or not descriptor["enabled"]:
        raise AdapterError(descriptor.get("error") or "Tenant adapter is disabled")
    path = adapter_path(config, descriptor["file"])
    content = path.read_text(encoding="utf-8")
    expected = revision or descriptor["revision"]
    current_revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if current_revision != expected:
        raise AdapterConflict("Adapter changed during the operation; reload and retry")
    with _lock:
        cached = _cache.get(path)
        if cached and cached[0] == current_revision:
            module = cached[1]
        else:
            adapter, _source, _revision, module = _build_adapter(content, path.name)
            adapter.close()
            _cache[path] = (current_revision, module)
    adapter = module.build_adapter()
    return adapter


def read_content(config, filename: str) -> dict:
    path = adapter_path(config, filename)
    content = path.read_text(encoding="utf-8")
    return {
        "file": filename,
        "code": content,
        "revision": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "persistence": {"durable": False, "mode": "runtime_only", "warning": PERSISTENCE_WARNING},
    }


def save_content(config, filename: str, content: str, expected_revision: str | None) -> dict:
    path = target_path(config, filename)
    inspected = inspect_content(content, filename)
    root = path.parent
    if not root.is_dir():
        raise AdapterStorageError(f"Adapter directory does not exist: {root}")
    with _lock:
        if path.exists():
            current = hashlib.sha256(path.read_bytes()).hexdigest()
            if expected_revision is None:
                raise AdapterConflict(f"Adapter already exists: {filename}")
            if current != expected_revision:
                raise AdapterConflict("Adapter changed after it was opened; reload before saving")
            mode = path.stat().st_mode & 0o777
        else:
            if expected_revision not in (None, ""):
                raise AdapterConflict("Adapter no longer exists; reload before saving")
            mode = 0o640
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".tmp", dir=root)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            except BaseException:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
                raise
        except OSError as exc:
            raise AdapterStorageError(
                f"Runtime adapter directory is not writable; contact the project Owner: {exc}"
            ) from exc
        _cache.pop(path, None)
    return {
        **inspected,
        "revision": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "persistence": {"durable": False, "mode": "runtime_only", "warning": PERSISTENCE_WARNING},
    }


def filters_for(descriptor: dict, body: dict) -> tuple[dict, int]:
    supported = set(descriptor.get("supported_filters") or [])
    controls = {"max_sessions", "force_reprocess", "defer_evolution_trigger"}
    unknown = set(body) - supported - controls
    if unknown:
        raise AdapterError(f"Unsupported fields: {', '.join(sorted(unknown))}")
    filters = {key: value for key, value in body.items() if key in supported and value not in (None, "")}
    missing = [key for key in descriptor.get("required_filters", []) if not filters.get(key)]
    if missing:
        raise AdapterError(f"Required filters: {', '.join(missing)}")
    cap = body.get("max_sessions", descriptor.get("max_sessions", 100))
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= 1000:
        raise AdapterError("max_sessions must be between 1 and 1000")
    for key in ("force_reprocess", "defer_evolution_trigger"):
        if key in body and not isinstance(body[key], bool):
            raise AdapterError(f"{key} must be boolean")
    return filters, cap


def apply_session_id_exclusions(
    descriptor: dict,
    filters: dict,
    items: list,
) -> tuple[list, list[dict[str, str]]]:
    """Apply adapter-owned glob exclusions while preserving explicit lookups."""
    values = list(items)
    if str(filters.get("session_id") or "").strip():
        return values, []
    patterns = descriptor.get("exclude_session_id_patterns") or []
    if not patterns:
        return values, []

    included = []
    excluded = []
    for item in values:
        session_id = str(
            (item.get("session_id") or "") if isinstance(item, dict) else item
        ).strip()
        matched = next(
            (
                pattern
                for pattern in patterns
                if fnmatch.fnmatchcase(session_id, pattern)
            ),
            "",
        )
        if not matched:
            included.append(item)
            continue
        excluded.append(
            {
                "session_id": session_id,
                "status": "filtered",
                "reason": f"excluded_session_id_pattern:{matched}",
            }
        )
    return included, excluded


def probe(config, tenant) -> dict[str, Any]:
    adapter = load(config, tenant)
    try:
        result = adapter.health()
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise AdapterError("health() must return {ok: bool}")
        return result
    finally:
        adapter.close()


def test_content(content: str, filename: str, mode: str, body: dict) -> dict:
    adapter, source, revision = load_from_content(content, filename)
    try:
        base = {"ok": True, "source": "editor", "file": filename, "revision": revision, "metadata": source}
        if mode == "validate":
            return base
        if mode == "health":
            result = adapter.health()
            if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                raise AdapterError("health() must return {ok: bool}")
            return {**base, "health": result, "ok": result["ok"]}
        if mode == "preview":
            filters, cap = filters_for(source, body)
            rows = (
                adapter.preview_sessions(filters, max_sessions=cap)
                if callable(getattr(adapter, "preview_sessions", None))
                else [{"session_id": sid} for sid in adapter.list_session_ids(filters, max_sessions=cap)]
            )
            if not isinstance(rows, list) or len(rows) > cap:
                raise AdapterError("Adapter exceeded the preview limit")
            total = len(rows)
            rows, excluded = apply_session_id_exclusions(source, filters, rows)
            return {
                **base,
                "filters": filters,
                "total": total,
                "count": len(rows),
                "counts": {"included": len(rows), "filtered": len(excluded)},
                "sessions": rows,
            }
        if mode == "session":
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                raise AdapterError("session_id is required for conversion testing")
            raw, traces = adapter.fetch_session(session_id)
            session = adapter.convert_session(raw, traces)
            if not isinstance(session, dict) or not isinstance(session.get("turns"), list):
                raise AdapterError("Adapter must return a Session with turns")
            return {**base, "session": session, "trace_count": len(traces or [])}
        raise AdapterError("mode must be validate, health, preview, or session")
    finally:
        adapter.close()


def preview(config, tenant, body: dict) -> dict:
    descriptor = describe(config, tenant)
    filters, cap = filters_for(descriptor, body)
    adapter = (
        load(config, tenant, revision=descriptor["revision"]) if descriptor["configured"] else load(config, tenant)
    )
    try:
        if callable(getattr(adapter, "preview_sessions", None)):
            rows = adapter.preview_sessions(filters, max_sessions=cap)
        else:
            rows = [{"session_id": sid} for sid in adapter.list_session_ids(filters, max_sessions=cap)]
        if not isinstance(rows, list) or len(rows) > cap:
            raise AdapterError("Adapter exceeded the preview limit")
        total = len(rows)
        rows, excluded = apply_session_id_exclusions(descriptor, filters, rows)
        return {
            "filters": filters,
            "total": total,
            "count": len(rows),
            "counts": {"included": len(rows), "filtered": len(excluded)},
            "sessions": rows,
        }
    finally:
        adapter.close()
