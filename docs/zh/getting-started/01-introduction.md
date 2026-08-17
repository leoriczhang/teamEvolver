# 产品简介

teamEvolver 是 **Agent 团队能力进化控制面**。它不是另一个 Agent Runtime，而是架设在现有 Agent Runtime（如 Hermes、Pi、Codex 等）之上的能力进化层，把真实工作中产生的经验转化为可复用、可验证、可治理的团队 Skill 与团队 Memory。

## 核心定位

Agent Runtime 负责执行任务，teamEvolver 负责让团队越用越强：

- **经验回流**：从 Agent 的实际 Session 中提取可复用的 Evidence
- **候选生成**：基于 Evidence 生成 Skill Candidate 和 Memory Change 提案
- **真实验证**：通过 True Replay 在隔离的真实 Agent Runtime 中并行运行 Baseline 和 Candidate
- **门禁发布**：Checklist 完成性门禁 + 效率对比（轮次/工具调用/Token）+ 管理员审核
- **持续进化**：DreamCycle 定期执行团队 Memory 聚合、去重、清理和概况维护

## 与 OpenViking 的关系

teamEvolver 使用 OpenViking 作为唯一的持久化存储后端，不使用任何本地 ObjectStore：

| 数据类型 | OpenViking 中的位置 |
|---------|-------------------|
| 团队 Skill | `viking://skills/` 下的版本化 Skill Bundle |
| 团队 Memory | `viking://memory/team/` 下的长期记忆条目 |
| 个人 Memory | `viking://memory/personal/<user>/` 下的个人记忆 |
| Session 归档 | `viking://sessions/` 下的完整轨迹与 Evidence |
| Snapshot | `viking://snapshots/` 下的冻结 Context 投影 |

## 适用场景

- **团队 Coding Agent**：多台 Hermes 机器共享团队 Skill，自动从完成任务中提炼新技能
- **多 Runtime 接入**：Pi、Hermes 等多种 Coding Agent Runtime 统一进化和能力分发
- **企业内部 Agent**：私有化部署，从业务对话中沉淀领域 Memory 和 Skill
- **评测与进化研究**：使用 True Replay 做严谨的 Baseline vs Candidate 对比实验

## 不做什么

teamEvolver 明确不承担以下职责：

- **不执行任务**：没有自己的 Agent Runtime，所有 Replay 在接入方 Runtime 中执行
- **不替代 Agent 配置**：Agent 自身的模型、工具、系统 Prompt 仍由 Runtime 管理
- **不做中心化推理**：进化 Pipeline 中的 LLM 调用可配置，但只是辅助分析，不替代 Runtime 的推理
- **不做未经审核的自动发布**：Skill 发布必须经过 CheckList 门禁和人工审核

## 下一步

- [快速开始](./02-quickstart)：5 分钟跑起一个本地 teamEvolver 实例
- [安装部署](./03-installation)：完整的安装与配置说明
