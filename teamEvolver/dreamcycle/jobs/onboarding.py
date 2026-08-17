"""Onboarding Check Job — ensure new members can discover team context."""

from __future__ import annotations

from .base import Job
from ..react.planner import TaskPlan, TaskPlanner
from ..policy import SHARED_MEMORY_POLICY, WRITE_POLICY, team_naming_rule


class OnboardingCheckJob(Job):
    """Ensure team space is new-member friendly."""

    @property
    def name(self) -> str:
        return "onboarding_check"

    @property
    def description(self) -> str:
        return "Verify team space is discoverable and useful for new members"

    @property
    def priority(self) -> int:
        return 40

    def create_plan(self) -> TaskPlan:
        return TaskPlanner.plan_onboarding()

    def get_system_prompt(self) -> str:
        return f"""你是共享知识库新人可发现性检查员。

{SHARED_MEMORY_POLICY}
{WRITE_POLICY}

{team_naming_rule(self.team_name)}

## 任务
确保你自己 user memory（viking://user/memories/）中的团队上下文对新加入的成员有用——新人通过搜索/浏览能快速了解团队。

## 检查清单
模拟一个新人会搜索的问题：
1. “这个团队是做什么的？” → 应该能找到团队介绍
2. “团队有哪些人？” → 应该能找到成员列表
3. “目前在做什么项目？” → 应该能找到项目概览
4. “常用工具和服务有哪些？” → 应该能找到工具清单
5. “工作流程是什么？” → 应该能找到基本的协作流程

## 工作方式
1. 对每个检查项执行 viking_search，同时用 viking_browse 校验目录
2. 如果返回有用结果 → 通过
3. 如果返回空或不相关 → 优先报告搜索/索引问题，不要新建重复“搜索友好入口”
4. 如果确实缺失且无法推断 → 记录为待补充，优先 save_report
5. 只有当核心权威文档不存在时，才允许 viking_remember 新建一份精简入口，并填写 allow_create_reason

## 补充方式
- 默认不写入长期 memory；确需写入时，更新已有权威文档
- content 格式：简洁、结构化、易读
- 标注信息来源和时间
- 不编造信息，只整理已有内容
"""
