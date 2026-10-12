# team_miner

team_miner 承载控制台的 OV Skill 挖掘与知识库挖掘。Skill 挖掘由 teamEvolver 管理真实轮次与人工 Evidence 补证，每轮调用 OpenViking Compile；知识库挖掘将来源文档编译为 Wiki，并提供文件编辑、增量编译及关系图。

## 维护入口

| 文件 | 职责 |
| --- | --- |
| `bridge.py` | Skill 挖掘路由、主服务桥接与任务监控 |
| `ov_round_orchestration.py` | 来源冻结、累积补证、独立编译器安装与幂等提交 |
| `ov_compile_mining.py` | 来源注册、任务状态、复核、归档与产物质量 |
| `compile_strategy.py` | OV Compile 能力拆分计划与共享分析、候选包契约 |
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

Skill 挖掘支持 1–5 个真实轮次、每轮 60–7200 秒运行期限、人工补证、附件与来源引用、独立轮次归档、停止和删除。新任务先规划可独立执行的任务方法，再编译 0–8 个 Skill Candidate；数量取决于触发场景、输入输出和独立执行、验证的边界，不按文档篇数拆分。重叠方法合并，完整流程中不能独立执行的步骤保持在同一候选内。8 是执行上限，不是数量目标；纯事实与 Evidence 不足的内容暂缓。

每轮共享 `analysis/evidence-map.md`、`gaps.md`、`review.json` 和 `capability-plan.json`。能力拆分计划使用 `skill-miner.plan.v1`，包含 `schema_version`、`skills` 和 `deferred`；候选条目记录 `name`、`task`、`trigger`、`inputs`、`outputs`、`boundary`、`rationale` 与 `source_refs`，暂缓条目记录 `reason` 与 `source_refs`。每个候选在 `compiled_skill/<skill-name>/` 下有独立的 `SKILL.md`、`EVALUATION.md`、`BENCHMARK.md` 和 `benchmark.json`；后者使用 `team-replay.dataset`，包含 12–24 个案例。零候选保留分析和暂缓原因，不产生可提交的候选包。

首轮从冻结的原始来源确定并保存主要语言；中文主导的材料使用简体中文。后续轮次沿用该语言，不因旧英文产物或补证语言改变。Skill Candidate 正文、说明、案例 Query、Checklist 自然语言以及共享分析和复核问题使用该语言；字段名、ID、文件路径、来源 URI、代码、固定枚举及原文直接引用保持原样。`Checklist`、`Evidence`、`Materials` 等结构标签保持不变。

控制台没有独立的输出语言选择器。运行时将任务语言规则加入新的 Compile 请求，自定义业务编译指令原样保存，无需为语言更新而重置。已受理的请求和已完成的产物不追溯修改，也不会自动翻译或重新 Compile。

控制台按 Skill Candidate 分组浏览产物，每张卡片包含能力说明、该候选的结构校验结果和四个文件。共享分析、暂缓内容与任务级 Evidence 问题单独展示；历史轮次采用相同分组，只读查看。结构校验通过不代表业务事实正确，也不代表已经通过 True Replay。

初次产物同步时，系统从结构合格的 `benchmark.json` 确定性地规范化已生成的 `BENCHMARK.md`，使案例、Checklist 和来源一致，不添加案例或事实。候选分别校验；某个候选缺失 `SKILL.md` 或其他必需文件时独立标记为产物不完整，不能提交，但不阻止其他结构合格的候选。规范化不补齐缺失的必需文件。

上一轮候选集合仅作为修订上下文；原始来源和人工补证才是 Evidence。已有任务保留单候选的七文件契约及冻结编译器版本，不在恢复或继续时自动切换。已保存的旧单候选自定义编译 Skill 继续使用单候选契约并显示兼容性警告；重置为当前默认编译 Skill 后创建的新任务才使用多候选。

完成后可以预览、编辑产物，选择具体 Skill Candidate，连同其专属 Test Dataset 提交到 True Replay 与 Candidate Review。每个候选保留独立的验证、评审和 Publish 结果，通过门禁后进入已有 Skill 库。挖掘不会自动 Publish。

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
