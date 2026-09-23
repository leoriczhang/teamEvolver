"""项目上下文：.ontology-enhancer/ 目录布局与工件读写。"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..config import DEFAULT_PROJECT_DIR_NAME, ProjectConfig, dump_config, load_config

ARTIFACTS = [
    "chunks",
    "terms",
    "concepts",
    "taxonomy_edges",
    "relations",
    "closed_values",
    "iron_law_constraints",
    "tool_constraints",
    "workflows",
    "runs",
]


class ProjectContext:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    @property
    def packs_dir(self) -> Path:
        return self.root / "packs"

    @property
    def dist_dir(self) -> Path:
        return self.root / "dist"

    @property
    def decisions_path(self) -> Path:
        return self.root / "decisions.jsonl"

    # ---- 初始化 / 发现 ----

    @classmethod
    def create(cls, config: ProjectConfig, *, root: Path | None = None) -> ProjectContext:
        context = cls(root or Path.cwd() / DEFAULT_PROJECT_DIR_NAME)
        context.root.mkdir(parents=True, exist_ok=True)
        for sub in ("artifacts", "packs", "dist"):
            (context.root / sub).mkdir(exist_ok=True)
        dump_config(config, context.config_path)
        return context

    @classmethod
    def discover(cls, start: Path | None = None) -> ProjectContext:
        current = (start or Path.cwd()).resolve()
        for candidate in [current, *current.parents]:
            marker = candidate / DEFAULT_PROJECT_DIR_NAME
            if marker.is_dir() and (marker / "config.toml").is_file():
                return cls(marker)
        raise FileNotFoundError(f"未找到项目（{DEFAULT_PROJECT_DIR_NAME}/config.toml），请先运行 `oe init`")

    def load_config(self) -> ProjectConfig:
        return load_config(self.config_path)

    # ---- 工件 ----

    def artifact_path(self, name: str) -> Path:
        if name not in ARTIFACTS:
            raise ValueError(f"unknown artifact: {name}")
        return self.artifacts_dir / f"{name}.jsonl"

    def write_jsonl(self, name: str, items: Iterable[dict]) -> Path:
        path = self.artifact_path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")

        def _default(obj: Any) -> Any:
            if isinstance(obj, BaseModel):
                return obj.model_dump()
            return str(obj)

        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(item, ensure_ascii=False, default=_default) + "\n" for item in items)
        tmp.replace(path)
        return path

    def read_jsonl(self, name: str) -> list[dict[str, Any]]:
        path = self.artifact_path(name)
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def has_artifact(self, name: str) -> bool:
        return self.artifact_path(name).exists()

    # ---- pack ----

    def pack_dir(self, pack_id: str) -> Path:
        return self.packs_dir / pack_id

    def annotated_path(self, pack_id: str) -> Path:
        return self.pack_dir(pack_id) / "annotated.json"

    def published_dir(self, pack_id: str) -> Path:
        return self.pack_dir(pack_id) / "published"
