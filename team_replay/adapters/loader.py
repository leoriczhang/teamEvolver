"""Build a fixed tenant factory from immutable, owner supplied Python source."""
from __future__ import annotations

import ast
import copy
import hashlib
import re
from dataclasses import dataclass
from types import ModuleType


class AdapterError(ValueError):
    pass


def validate_content(content, filename):
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*\.py", filename):
        raise AdapterError("invalid Replay adapter filename")
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 256 * 1024:
        raise AdapterError("Replay adapter source must contain 1..262144 bytes")
    try:
        tree = ast.parse(content, filename=filename)
    except SyntaxError as exc:
        raise AdapterError(f"invalid Python at line {exc.lineno}") from exc
    factories = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_replay_adapter"]
    if len(factories) != 1:
        raise AdapterError("one build_replay_adapter(config) function is required")
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "REPLAY_ADAPTER" for t in node.targets
        ):
            try:
                meta = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise AdapterError("REPLAY_ADAPTER must be a literal dictionary") from exc
            if not isinstance(meta, dict) or type(meta.get("enabled", True)) is not bool:
                raise AdapterError("REPLAY_ADAPTER.enabled must be boolean")
            if not isinstance(meta.get("label", filename), str):
                raise AdapterError("REPLAY_ADAPTER.label must be text")
            return {"enabled": meta.get("enabled", True), "label": meta.get("label", filename)[:160]}
    raise AdapterError("REPLAY_ADAPTER metadata is required")


@dataclass(frozen=True)
class BoundFactory:
    factory: object
    filename: str
    revision: str

    def open(self, context):
        return self.factory.open(context)


def build_from_content(content, filename, config, *, tenant_id):
    metadata = validate_content(content, filename)
    if not metadata["enabled"]:
        raise AdapterError("Replay adapter is disabled")
    sha = hashlib.sha256(content.encode()).hexdigest()
    module = ModuleType(f"_replay_{tenant_id}_{sha}")
    exec(compile(content, filename, "exec"), module.__dict__)
    factory = module.build_replay_adapter(copy.deepcopy(config))
    if not callable(getattr(factory, "open", None)):
        raise AdapterError("factory must implement open(context)")
    return BoundFactory(factory, filename, sha)
