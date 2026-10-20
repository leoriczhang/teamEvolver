"""Load the self-contained compiler Skill; business preparation stays inside its assets."""

from pathlib import Path

from .compiler_assets import POINT_COMPILER_ASSET_PATHS, load_compiler_assets

PROTOCOL = "te-knowledge-point-skill-mining.v8"
_ROOT = Path(__file__).with_name("compiler_skills") / "knowledge-point-v8"
AUXILIARY_FILES, SKILL_BODY = load_compiler_assets(_ROOT, POINT_COMPILER_ASSET_PATHS)
SOURCE_INDEX = AUXILIARY_FILES["references/source-index.md"]
POINT_PLANNING = AUXILIARY_FILES["references/point-planning.md"]
POINT_AUTHORING = AUXILIARY_FILES["references/point-authoring.md"]
