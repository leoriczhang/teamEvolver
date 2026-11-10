"""Load the isolated versioned compiler and freeze its attachment digest."""

from pathlib import Path

from ..compiler_assets import load_compiler_assets
from ..native_compiler import _asset_digest

ROOT = Path(__file__).parent.parent / "compiler_skills" / "dialogue-dimensions-v1"
ASSETS = ("CONTRACT.yaml", "references/extract.md", "references/merge.md")


def load():
    for name in ("SKILL.md", *ASSETS):
        path = ROOT / name
        if path.is_symlink() or ROOT.resolve() not in path.resolve().parents:
            raise ValueError("unsafe dialogue compiler asset")
    return load_compiler_assets(ROOT, ASSETS)


AUXILIARY_FILES, SKILL_BODY = load()
ASSET_DIGEST = _asset_digest(AUXILIARY_FILES)


def instruction(stage, quote_mode):
    if stage not in {"extract", "merge"}:
        raise ValueError("invalid dialogue stage")
    return (
        f"execution_protocol=te-dialogue-dimensions.v1; stage={stage}; quote_mode={quote_mode}。\n"
        f"只执行 references/{stage}.md。每文件一个 child，完整读当前文件和实际所有 ranges。"
        "Entry 立即返回 needs_pipeline=true,files=[]；Planner只使用"
        " files=p.map(sources,task=contract.extract);p.finalize(files,into=target)。"
        "contract.extract: execution=agent,input_unit=file,output=files,fields={}。"
        "Transform instructions保留当前stage及附件入口。不得添加Reduce、Shuffle、第二次Map。"
        "输出路径由文件Metadata.output_path或window.output_path给定；未给定时extract使用"
        " analysis/dialogue_dimensions/receipts/<case_ref>-<case_version>.json。"
        "路径相对Compile.to。emit只使用当前assignment真实顶层ID，交付后停止。"
    )
