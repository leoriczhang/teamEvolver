# 关于 teamEvolver

teamEvolver 是一个开源的 Agent 团队能力进化控制面，让团队的 AI Agent 从真实工作中持续学习和进化。

## 项目理念

AI Agent 的价值不在于单次回答的质量，而在于团队能力的持续积累。当一个 Agent 完成了一次复杂任务，其中产生的经验应该被整个团队复用，而不是消失在对话历史中。

teamEvolver 的设计原则：

1. **Evidence 驱动**：每一次 Skill/Memory 的变更都有可追溯的 Evidence 来源
2. **真实验证**：Candidate 必须在真实 Agent Runtime 的隔离环境中通过 True Replay 验证
3. **白盒可控**：所有 Prompt、模型参数、进化流程都可配置、可观测、可干预
4. **安全隔离**：Replay 分支在沙箱中运行，不产生真实外部副作用
5. **治理优先**：Checklist 门禁 + 人工审核 + 完整审计链

## 技术栈

- **后端**：Python 3.10+, FastAPI, Pydantic
- **前端**：React 18, TypeScript, Vite, shadcn/ui, Tailwind CSS
- **存储**：OpenViking（必须）
- **可观测性**：Langfuse（可选）
- **文档**：Markdown 源文件 + 控制台内置阅读器（全文搜索、中英双语）

## 开源协议

MIT License。详见 [LICENSE](https://github.com/leoriczhang/teamEvolver/blob/main/LICENSE)。

## 贡献

欢迎提交 Issue 和 Pull Request：

- Issue 追踪：[GitHub Issues](https://github.com/leoriczhang/teamEvolver/issues)
- 代码仓库：[leoriczhang/teamEvolver](https://github.com/leoriczhang/teamEvolver)
- 文档规范：见 [文档维护指南](../api/99-docs-maintenance)

提交代码前请确保：
1. 通过现有测试：`python -m pytest tests/ -v`
2. 遵循代码风格：`ruff check teamEvolver/`
3. 更新相关文档
