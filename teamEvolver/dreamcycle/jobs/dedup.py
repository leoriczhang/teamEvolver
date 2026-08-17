"""Deduplication Job — find and merge semantically similar team memories."""

from __future__ import annotations

from .base import Job
from ..react.planner import TaskPlan, TaskPlanner
from ..policy import SHARED_MEMORY_POLICY, WRITE_POLICY, team_naming_rule


class DeduplicationJob(Job):
    """Find and merge duplicate/overlapping team memories."""

    @property
    def name(self) -> str:
        return "deduplication"

    @property
    def description(self) -> str:
        return "Find and merge duplicate or highly similar team memories"

    @property
    def priority(self) -> int:
        return 20

    def create_plan(self) -> TaskPlan:
        return TaskPlanner.plan_deduplication()

    def get_system_prompt(self) -> str:
        return f"""你是共享知识库去重维护员。

{SHARED_MEMORY_POLICY}
{WRITE_POLICY}

{team_naming_rule(self.team_name)}

## 任务
主动识别你自己 user memory（viking://user/memories/）中语义重复、高度重叠、维护过程产生的冗余记忆，并将它们合并/归档。
重点：文件名不同但内容讲同一件事的条目也是重复项——不要只看文件名。

## 工作方式
1. 先调用 memory_audit 获取重复候选（它会给出文件名相近组，以及基于正文的高相似度组 similar_content_group）。
2. 用 viking_browse tree 浏览每个目录（尤其 experiences/、pattern/、trajectories/），把“同一主题/同一工作对象”的多条记忆列成候选组。
3. 对同目录下主题相近的条目（例如同一份营销笔记/报告的“方案补全 / 拍摄补充 / 合规补全 / 数据校验”这类围绕同一交付物的多条经验），一律用 viking_read 读全文逐条对比。
4. 判断重复/重叠：只要两条讲的是同一工作对象且结论可合并，就视为应合并，不要因为标题措辞不同而放过。
5. 如果确实重复或高度重叠：
   a. 用 viking_remember + target_uri 把它们合并进一条精简、权威、高质量版本（保留全部可执行结论，去掉重复铺垫）；标题不带维护项目名前缀。
   b. 用 viking_forget 归档其余被合并的旧条目，并在 replaces 里列出被合并的 URI。
6. 优先归档：搜索友好版、合并版旧稿、检查报告、诊断过程、重复导航、被新版本完全覆盖的文档、以及围绕同一交付物拆成多条的经验碎片。

## 判断标准
- 完全重复：归档低质量/旧版本/维护痕迹更重的一条。
- 部分重叠（同一工作对象的不同侧面）：合并为一条完整版本，归档原始重复条。
- 互补但过细：并入更高层文档；除非长期独立有价值，否则归档。
- 临时运行产物：保存报告即可，不作为长期 memory。

## 限制
- 只操作你自己的 user memory 空间
- 归档时说明原因
- 不误删唯一事实来源；但不要因为“不确定”而保留明显重复的平行版本
- 本轮至少完成一组真实的合并或归档（若确有重复），不要只浏览不动手
"""
