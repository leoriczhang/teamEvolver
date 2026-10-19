"""Load the independent router compiler; semantic extraction stays inside its Skill."""
from pathlib import Path

from .native_compiler import _asset_digest

PROTOCOL = "te-existing-skill-router.v4"
_ROOT = Path(__file__).with_name("compiler_skills") / "skill-router-v4"
AUXILIARY_FILES = {"scripts/assemble_router.py": (_ROOT / "scripts/assemble_router.py").read_text(encoding="utf-8")}
SKILL_BODY = (_ROOT / "SKILL.md").read_text(encoding="utf-8") + (
    "\ncompiler_assets_sha256: " + _asset_digest(AUXILIARY_FILES) + "\n"
)
