"""Team Overview Job — maintains team member list, roles, and active projects."""

from __future__ import annotations

from .base import Job
from ..react.planner import TaskPlan, TaskPlanner
from ..policy import (
    SHARED_MEMORY_POLICY,
    WRITE_POLICY,
    team_naming_rule,
    team_overview_title,
)


class TeamOverviewJob(Job):
    """Maintain an up-to-date team overview in shared space."""

    @property
    def name(self) -> str:
        return "team_overview"

    @property
    def description(self) -> str:
        return "Maintain team member list, roles, and active project summary"

    @property
    def priority(self) -> int:
        return 10  # Runs first — foundational

    def create_plan(self) -> TaskPlan:
        return TaskPlanner.plan_team_overview()

    def get_system_prompt(self) -> str:
        overview_title = team_overview_title(self.team_name)
        return f"""你是共享知识库的团队概况维护员。

{SHARED_MEMORY_POLICY}
{WRITE_POLICY}

{team_naming_rule(self.team_name)}

## 任务
维护你自己 user memory（viking://user/memories/）中的团队概况信息，确保：
1. 团队成员名单是最新的（包含各人的主要职责方向）
2. 团队当前在推进的项目有一个汇总
3. 常用服务、工具、地址等信息保持准确

## 工作方式
1. 先用 list_customers 获取你名下的所有 peer
2. 搜索你自己的 memory 看是否已有团队概况/成员/项目总览；必须优先更新权威文档
3. 必要时用 viking_search（scope=own）回顾自己 memory（含 peers）中的相关记录
4. 如有多个同类文档，先合并成一个精简版本，再归档旧版本
5. 写入时优先传 target_uri 更新现有 overview；只有新增类别/独立长期内容才允许不传 target_uri 并填写 allow_create_reason

## 质量要求
- 输出应是“{overview_title}”，不要写维护项目名。
- 不新增搜索友好版、合并版、入口版等平行文档；只能保留一个权威入口。
- 不复制个人隐私信息（如个人偏好、私人事项）
- 只记录工作相关的公开信息
- 信息不确定时标注“待确认”，不臆造成员或项目
"""
