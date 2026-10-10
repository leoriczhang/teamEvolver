"""One TE-owned configuration surface; no separate runtime deployment."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CompileDeadlineConfig:
    """Adaptive compile wall clock; constants and rationale in
    docs/superpowers/specs/2026-09-28-adaptive-compile-deadline-design.md §3.2/§4."""

    base_seconds: int = 1800
    wave_seconds: int = 4500
    merge_wave_seconds: int = 1800
    safety_factor: float = 1.5


@dataclass
class OntologyConfig:
    enabled: bool = False
    state_dir: str = ""
    # Deprecated YAML compatibility only; publication uses native trusted authentication.
    signing_key_file: str = ""
    signing_key_id: str = "te-sit-v1"
    # Explicit legacy adapters remain testable; native R6 installations retire new OV-owned builds.
    kee_legacy_writes_enabled: bool = True
    kee_query_enabled: bool = False
    kee_max_model_calls: int = 64
    kee_compile_deadline_seconds: int = 480
    queue_limit: int = 32
    allow_fixture: bool = False
    model: dict = field(default_factory=dict)
    compile_skill_uri: str = "viking://agent/skills/ontology-extraction-v1"
    compile_workspace_uri: str = "viking://resources/te_ontology_compile"
    compile_user: str = "te-ontology-compiler"
    compile_deadline: CompileDeadlineConfig = field(default_factory=CompileDeadlineConfig)
    # Operator bumps this when OV's model/provider configuration changes. Empty disables cross-job reuse.
    compile_model_revision: str = ""
    source_max_files: int = 1000
    source_max_depth: int = 20
    source_max_entries: int = 10000
    source_max_bytes: int = 20 * 1024 * 1024
    source_max_file_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_host(cls, cfg):
        raw = dict(getattr(cfg, "ontology", {}) or {})
        legacy = {
            "enabled": os.environ.get("TE_ONTOLOGY_ENABLED") == "1",
            "state_dir": os.environ.get("TE_ONTOLOGY_STATE", ""),
        }
        for key, value in legacy.items():
            raw.setdefault(key, value)
        if "TE_ONTOLOGY_ENABLED" in os.environ:
            raw["enabled"] = os.environ["TE_ONTOLOGY_ENABLED"] == "1"
        # compile_timeout_seconds was replaced by the adaptive compile_deadline group;
        # mounted yamls may still carry the legacy key across a rolling deploy.
        raw.pop("compile_timeout_seconds", None)
        if isinstance(raw.get("compile_deadline"), dict):
            raw["compile_deadline"] = CompileDeadlineConfig(**raw["compile_deadline"])
        raw["kee_legacy_writes_enabled"] = False
        result = cls(**raw)
        result.queue_limit = max(1, min(32, result.queue_limit))
        result.kee_max_model_calls = max(1, min(64, result.kee_max_model_calls))
        result.kee_compile_deadline_seconds = max(30, min(480, result.kee_compile_deadline_seconds))
        if not result.state_dir:
            result.state_dir = str(Path.home() / ".teamEvolver" / "ontology")
        return result

    def llm(self, host):
        return {
            "model": self.model.get("model") or host.llm_model_id,
            "base_url": self.model.get("base_url") or host.llm_api_base,
            "api_key": self.model.get("api_key") or host.llm_api_key,
        }
