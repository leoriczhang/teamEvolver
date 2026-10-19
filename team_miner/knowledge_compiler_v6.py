"""Load the self-contained compiler Skill; business preparation stays inside its assets."""

from pathlib import Path

from .native_compiler import _asset_digest

PROTOCOL = "te-knowledge-point-skill-mining.v6"
_ROOT = Path(__file__).with_name("compiler_skills") / "knowledge-point-v6"
SOURCE_INDEX = (_ROOT / "references/source-index.md").read_text(encoding="utf-8")
POINT_PLANNING = (_ROOT / "references/point-planning.md").read_text(encoding="utf-8")
POINT_AUTHORING = (_ROOT / "references/point-authoring.md").read_text(encoding="utf-8")
AUXILIARY_FILES = {
    "references/source-index.md": SOURCE_INDEX,
    "references/point-planning.md": POINT_PLANNING,
    "references/point-authoring.md": POINT_AUTHORING,
    "scripts/prepare_point.py": (_ROOT / "scripts/prepare_point.py").read_text(encoding="utf-8"),
}
SKILL_BODY = (_ROOT / "SKILL.md").read_text(encoding="utf-8") + (
    "\ncompiler_assets_sha256: " + _asset_digest(AUXILIARY_FILES) + "\n"
)
