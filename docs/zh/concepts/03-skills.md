# Skill 体系

Skill 是**可执行、可复用的任务方法**，明确规定适用场景、操作步骤、约束条件与配套资源。Skill 不是 Prompt、不是脚本文件、不是 SOP 文档——它是经过团队验证和治理的可执行能力单元。

## Skill 结构

一个 Skill Bundle 是一个目录，包含：

```
my-skill/
├── SKILL.md          # 主入口（必选），使用 YAML frontmatter 声明元数据
├── references/       # 参考文档（可选）
│   ├── policy.md
│   └── examples.md
├── scripts/          # 辅助脚本（可选）
│   └── helper.py
└── assets/           # 配套资源（可选）
    └── template.md
```

### SKILL.md Frontmatter

```yaml
---
name: my-skill                # Skill 唯一标识
version: "1.2.0"              # 语义化版本
description: 简要描述这个 Skill 的用途
applicable_when: 触发条件描述  # 何时加载此 Skill
required_tools: [edit, bash]  # 需要的工具列表
author: team-name             # 作者/团队
created_at: "2025-01-01"      # 创建时间
updated_at: "2025-03-15"      # 更新时间
tags: [code-review, backend]  # 标签
---
```

## Skill 版本管理

teamEvolver 中的 Skill 有三种状态：

| 状态 | 说明 | 是否影响 Agent |
|------|------|---------------|
| **已发布（Published）** | 当前生效版本，通过所有门禁 | Agent 默认拉取此版本 |
| **候选（Candidate）** | 验证/审核中，不覆盖已发布版本 | 不影响生产 Agent |
| **历史（Archived）** | 被新版本取代，但保留完整内容和审计链 | 不可直接使用，可回滚 |

版本号遵循语义化版本（SemVer）：
- **MAJOR**：不兼容的 Skill 结构变更
- **MINOR**：向后兼容的功能新增
- **PATCH**：向后兼容的问题修复

`SkillMutationService` 维护所有版本的 commit 记录和 tombstone 标记。

## Skill 生命周期

```
  创建/修改
      │
      ▼
  ┌──────────┐    未通过    ┌──────────┐
  │ Candidate │───────────►│  归档     │
  └────┬─────┘             └──────────┘
       │ 通过静态检查
       ▼
  ┌──────────┐    失败      ┌──────────┐
  │TrueReplay│───────────►│ 拒绝/修改 │
  └────┬─────┘             └──────────┘
       │ Checklist + 效率达标
       ▼
  ┌──────────┐    拒绝      ┌──────────┐
  │人工审核  │───────────►│ 拒绝/修改 │
  └────┬─────┘             └──────────┘
       │ 通过
       ▼
  ┌──────────┐
  │ 发布生效  │◄── 回滚 ── 历史版本
  └──────────┘
```

## 技能同步

### 拉取模式（Pull）

Agent 启动时或每次会话前调用：

```
GET /internal/agents/context/skills
Authorization: Bearer <agent-access-token>
```

返回当前已发布的团队 Skill Bundle manifest 和内容。

### 推送模式（Push）

Skill 发布后，`SkillMutationService` 通过 outbox 机制向支持推送的 Agent 发送更新通知。推送适配器在 [skill_sync_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/skill_sync_adapters.py) 中注册。

### Hermes 集成

Hermes 通过 `pre_llm_call` hook 实现自动拉取：

```
用户发起任务 → Hermes pre_llm_call hook → teamEvolver-sync → 
拉取最新 Skill Bundle → 更新 external_dirs → Hermes 原生 skill discovery
```

安装脚本见 [hermes_skill_sync/install.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/hermes_skill_sync/install.py)。

## Skill 与 Memory 的边界

| 维度 | Skill | Memory |
|------|-------|--------|
| 内容性质 | 可执行的任务方法（步骤、流程） | 可检索的事实和背景 |
| 是否规定执行流程 | 是，明确操作步骤 | 否，不规定完整流程 |
| 验证方式 | True Replay 对比验证 | DreamCycle 语义去重/合并 |
| 更新门禁 | 严格（自动+人工） | 宽松（按风险自动或人工） |
| 典型例子 | "如何做 Code Review"、"如何写单测" | "团队用的是 pnpm"、"服务端口是 52010" |

## 代码入口

| 模块 | 路径 |
|------|------|
| Skill Bundle 模型 | [skills/bundle.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/bundle.py) |
| 变更服务 | [skills/mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py) |
| 渲染引擎 | [skills/render.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/render.py) |
| 注册表 | [skills/registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/registry.py) |
| Frontmatter 解析 | [skills/frontmatter.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/frontmatter.py) |
