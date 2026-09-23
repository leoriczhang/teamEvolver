"""Shared maintenance policy for memory maintenance jobs.

These constants keep every job aligned on conservative shared-memory write
policy and prevent maintenance tooling from leaking into team memory. The
maintainer is team-agnostic: it never invents a team name and never presents
the maintenance project/tool/agent as the team.
"""

from __future__ import annotations

import os


MAINTAINER_NAME = "memory-maintainer"


def normalize_team_name(value: str | None = None) -> str:
    """Return a single-line team name from an explicit value or legacy env."""
    raw = (
        value
        if value is not None
        else (
            os.environ.get("EVOLVE_TEAM_DISPLAY_NAME")
            or os.environ.get("DREAMCYCLE_TEAM_NAME")
            or ""
        )
    )
    return " ".join(str(raw or "").split())[:120]


def team_label(team_name: str | None = None) -> str:
    """User-facing label, falling back to a neutral name."""
    return normalize_team_name(team_name) or "团队"


def team_reference(team_name: str | None = None) -> str:
    """Natural-language team reference without duplicating the word 团队."""
    name = team_label(team_name)
    return name if name.endswith("团队") else f"{name} 团队"


def team_overview_title(team_name: str | None = None) -> str:
    """Title for the authoritative team-overview memory."""
    name = normalize_team_name(team_name)
    if not name:
        return "团队概况"
    return f"{name}概况" if name.endswith("团队") else f"{name}团队概况"


def team_naming_rule(team_name: str | None = None) -> str:
    """Per-job line describing how to refer to the team in shared memory."""
    name = normalize_team_name(team_name)
    if name:
        return (
            f"已知团队名称：{name}。共享 memory 只呈现团队自身的真实内容，"
            "不要写维护项目名、维护工具名或维护 agent 名称。"
        )
    return (
        "共享 memory 只呈现团队自身的真实内容；团队名称未知时用“团队”指代，"
        "不要臆造团队名称，也不要写维护项目名、维护工具名或维护 agent 名称。"
    )


SHARED_MEMORY_POLICY = """## 共享空间维护原则
- 共享 memory 只呈现团队自身的真实内容；不写维护项目、维护工具或维护 agent 的名称，也不臆造团队名称。
- 面向用户的标题、正文、元数据都不要出现维护项目名；团队名称未知时统一用“团队”指代。
- 不要为了提高搜索命中率重复写同义入口；优先更新已有权威文档、合并重复内容、归档过时内容。
- 写入前必须先搜索/浏览同主题内容；已有可更新文档时必须用 target_uri 修改原文，不新增并行版本。
- 每次只保留少量高价值、长期有效、可复用的内容；临时检查报告、诊断过程、一次性状态不要沉淀为长期 memory，必要时保存到本地 report。
- 新增文档只允许两种情况：需要新增类别，或确认这是与现有文档不同的团队长期内容；必须填写 allow_create_reason。
- 合并时保留结论和可执行信息，删除重复铺垫、过程流水账和维护者自称。
"""


WRITE_POLICY = """写入/归档规则：
1. 先查重，再写入；默认选择“用 target_uri 更新原文/合并/归档”，不是“新增”。
2. 禁止使用维护项目名或维护工具名作为面向用户文档标题、文件名前缀、正文或元数据。
3. 禁止把维护者写成团队名称；created_by 元数据统一写 memory-maintainer，不要写成团队名或具体维护项目名。
4. 搜索问题、检查报告、运行报告优先用 save_report，不写入长期 user memory。
5. 对重复内容应大胆归档，但只归档已经被合并、明显过时、或维护过程产生的冗余文档。
6. viking_remember 新增文件时必须提供 allow_create_reason；未提供 target_uri 时会被严格审查。
"""
