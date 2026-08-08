# Document-to-Skill Pipeline

将一组同主题领域文档挖掘为可执行的 Agent Skill，并同步生成可复跑的评测基准与稳定性报告。

项目基于 [Hermes](https://github.com/NousResearch/hermes-agent) 的 `hermes -z` 单次调用能力实现，Python 部分仅使用标准库。

## 能力

1. **样本包构建**：按证据视角和上下文容量把输入文档组织成若干样本包。产物随即经过程序化的切分质量校验（切片深度、跨包去重、覆盖、common/ 一致性等硬指标），有硬伤时自动携带违规明细重跑一次，仍不过则中止本轮。
2. **语义发现**：从每个样本包归纳可复用的决策单元、流程和边界，并标注证据缺口（GAP）。
3. **Skill 编译**：生成 `SKILL.md` 与配套的 `EVALUATION.md`，并给出置信档与待补缺口清单。
4. **反思环**：在置信档未收敛且仍有补充素材时，携带上一轮缺口进行定向补证（默认最多 3 轮）。
5. **Benchmark**：依据 `EVALUATION.md` 构建题库，支持多轮对话（模拟情境参与者）与单轮作答两种跑分方式、难度分布配额。
6. **轨迹 Benchmark 独立挖掘**：直接接收 teamEvolver/SkillGen 或 OpenAI messages 风格轨迹，生成 held-out Benchmark；不进入样本包、语义发现、Skill 编译或 LIFT 流程。
7. **多次构建 · 交集 · 稳定性复跑**：把多次构建的题库存为快照、求交集，再对交集项各跑多个 session，观察 skill 在多轮对话下的行为稳定性。
8. **覆盖报告**：统计语义单元采纳率、GAP 消解率和维度证据覆盖。
9. **Web 控制台**：提供真实运行、人工检查点、跑分和覆盖报告入口。
10. **LIFT 适配**：把 SkillMiner 题库转换为 LIFT Suite v1 与 Markdown 场景，经人工编辑、校验、批准后再发布到外部 LIFT 工作区，并可从统一控制台启动评测。

## 项目结构

```text
.
├── data/input/                         # 放入待挖掘的领域文档（默认输入，仓库不含业务语料）
├── sample-package-constructor-agent-skill/
├── semantic-discovery-agent-skill/
├── evaluation-compiler-agent-skill/    # 三个流水线 Agent Skill
├── web_console/                        # 标准库 SSE 控制台
├── run_pipeline.py                     # 主流水线：Step 1-3 + 反思环
├── validate_sample_packages.py         # Step1 切分质量校验器（深度/去重/覆盖硬指标）
├── run_benchmark.py                    # 构建 / 执行 benchmark
├── trajectory_benchmark.py             # 轨迹 → Benchmark 独立挖掘接口
├── run_coverage_report.py              # 语义覆盖报告
├── run_multi_session.py                # 多次 benchmark 的快照 / 交集 / 稳定性复跑
├── lift_integration.py                 # SkillMiner → LIFT 转换、审核、发布与运行桥
├── run_skill_test.py                   # 通用 smoke test
├── test_pipeline_static.py             # 不调用模型的静态自检
├── clean_artifacts.sh                  # 清理运行产物
├── .env.example                        # 凭据环境变量示例
└── .gitignore
```

运行时生成的产物目录默认被 Git 忽略：

```text
sample_packages/      # Step 1 产物：样本包 + 全局/分包笔记
semantic_reports/     # Step 2 产物：各样本包的语义分析报告
compiled_skill/       # Step 3 产物：<skill-name>/SKILL.md、EVALUATION.md、benchmark.*
reflection_rounds/    # 反思环各轮的中间产物
run_history/          # 新任务启动时隔离保存的上一批生成物
benchmark_sessions/   # 快照、交集清单与多 session 复跑留档
trajectory_benchmarks/# 从轨迹独立挖掘的 Benchmark 及清单
lift_datasets/        # LIFT 待审核草稿、已发布快照与运行元数据
.hermes_home/         # Hermes 运行时状态（含 config.yaml，不应提交）
```

此外 `logs/` 下的运行日志按 `*.log` 规则被忽略；`clean_artifacts.sh` 会清理上述除 `.hermes_home/` 外的产物目录（`.hermes_home/` 已配好 provider，如需重置请手动删除）。

## 前置条件

- Python 3.10+
- 已安装并可从 `PATH` 调用的 Hermes：

```bash
hermes --version
```

- 一个可用的模型 provider。本项目默认对接**火山方舟（Volcengine Ark）**，在 `.hermes_home/config.yaml` 中以 `custom:volcengine-ark` provider 配置，默认模型为 `doubao-seed-1-6-250615`（256k 上下文，输出上限 32768）。密钥通过环境变量注入，不要写进仓库：

```bash
cp .env.example .env
# 在 shell 或密钥管理工具中设置：
export ARK_API_KEY="your-api-key"
```

> 说明：`run_pipeline.py` / `run_benchmark.py` / `run_multi_session.py` 启动时会读取 `ARK_API_KEY` 并注入 Hermes 运行环境，并先做一次连通性自检（返回 `HERMES_OK` 即通过）。自检失败时默认立即停止，避免已知无效的调用继续消耗时间；仅当确认是探测命令与 provider 不兼容时，主流水线可显式使用 `--allow-connection-probe-failure`。若换用其他 OpenAI 兼容端点，改 `config.yaml` 里的 `providers.*.base_url` 与 `model.default` 即可。首次运行会创建项目本地 `.hermes_home/`；该目录已配好上述 provider，重跑不会被覆盖。

## 快速开始

1. 将同一主题的 Markdown 文档放入 `data/input/`。
2. 运行静态自检（不调用模型）：

```bash
python3 test_pipeline_static.py
```

3. 运行一次挖掘（Step 1-3 + 反思环）：

```bash
python3 run_pipeline.py --input data/input --max-rounds 1
```

4. 查看输出：

```text
compiled_skill/<skill-name>/SKILL.md
compiled_skill/<skill-name>/EVALUATION.md
```

5. 生成并执行 benchmark：

```bash
python3 run_benchmark.py --difficulty-dist "easy:3,medium:8,hard:7" --target-total 16
```

6. 生成语义覆盖报告：

```bash
python3 run_coverage_report.py
```

## 从轨迹独立挖掘 Benchmark

该接口只生成 Benchmark，不依赖 `data/input/`，不运行 Step 1–3，不会生成样本包、语义报告、`SKILL.md`、`EVALUATION.md` 或 LIFT 草稿。统一 teamEvolver 服务下的标准入口为：

```text
POST /api/mining/trajectory-benchmarks
```

请求示例：

```json
{
  "dataset_name": "skillgen-evolution",
  "target_total": 18,
  "difficulty_dist": "easy:3,medium:10,hard:5",
  "trajectories": [
    {
      "session_id": "session-001",
      "turns": [
        {
          "turn_num": 1,
          "prompt_text": "用户任务",
          "response_text": "Agent 回答",
          "tool_calls": [],
          "tool_results": [],
          "success": true
        }
      ]
    }
  ]
}
```

`trajectories` 支持三类常用形状：teamEvolver `turns`、OpenAI `messages`、以及含 `action` / `observation` 的 `steps` / `events`。服务会在进入模型前去重、限制规模并脱敏密钥、手机号、邮箱和本机用户路径。

创建成功返回 HTTP `202` 以及 `run.run_id`、`unified_status_path`。使用返回的状态地址轮询即可：

```text
GET /api/mining/trajectory-benchmarks/<run_id>
```

状态为 `running`、`done`、`error` 或 `stopped`。完成后返回标准化题目，并将三个产物写入 `trajectory_benchmarks/<run_id>/`：

```text
benchmark.jsonl       # teamEvolver-benchmark-v1 题库
BENCHMARK.md          # 便于人工审核的可读版
manifest.json         # 来源摘要、难度和维度统计
```

结果列表可通过 `GET /api/mining/trajectory-benchmarks` 查询。源轨迹原文不落盘；`manifest.json` 只保留轨迹 ID、数量与 SHA-256 摘要，用于审计和去重。

## LIFT 集成与人工审核

SkillMiner 生成 `benchmark.jsonl` 后，会默认同时生成一个 LIFT 待审核草稿。该动作只写入本项目的 `lift_datasets/drafts/`，不会直接修改 LIFT。若暂时不需要自动生成，可设置：

```bash
export SKILLMINER_LIFT_AUTO_DRAFT=0
```

先在 teamEvolver 仓库根目录准备外部 LIFT checkout：

```bash
bash scripts/setup_lift.sh

# 可选：同时创建 Python 3.12 虚拟环境并安装上游依赖
bash scripts/setup_lift.sh --install-deps
export TEAMEVOLVER_LIFT_PYTHON="$PWD/external/LIFT/.venv-teamEvolver/bin/python"
```

也可以复用已有 checkout：

```bash
export TEAMEVOLVER_LIFT_ROOT=/absolute/path/to/LIFT
export TEAMEVOLVER_LIFT_PYTHON=/absolute/path/to/python
```

启动 teamEvolver 控制台后进入“评测中心”，按以下顺序操作：

1. 从已生成 benchmark 的 Skill 创建 LIFT 草稿。
2. 逐题检查并编辑 `query`、内容要求和轨迹要求，确认 warmup/holdout 划分。
3. 保存并通过结构校验，然后点击“人工审核通过”。审核人取自当前 teamEvolver 登录用户。
4. 点击“发布到 LIFT”。只有 `approved` 状态允许发布；同名旧数据会先备份，失败时自动恢复。
5. 选择 runtime 并启动。运行日志会实时显示，完整结果保留在 LIFT 的 `results/lift-runid-*/`。

发布后的结构为：

```text
<LIFT>/assets/benchmarks/teamEvolver/<suite>.json
<LIFT>/assets/benchmark_mds/teamEvolver/<suite>/
├── train/q*/q*.md
├── test/q*/q*.md
└── skills/<skill>/SKILL.md
```

Suite JSON 严格使用 LIFT 的 `name`、`category`、`warmup_tasks`、`holdout_tasks` 契约；每个 task 包含 `query`、`requirements` 和 `expected_result`。当前适配固定审计 revision `ed8c9d750d729e4c5b1bbf237dd8483d9d142689`。LIFT 完整运行还需要按其文档配置 Docker、Langfuse、模型凭据和对应 runtime 镜像。

> LIFT 仓库当前未提供许可证文件，因此本项目不复制或打包其源码，只通过数据契约连接一个由你单独配置的外部 checkout。部署或再分发前请自行确认上游授权条件。

## 多次构建 · 交集 · 稳定性复跑

出题带随机性，单次题库不足以判断 skill 是否稳定。推荐做法是**多次构建题库 → 每次存快照 → 求交集 → 对交集项跑多个 session**：

```bash
# 1) 多次仅构建题库，每次构建后存一个快照
python3 run_benchmark.py --build-only
python3 run_multi_session.py snapshot          # 序号自增，也可 --slot N 指定

# 2) 查看快照与交集概况（不调用模型）
python3 run_multi_session.py status

# 3) 求交集并写清单（不调用模型）
#    默认先按情境文本相似度求交集；若为空（各次构建情境措辞差异大），
#    自动回退到「按考核维度」求交集。也可 --by-dimension 强制维度口径。
python3 run_multi_session.py intersect

# 4) 对交集项各跑 M 个 session（会调用模型），生成 SESSIONS_REPORT.md
python3 run_multi_session.py run --sessions 3

# 只重跑某个维度（其余维度复用已有结果，报告仍完整）——定向修题后省额度重验
python3 run_multi_session.py run --sessions 3 --only EVAL-01
```

产物位于 `benchmark_sessions/`：`snapshots/build-N.jsonl` 快照、`intersection.md` 交集清单、`DIM_*/session-N.md` 每个 session 的完整对话与阅卷、`SESSIONS_REPORT.md` 逐维稳定性汇总。

## Web 控制台

```bash
python3 web_console/server.py
```

打开 `http://127.0.0.1:8765`。控制台只提供真实流水线运行，不包含模拟运行模式。

## 常用命令

```bash
# 指定输入和反思轮数
python3 run_pipeline.py --input data/input --max-rounds 3

# Step1 校验硬伤时仅告警不中止（默认：带反馈重跑一次，仍不过则中止本轮）
python3 run_pipeline.py --no-strict-step1

# 单独校验现有样本包的切分质量（不调用模型）
python3 validate_sample_packages.py --input data/input --packages sample_packages

# 仅构建题库 / 复用现有题库跑分 / 快速冒烟
python3 run_benchmark.py --build-only
python3 run_benchmark.py --skip-build
python3 run_benchmark.py --limit 3

# 单轮作答模式（默认是多轮对话）
python3 run_benchmark.py --mode single

# 清理所有运行产物（保留 data/input/）
bash clean_artifacts.sh
```

## 数据与安全

- 请仅使用有权处理和分发的领域文档。
- 不要提交 `.env`、`.hermes_home/`、模型响应、运行日志或生成产物。
- 生成的 skill 与 benchmark 应经过领域专家审核后再用于生产环境。
