"""Load an explicitly bound, deployment-owner-managed Python Replay factory."""

from __future__ import annotations

import ast
import copy
import hashlib
import os
import re
import sys
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from .hooks import ReplayAdapterFactory

MAX_CODE_BYTES = 256 * 1024
_lock = threading.RLock()
_modules: OrderedDict[tuple[str, str, str], ModuleType] = OrderedDict()


class AdapterError(ValueError):
    pass


class AdapterConflict(AdapterError):
    pass


@dataclass(frozen=True)
class BoundFactory:
    factory: ReplayAdapterFactory
    filename: str
    revision: str

    def open(self, context):
        return self.factory.open(context)


def directory(config: Any) -> Path:
    configured = str(getattr(config, "replay_adapters_dir", "") or "")
    return Path(configured).expanduser().resolve() if configured else Path(__file__).parent / "customer_adapters"


def _filename(filename: str) -> str:
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.py", filename):
        raise AdapterError("Select a .py file directly inside replay.adapters_dir")
    return filename


def _path(config: Any, filename: str) -> Path:
    root = directory(config).resolve()
    path = root / _filename(filename)
    if path.is_symlink() or path.resolve().parent != root:
        raise AdapterError("Adapter path must not be a symlink or leave replay.adapters_dir")
    return path


def validate_content(content: str, filename: str) -> dict[str, Any]:
    """Check syntax/metadata only; this is not a security sandbox."""
    _filename(filename)
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > MAX_CODE_BYTES:
        raise AdapterError("Replay adapter source must contain 1..262144 bytes")
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as exc:
        raise AdapterError(f"invalid Python at line {exc.lineno}: {exc.msg}") from exc
    factories = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "build_replay_adapter"]
    if len(factories) != 1:
        raise AdapterError("Exactly one build_replay_adapter(config) function is required")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "REPLAY_ADAPTER" for target in node.targets
        ):
            try:
                metadata = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise AdapterError("REPLAY_ADAPTER must be a literal dictionary") from exc
            if not isinstance(metadata, dict) or not isinstance(metadata.get("enabled", True), bool):
                raise AdapterError("REPLAY_ADAPTER.enabled must be boolean")
            label = metadata.get("label", filename)
            if not isinstance(label, str):
                raise AdapterError("REPLAY_ADAPTER.label must be a string")
            return {"label": label[:160], "enabled": metadata.get("enabled", True)}
    raise AdapterError("REPLAY_ADAPTER metadata is required")


def available(config: Any) -> list[dict[str, Any]]:
    result = []
    for path in sorted(directory(config).glob("*.py")):
        if path.is_symlink() or path.name.startswith("_"):
            continue
        try:
            result.append({"file": path.name, **validate_content(path.read_text("utf-8"), path.name)})
        except (OSError, ValueError) as exc:
            result.append({"file": path.name, "enabled": False, "error": str(exc)})
    return result


def binding(config: Any, tenant: Any) -> str:
    if tenant is not None and tenant.tenant_id != "default":
        return str((tenant.config_overrides or {}).get("replay_adapter") or "")
    return str(getattr(config, "replay_adapter", "") or "")


def read_content(config: Any, filename: str) -> dict[str, Any]:
    try:
        content = _path(config, filename).read_text("utf-8")
    except OSError as exc:
        raise AdapterError(f"Adapter file unavailable: {filename}") from exc
    return {"file": filename, "code": content, "revision": hashlib.sha256(content.encode()).hexdigest()}


def describe(config: Any, tenant: Any) -> dict[str, Any]:
    filename = binding(config, tenant)
    result = {
        "tenant_id": tenant.tenant_id if tenant else "default", "file": filename,
        "configured": False, "enabled": False,
    }
    if not filename:
        return {**result, "error": "No Replay adapter bound to this tenant"}
    try:
        source = read_content(config, filename)
        result.update(validate_content(source["code"], filename), configured=True, revision=source["revision"])
    except AdapterError as exc:
        result["error"] = str(exc)
    return result


def build_from_content(content: str, filename: str, config: Any, *, tenant_id: str) -> ReplayAdapterFactory:
    metadata = validate_content(content, filename)
    if not metadata["enabled"]:
        raise AdapterError("Replay adapter is disabled")
    revision = hashlib.sha256(content.encode()).hexdigest()
    key = (tenant_id, filename, revision)
    with _lock:
        module = _modules.get(key)
        if module is None:
            name = "_replay_adapter_" + hashlib.sha256(repr(key).encode()).hexdigest()
            module = ModuleType(name)
            module.__file__ = filename
            sys.modules[name] = module
            try:
                exec(compile(content, filename, "exec"), module.__dict__)
            except BaseException:
                sys.modules.pop(name, None)
                raise
            _modules[key] = module
            while len(_modules) > 32:
                _, expired = _modules.popitem(last=False)
                sys.modules.pop(expired.__name__, None)
        _modules.move_to_end(key)
    # Factories are never shared between runs, branches or tenants.
    factory = module.build_replay_adapter(copy.deepcopy(config))
    if not callable(getattr(factory, "open", None)):
        raise AdapterError("build_replay_adapter(config) must return a factory with open(context)")
    return BoundFactory(factory, filename, revision)


def load_factory(config: Any, tenant: Any, revision: str | None = None) -> ReplayAdapterFactory:
    filename = binding(config, tenant)
    if not filename:
        raise AdapterError("No Replay adapter bound to this tenant")
    source = read_content(config, filename)
    if revision is not None and source["revision"] != revision:
        raise AdapterConflict("Replay adapter changed during the run; reload before retrying")
    return build_from_content(
        source["code"], filename, config, tenant_id=tenant.tenant_id if tenant else "default",
    )


def save_content(
    config: Any, filename: str, content: str, expected_revision: str | None = None,
) -> dict[str, Any]:
    metadata = validate_content(content, filename)
    path = _path(config, filename)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        if current != (expected_revision or None):
            raise AdapterConflict("Replay adapter changed after it was opened; reload before saving")
        fd, temporary = tempfile.mkstemp(prefix=f".{filename}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "file": filename, **metadata, "revision": hashlib.sha256(content.encode()).hexdigest(),
        "persistence": {"mode": "deployment_directory", "requires_release": True},
    }
