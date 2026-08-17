from __future__ import annotations

from pathlib import Path

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.evolve import EvolveServer, EvolveServerConfig

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_SOURCE_MARKERS = (
    "team_evolve_agent",
    "TEAMEVOLVER_EVOLVER_REPO",
    "from skill_evolver",
    "import skill_evolver",
    "from skillgene",
    "import skillgene",
)


def test_runtime_source_has_no_external_evolver_imports() -> None:
    hits: list[str] = []
    for path in sorted((ROOT / "teamEvolver").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for marker in FORBIDDEN_SOURCE_MARKERS:
            if marker in text:
                hits.append(f"{path.relative_to(ROOT)}: {marker}")
    assert hits == []


def test_builtin_evolve_engine_instantiates_from_primary_config(tmp_path: Path) -> None:
    config = TeamEvolverConfig(
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_viking_endpoint="memory://" + str(tmp_path / "storage"),
        skills_dir=str(tmp_path / "skills"),
    )
    Path(config.skills_dir).mkdir(parents=True)

    evolve_config = EvolveServerConfig.from_teamEvolver_config(config)
    server = EvolveServer(evolve_config)

    assert type(server).__module__.startswith("teamEvolver.evolve.")
    assert evolve_config.storage_backend == "viking"


def test_agent_protocol_install_resources_are_present() -> None:
    integrations = ROOT / "teamEvolver" / "integrations"

    assert (integrations / "agent_protocol.py").is_file()
    assert (integrations / "hermes_delivery.py").is_file()
    assert (
        integrations / "hermes_context_provider" / "__init__.py"
    ).is_file()
    assert (
        integrations / "hermes_context_provider" / "plugin.yaml"
    ).is_file()
    assert (
        ROOT / "docs" / "schemas" / "agent-registration-v1.schema.json"
    ).is_file()
    assert (
        ROOT / "docs" / "schemas" / "replay-branch-result-v1.schema.json"
    ).is_file()
