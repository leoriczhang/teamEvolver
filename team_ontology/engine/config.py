"""项目配置：领域、LLM 接入、阈值与评审映射。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

DEFAULT_PROJECT_DIR_NAME = ".ontology-enhancer"


class LLMConfig(BaseModel):
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout_seconds: int = 180
    self_consistency_samples: int = Field(default=3, ge=1, le=7)
    # 某些兼容端点不支持 response_format json_schema，自动降级为 prompt 约束 + 解析
    strict_json: bool = True


class Thresholds(BaseModel):
    max_concepts: int = Field(default=40, ge=1, le=50)  # 对齐 PackConfig 上限
    min_term_frequency: int = Field(default=2, ge=1)
    term_candidates: int = Field(default=120, ge=10, le=500)
    chunk_max_chars: int = Field(default=2000, ge=256, le=12000)
    chunk_overlap_chars: int = Field(default=200, ge=0, le=2000)
    terms_per_llm_batch: int = Field(default=25, ge=5, le=80)
    concepts_per_llm_batch: int = Field(default=20, ge=5, le=60)
    taxonomy_self_consistency: int = Field(default=3, ge=1, le=7)


class ProjectConfig(BaseModel):
    pack_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    pack_name: str = Field(min_length=1, max_length=255)
    domain: str = Field(min_length=1, max_length=128)
    version: str = Field(default="0.1.0", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    description: str = ""
    languages: list[str] = Field(default_factory=lambda: ["zh"])
    llm: LLMConfig = Field(default_factory=LLMConfig)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    # 风险聚合 -> 评审级别
    review_level_by_risk: dict[str, str] = Field(
        default_factory=lambda: {"high": "committee", "medium": "checkpoint", "low": "none"}
    )
    tool_catalog_path: str | None = None

    @model_validator(mode="after")
    def check_review_mapping(self) -> ProjectConfig:
        for level in self.review_level_by_risk.values():
            if level not in {"none", "checkpoint", "committee"}:
                raise ValueError(f"invalid review level: {level}")
        return self


def default_project_dir() -> Path:
    return Path.cwd() / DEFAULT_PROJECT_DIR_NAME


def load_config(path: Path) -> ProjectConfig:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with open(path, "rb") as fh:
        return ProjectConfig.model_validate(tomllib.load(fh))


def dump_config(config: ProjectConfig, path: Path) -> None:
    import tomli_w  # type: ignore[import-not-found]

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        tomli_w.dump(_tomlable(config.model_copy(update={"llm": config.llm.model_copy(update={"api_key": ""})})), fh)


def _tomlable(model: BaseModel) -> dict[str, Any]:
    return {
        key: (value if not isinstance(value, BaseModel) else _tomlable(value))
        for key, value in model.model_dump().items()
        if value is not None
    }
