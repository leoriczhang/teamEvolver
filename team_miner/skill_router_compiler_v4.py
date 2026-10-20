"""Load the independent router compiler; semantic extraction stays inside its Skill."""
from pathlib import Path

from .compiler_assets import load_compiler_assets

PROTOCOL = "te-existing-skill-router.v4"
_ROOT = Path(__file__).with_name("compiler_skills") / "skill-router-v4"
AUXILIARY_FILES, SKILL_BODY = load_compiler_assets(_ROOT, ("scripts/assemble_router.py",))
