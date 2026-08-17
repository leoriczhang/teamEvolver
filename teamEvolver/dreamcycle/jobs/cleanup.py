"""Cleanup Job — archive stale and outdated team memories."""

from __future__ import annotations

from .base import Job
from ..react.planner import TaskPlan, TaskPlanner
from ..policy import SHARED_MEMORY_POLICY, WRITE_POLICY, team_naming_rule


class CleanupJob(Job):
    """Archive stale, outdated, or completed project memories."""

    @property
    def name(self) -> str:
        return "cleanup"

    @property
    def description(self) -> str:
        return "Archive stale or outdated team memories"

    @property
    def priority(self) -> int:
        return 30

    def create_plan(self) -> TaskPlan:
        return TaskPlanner.plan_cleanup()

    def get_system_prompt(self) -> str:
        return f"""你是共享知识库清理维护员。

{SHARED_MEMORY_POLICY}
{WRITE_POLICY}

{team_naming_rule(self.team_name)}

## 任务
清理你自己 user memory（viking://user/memories/）中过时、已完成、重复、或不再相关的记忆。

## 归档标准
1. 已完成的项目里程碑/进度信息（保留结论性总结，归档过程细节）
2. 超过 30 天且不再相关的临时性信息
3. 被更新版本取代的旧版本知识
4. 非常具体/临时的调试信息、搜索诊断、检查报告
5. 标题或正文把维护项目名误写成团队/知识库品牌的冗余入口

## 不归档
- 唯一的团队组织结构信息（除非已合并到新权威文档）
- 长期有效且未被覆盖的 SOP/工作流
- 人员信息（即使不活跃，也可能回来）
- 工具使用指南（除非工具已下线或已合并）

## 工作方式
1. 先调用 memory_audit 搜索可能过时或重复的内容（旧项目名、检查报告、搜索友好、合并版、维护项目误命名等）
2. 用 viking_read 确认内容确实过时/重复/误命名
3. 对保留文档调用 memory_sanitize 清理误命名
4. 用 viking_forget 归档，并说明原因
5. 如需保留结论，先合并到权威文档再归档原文

## 限制
- 只操作你自己的 user memory 空间
- 每次清理不超过 8 条，避免过度清理
"""
