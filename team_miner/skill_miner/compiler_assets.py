"""Read compiler Skill assets without changing their order or frozen content."""

from pathlib import Path

from .native_compiler import _asset_digest

POINT_COMPILER_ASSET_PATHS = (
    "references/source-index.md",
    "references/point-planning.md",
    "references/point-authoring.md",
    "scripts/prepare_point.py",
)


def load_compiler_assets(root: Path, asset_paths: tuple[str, ...]) -> tuple[dict[str, str], str]:
    """Preserve explicit attachment order and the existing compiler digest format."""
    assets = {name: (root / name).read_text(encoding="utf-8") for name in asset_paths}
    body = (root / "SKILL.md").read_text(encoding="utf-8")
    return assets, body + "\ncompiler_assets_sha256: " + _asset_digest(assets) + "\n"
