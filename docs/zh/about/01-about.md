# 关于 teamEvolver

teamEvolver 是 Agent 团队能力进化控制面。它接收 Agent 的真实 Session，从中提取 Evidence，生成 Skill Candidate 并通过 True Replay 验证，经 Candidate Review 发布为团队 Skill；同时把成员的个人 Memory 聚合、维护为团队 Memory。

## 设计原则

1. **Evidence 驱动**：每次 Skill 或 Memory 变更都能追溯到来源 Session
2. **真实验证**：Candidate 在独立工作区中与 Baseline 对比回放，先满足 Checklist 再比较效率
3. **白盒可控**：Prompt、模型参数与流程参数可在控制台查看、修改和测试
4. **治理优先**：发布需经过门禁与人工评审，版本链与审计记录完整保留
5. **租户隔离**：所有业务数据按租户隔离，由 PostgreSQL 行级安全保证

## 技术栈

| 层 | 组成 |
|------|------|
| 后端 | Python 3.10+、FastAPI、asyncpg |
| 前端 | React 18、TypeScript、Vite、Tailwind CSS |
| 权威存储 | PostgreSQL（必需） |
| 知识存储 | OpenViking（Memory、Resource、Compile） |
| 可观测性 | Langfuse（可选） |

运行时分为 `api` 与 `worker` 两个角色，可分别水平扩展，见 [架构](../concepts/01-architecture.md)。

## 开源协议

MIT License，见仓库根目录 `LICENSE`。

## 贡献

提交代码前：

1. 运行测试：`python -m pytest tests/`
2. 运行代码检查：`ruff check .`
3. 前端改动运行 `npm --prefix web-ui test` 与 `npm --prefix web-ui run build`
4. 同步更新中英文文档，并通过 `node docs/scripts/check-docs-refs.mjs --strict`，见 [文档维护](../api/99-docs-maintenance.md)
