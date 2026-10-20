# 测试约定

所有 Python 回归测试统一放在 `tests/`，按领域组织。`platform/` 覆盖平台身份、租户、数据库、Context 和 runtime；其余领域目录对应业务模块。`contracts/` 覆盖架构、发布和文档约束，`acceptance/` 覆盖跨领域持久化验收。

文件用 `test_<职责>.py` 命名，不使用实施阶段、日期或临时骨架名称。测试实现保留在领域目录；可复用的 fake、数据工厂和驱动器放在 `support/`。禁止从其他 `test_*.py` 导入 fixture 或 helper。仅在多个测试需要相同环境时使用 `conftest.py`，不要全局自动创建数据库或 App。

```bash
python -m pytest
python -m pytest tests/skills
python -m pytest -m "not postgres and not external"
python -m pytest -m postgres
python -m pytest -m e2e
```

`postgres` 只标记真正需要数据库的用例；未配置隔离 DSN 时跳过。通用 PG 用例使用 `TEAMEVOLVER_TEST_PG_DSN`，每个用例通过当前 `SchemaRunner` 创建独立 schema 并清理。`external` 标记依赖其他仓库的套件，`e2e` 标记完整流程用例。离线 fake 只能证明应用契约，数据库锁和 RLS 由真实 PG 用例验证。

历史实施记录保留在 `docs/acceptance/`；长期测试不依赖历史验收快照或阶段计划。开发预览和独立故障注入脚本放在 `scripts/`。
