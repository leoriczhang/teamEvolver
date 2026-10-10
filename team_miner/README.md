# team_miner

team_miner 承载控制台的 OV Skill 挖掘与知识库挖掘。Skill 挖掘由 teamEvolver 管理真实轮次与人工 Evidence 补证，每轮调用 OpenViking Compile；知识库挖掘将来源文档编译为 Wiki，并提供文件编辑、增量编译及关系图。

## 维护入口

| 文件 | 职责 |
| --- | --- |
| `bridge.py` | Skill 挖掘路由、主服务桥接与任务监控 |
| `ov_round_orchestration.py` | 来源冻结、累积补证、独立编译器安装与幂等提交 |
| `ov_compile_mining.py` | 来源注册、任务状态、复核、归档与产物质量 |
| `compile_strategy.py` | OV Compile 计划与七个输出文件契约 |
| `routes.py` | 知识库上传、编译、Wiki 编辑与发现 |
| `document_converter.py` | 上传文档转换 |
| `wiki_graph.py` | Wiki 链接索引与关系图 |
| `settings.py` | 默认轮数、运行期限与编译 Skill 配置 |
| `benchmark_format.py` | 挖掘产物与统一 Test Dataset 的格式边界 |
| `lifecycle.py` | 已完成产物提交到 Skill Candidate 生命周期 |
| `frontend/` | 当前 React 页面与测试 |
| `tests/` | OV、知识库、候选提交和 Dataset 保护测试 |

## 运行与使用

从仓库根目录启动主服务：

```bash
python -m pip install -e ".[all,dev]"
teamEvolver start --daemon
```

在 `http://127.0.0.1:52010/` 登录，配置 OpenViking 的 Endpoint、Account 和服务端凭据，再注册或上传来源目录。给来源标记 Skill 或 Knowledge 用途后，在对应页面创建任务。

Skill 挖掘支持 1–5 个真实轮次、每轮 60–7200 秒运行期限、人工补证、附件与来源引用、独立轮次归档、停止和删除。每轮产生三个分析文件和四个 Skill 包文件，其中 `benchmark.json` 使用 `team-replay.dataset`。上一轮候选仅作为修订上下文；原始来源和人工补证才是 Evidence。

完成后可以预览、编辑产物，再将 Skill Candidate 和 Test Dataset 提交到 True Replay 与 Candidate Review。挖掘不会自动 Publish。

运行状态及复核记录在本地任务目录持久化；来源、补证和远端输出使用 OpenViking。不要删除仍在执行或等待复核任务的本地目录，也不要将运行数据或凭据提交到 Git。

详细操作见 [Skill 挖掘](../docs/zh/guides/07-skill-miner.md)、[多轮补证](../docs/zh/guides/17-multi-round-skill-mining.md) 和 [API](../docs/zh/api/17-skill-mining.md)。

## 验证

```bash
python -m pytest team_miner/tests tests/test_dataset_schema.py
npm --prefix web-ui test -- --run
npm --prefix web-ui run build
node docs/scripts/check-docs-refs.mjs
```

测试使用独立临时目录和模拟 OV，不应读取或改写本机运行中的任务。修改主服务后应按根目录 AGENTS.md 的进程约定重启服务。
