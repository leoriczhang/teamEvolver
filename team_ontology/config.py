"""One TE-owned configuration surface; no separate runtime deployment."""

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class OntologyConfig:
    enabled: bool = False
    state_dir: str = ""
    # Deprecated YAML compatibility only; publication uses native trusted authentication.
    signing_key_file: str = ""
    signing_key_id: str = "te-sit-v1"
    queue_limit: int = 32
    allow_fixture: bool = False
    model: dict = field(default_factory=dict)
    compile_skill_uri: str = "viking://agent/skills/ontology-extraction-v1"
    compile_workspace_uri: str = "viking://resources/te_ontology_compile"
    compile_user: str = "te-ontology-compiler"
    compile_timeout_seconds: int = 1800
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
        result = cls(**raw)
        result.queue_limit = max(1, min(32, result.queue_limit))
        if not result.state_dir:
            result.state_dir = str(Path.home() / ".teamEvolver" / "ontology")
        return result

    def llm(self, host):
        return {
            "model": self.model.get("model") or host.llm_model_id,
            "base_url": self.model.get("base_url") or host.llm_api_base,
            "api_key": self.model.get("api_key") or host.llm_api_key,
        }
