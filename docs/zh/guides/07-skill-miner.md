# Skill Miner 指南

Skill Miner 是 teamEvolver 内置的文档到技能挖掘子系统，可以将一组同主题领域文档自动挖掘为可执行的 Agent Skill，并同步生成可复跑的评测基准与稳定性报告。

Skill Miner 代码位于 `teamEvolver/skillminer/`，与主服务的桥接层位于 `teamEvolver/proxy/skillminer_bridge.py`，白盒配置由 `teamEvolver/mining_settings.py` 和 `teamEvolver/mining_lifecycle.py` 管理。独立 Web 控制台位于 `teamEvolver/skillminer/web_console/`。

## 核心能力

1. **样本包构建**：按证据视角和上下文容量将输入文档组织成若干样本包。产物经过程序化切分质量校验（切片深度、跨包去重、覆盖率、common/ 一致性等硬指标），有硬伤时自动携带违规明细重跑一次，仍不通过则中止本轮。

2. **语义发现**：从每个样本包归纳可复用的决策单元、流程和边界，并标注证据缺口（GAP）。

3. **Skill 编译**：生成 `SKILL.md` 与配套的 `EVALUATION.md`，并给出置信档与待补缺口清单。

4. **反思环**：在置信档未收敛且仍有补充素材时，携带上一轮缺口进行定向补证（默认最多 3 轮）。

5. **Benchmark 生成**：依据 `EVALUATION.md` 构建题库，支持多轮对话（模拟情境参与者）与单轮作答两种跑分方式，以及难度分布配额。

6. **轨迹 Benchmark 独立挖掘**：直接接收 teamEvolver/SkillGen 或 OpenAI messages 风格轨迹，生成 held-out Benchmark；不进入样本包、语义发现、Skill 编译或 LIFT 流程。

7. **多次构建与稳定性复跑**：把多次构建的题库存为快照、求交集，再对交集项跑多个 session，观察 skill 在多轮对话下的行为稳定性。

8. **覆盖报告**：统计语义单元采纳率、GAP 消解率和维度证据覆盖。

9. **Web 控制台**：提供真实运行、知识补证、跑分和覆盖报告入口。

10. **LIFT 适配**：把 SkillMiner 题库转换为 LIFT Suite v1 与 Markdown 场景，经人工编辑、校验、批准后发布到外部 LIFT 工作区。

## 挖掘流水线

Skill Miner 主流水线包含三个核心 Agent Skill 驱动的步骤，外加反思环和评测阶段：

### Step 1：样本包构建（Sample Package Constructor）

- **Agent Skill**：`sample-package-constructor-agent-skill/`
- **Prompt**：由 `teamEvolver/skillminer/sample_package_constructor_agent_prompt.py` 中的 `SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT` 定义
- **输入**：`data/input/` 目录下的原始 Markdown 文档
- **输出**：`sample_packages/` 目录下的结构化样本包
- **校验**：`validate_sample_packages.py` 执行硬指标校验：
  - 切片深度策略
  - 跨包去重
  - 源文档覆盖率
  - `common/` 共享区一致性

### Step 2：语义发现（Semantic Discovery）

- **Agent Skill**：`semantic-discovery-agent-skill/`
- **Prompt**：由 `teamEvolver/skillminer/semantic_discovery_agent_prompt.py` 中的 `SEMANTIC_DISCOVERY_AGENT_PROMPT` 定义
- **输入**：Step 1 产出的样本包
- **输出**：`semantic_reports/` 目录下各样本包的语义分析报告
- **产出**：可复用决策单元、流程定义、边界条件、证据缺口（GAP）清单

### Step 3：Skill 与评测编译（Evaluation Compiler）

- **Agent Skill**：`evaluation-compiler-agent-skill/`
- **Prompt**：由 `teamEvolver/skillminer/evaluation_compiler_agent_prompt.py` 中的 `EVALUATION_COMPILER_AGENT_PROMPT` 定义
- **输入**：Step 2 的语义分析报告
- **输出**：`compiled_skill/<skill-name>/` 目录
  - `SKILL.md`：可执行的 Agent 技能文件
  - `EVALUATION.md`：评测维度和评分标准
  - 初始 Benchmark 题目

### 反思环（Reflection Loop）

如果 Skill 置信档未达到阈值且仍有补充素材，流水线会携带上一轮的 GAP 清单重新进入 Step 1-3 进行定向补证，默认最多 3 轮。可通过 `mining.pipeline.max_rounds` 配置。

### Benchmark 构建与运行

在 Skill 编译完成后，`run_benchmark.py` 会根据 `EVALUATION.md` 构建基准题库并执行评测：

- **多轮对话模式**（默认）：模拟情境参与者（customer_sim）逐步披露事实，被测 Skill 多轮应对，最后由裁判评分
- **单轮作答模式**：一次性给出完整情境，被测 Skill 单轮回答后裁判评分
- **难度分布**：可配置 easy/medium/hard 比例，默认 `easy:4,medium:7,hard:5`（共 16 题）

## 前置条件

### 运行环境

- Python 3.11～3.13
- 已安装 Hermes 运行时（通过项目安装脚本）：
  ```bash
  bash scripts/install_teamEvolver.sh
  scripts/project_hermes.sh --version
  ```
- 可用的模型 provider，默认对接火山方舟（Volcengine Ark）

### 模型配置

API Key 通过环境变量 `ARK_API_KEY` 注入，不要写入仓库：

```bash
export ARK_API_KEY="your-api-key"
```

首次运行会自动从 `teamEvolver/skillminer/hermes/config.yaml.example` 创建项目本地 Hermes 配置 `.hermes_home/config.yaml`。

项目专用 Hermes 配置与全局 `~/.hermes/config.yaml` 隔离，可独立修改模型：

```bash
# 使用 Hermes 模型选择器（改动只写入项目 HERMES_HOME）
scripts/project_hermes.sh model

# 或直接编辑
${EDITOR:-vi} teamEvolver/skillminer/.hermes_home/config.yaml
```

## 快速开始

### 1. 准备输入文档

将同一主题的 Markdown 文档放入 `teamEvolver/skillminer/data/input/` 目录。

### 2. 静态自检（不调用模型）

```bash
cd teamEvolver/skillminer
python3 test_pipeline_static.py
```

### 3. 运行挖掘流水线

```bash
# 运行一轮挖掘（Step 1-3，无反思环）
python3 run_pipeline.py --input data/input --max-rounds 1

# 完整运行（最多 3 轮反思环）
python3 run_pipeline.py --input data/input --max-rounds 3

# Step1 校验硬伤时仅告警不中止
python3 run_pipeline.py --input data/input --no-strict-step1
```

### 4. 查看产物

```
compiled_skill/<skill-name>/
├── SKILL.md          # 生成的可执行技能
└── EVALUATION.md     # 评测标准
```

### 5. 构建并运行 Benchmark

```bash
# 使用默认难度分布构建并运行
python3 run_benchmark.py

# 指定难度分布和题目数量
python3 run_benchmark.py --difficulty-dist "easy:3,medium:8,hard:7" --target-total 16

# 仅构建题库不运行
python3 run_benchmark.py --build-only

# 复用现有题库跑分
python3 run_benchmark.py --skip-build

# 单轮作答模式
python3 run_benchmark.py --mode single

# 快速冒烟（仅跑 3 题）
python3 run_benchmark.py --limit 3
```

### 6. 生成覆盖报告

```bash
python3 run_coverage_report.py
```

## 轨迹 Benchmark 独立挖掘

除了从文档挖掘 Skill 外，Skill Miner 还支持直接从 Agent 会话轨迹挖掘 held-out Benchmark，此路径不生成 SKILL.md，只产出评测题目。

### HTTP API（teamEvolver 统一服务）

```bash
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

支持三种轨迹格式：
- teamEvolver `turns` 格式
- OpenAI `messages` 格式
- 含 `action`/`observation` 的 `steps`/`events` 格式

服务会自动去重、限制规模并脱敏密钥、手机号、邮箱和本机用户路径。返回 HTTP 202 和 run_id，通过轮询状态接口查询进度：

```bash
GET /api/mining/trajectory-benchmarks/<run_id>
```

完成后产物位于 `trajectory_benchmarks/<run_id>/`：
- `benchmark.jsonl`：teamEvolver-benchmark-v1 格式题库
- `BENCHMARK.md`：人工可读版
- `manifest.json`：来源摘要、难度和维度统计

### Python API

teamEvolver 内部组件可直接调用：

```python
from teamEvolver import amine_benchmark_from_trajectories

result = await amine_benchmark_from_trajectories({
    "dataset_name": "skillgen-evolution",
    "target_total": 18,
    "trajectories": trajectories,
})
```

查询历史产物：

```python
from teamEvolver.skillminer.trajectory_benchmark import (
    list_trajectory_benchmark_runs,
    get_trajectory_benchmark_run,
)
```

## 多次构建与稳定性复跑

单次出题带随机性，推荐多次构建题库并验证稳定性：

```bash
cd teamEvolver/skillminer

# 1) 多次构建题库并存快照
python3 run_benchmark.py --build-only
python3 run_multi_session.py snapshot

# 2) 查看快照与交集概况
python3 run_multi_session.py status

# 3) 求交集（按情境文本相似度，失败则按维度）
python3 run_multi_session.py intersect

# 强制按考核维度求交集
python3 run_multi_session.py intersect --by-dimension

# 4) 对交集项各跑 N 个 session
python3 run_multi_session.py run --sessions 3

# 只重跑特定维度（复用其余结果）
python3 run_multi_session.py run --sessions 3 --only EVAL-01
```

产物位于 `benchmark_sessions/`：
- `snapshots/build-N.jsonl`：每次构建的快照
- `intersection.md`：交集清单
- `DIM_*/session-N.md`：每个 session 的完整对话与阅卷
- `SESSIONS_REPORT.md`：逐维稳定性汇总

## 人工检查点

Skill Miner 运行过程中设置了多个人工审核节点，确保挖掘质量：

1. **Step 1 样本包审核**（可选，由 `strict_step1` 控制）：样本包切分质量不达标时中止，等待人工修正输入文档或调整切分策略
2. **Skill 产出审核**：编译完成后人工检查 SKILL.md 和 EVALUATION.md 的准确性
3. **Benchmark 题目审核**：自动生成的题目需要人工验证情境合理性和 gold 答案准确性
4. **LIFT 发布前审核**：在发布到外部 LIFT 工作区前，必须通过人工审核批准

人工检查点逻辑位于 `teamEvolver/skillminer/human_checkpoints.py`，测试文件为 `test_skillminer_human_checkpoints.py`。

## Web 控制台

### 独立控制台（Skill Miner 原生）

启动独立的 Skill Miner Web 控制台：

```bash
cd teamEvolver/skillminer
python3 web_console/server.py
```

打开 `http://127.0.0.1:8765`。控制台基于标准库实现 SSE 实时日志，只提供真实流水线运行，无模拟模式。

功能包括：
- 上传输入文档
- 启动挖掘任务，实时查看日志输出
- 查看样本包和语义报告
- 下载编译好的 SKILL.md
- 启动 Benchmark 跑分
- 查看覆盖报告
- 知识补证入口

前端代码位于 `teamEvolver/skillminer/web_console/static/`（`index.html`、`app.js`、`styles.css`）。

### 统一控制台（teamEvolver 主服务）

启动 teamEvolver 主服务后，统一控制台中也提供了 Skill Miner 入口：

- 挖掘任务提交和状态监控
- 模型配置（可独立于全局 LLM 配置）
- Pipeline 参数配置
- Prompt 白盒编辑（10 个可编辑 Prompt）
- Benchmark 运行和结果查看
- LIFT 集成审核和发布
- 覆盖报告查看
- 轨迹 Benchmark 提交

## LIFT 集成

Skill Miner 生成 `benchmark.jsonl` 后会自动生成 LIFT 待审核草稿，写入 `lift_datasets/drafts/`。如需禁用：

```bash
export SKILLMINER_LIFT_AUTO_DRAFT=0
```

### 准备 LIFT 环境

```bash
# 在 teamEvolver 仓库根目录
bash scripts/setup_lift.sh

# 可选：同时创建 Python 3.12 虚拟环境
bash scripts/setup_lift.sh --install-deps
export TEAMEVOLVER_LIFT_PYTHON="$PWD/external/LIFT/.venv-teamEvolver/bin/python"
```

或复用已有 LIFT checkout：

```bash
export TEAMEVOLVER_LIFT_ROOT=/absolute/path/to/LIFT
export TEAMEVOLVER_LIFT_PYTHON=/absolute/path/to/python
```

### 发布流程

1. 在统一控制台进入评测中心
2. 从已生成 Benchmark 的 Skill 创建 LIFT 草稿
3. 逐题检查并编辑 `query`、内容要求和轨迹要求，确认 warmup/holdout 划分
4. 保存并通过结构校验，点击"人工审核通过"
5. 点击"发布到 LIFT"
6. 选择 runtime 并启动，实时查看运行日志

发布后数据结构：

```
<LIFT>/assets/benchmarks/teamEvolver/<suite>.json
<LIFT>/assets/benchmark_mds/teamEvolver/<suite>/
├── train/q*/q*.md
├── test/q*/q*.md
└── skills/<skill>/SKILL.md
```

## 可用的 Miner Agent Skills

Skill Miner 包含三个内置的挖掘 Agent Skill，位于 `teamEvolver/skillminer/` 下：

### evaluation-compiler

- **目录**：`evaluation-compiler-agent-skill/`
- **用途**：将语义发现报告编译为 SKILL.md 和 EVALUATION.md
- **资产**：
  - `assets/evaluation_template.md`：EVALUATION.md 模板
  - `assets/skill_template.md`：SKILL.md 模板

### sample-package-constructor

- **目录**：`sample-package-constructor-agent-skill/`
- **用途**：将原始文档切分为符合质量标准的样本包
- **资产**：
  - `assets/coverage_report_template.md`：覆盖报告模板
  - `assets/mirror-config.example.yaml`：镜像配置示例
  - `assets/package_index_template.md`：包索引模板
  - `assets/package_note_template.md`：包备注模板
- **参考文档**：
  - `references/global-coverage-policy.md`：全局覆盖策略
  - `references/output-folder-spec.md`：输出目录规范
  - `references/package-count-decision.md`：包数量决策
  - `references/selection-priority.md`：选择优先级
  - `references/single-file-subsetting-policy.md`：单文件切分策略
  - `references/slice-depth-policy.md`：切片深度策略
  - `references/source-coverage-policy.md`：源文档覆盖策略
- **脚本**：`scripts/README.md`

### semantic-discovery

- **目录**：`semantic-discovery-agent-skill/`
- **用途**：从样本包中挖掘语义单元、决策流程和知识缺口
- **资产**：
  - `assets/judgment_structure_template.md`：判定结构模板
  - `assets/rebuttal_template.md`：反驳模板
  - `assets/report_filename_examples.md`：报告命名示例
  - `assets/report_template.md`：报告模板
  - `assets/semantic_role_template.md`：语义角色模板
- **参考文档**：
  - `references/common-false-structures.md`：常见伪结构
  - `references/evidence-priority.md`：证据优先级
  - `references/input-output-spec.md`：输入输出规范
  - `references/judgment-structure-screening.md`：判定结构筛选
  - `references/local-vs-global-evidence-policy.md`：局部 vs 全局证据策略
  - `references/output-contract.md`：输出契约
  - `references/semantic-role-screening.md`：语义角色筛选
- **脚本**：`scripts/README.md`

## 配置（mining 节）

Skill Miner 的配置位于 config.yaml 的 `mining` 节，可通过控制台白盒面板动态修改。

### 模型配置（mining.model）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider` | string | 继承全局 | 模型提供商 |
| `model_id` | string | 继承全局 | 模型 ID |
| `base_url` | string | 继承全局 | API Base URL |
| `api_key` | string | 继承全局 | API Key |
| `max_tokens` | integer | `100000` | 最大输出 token |
| `context_length` | integer | `240000` | 上下文窗口大小 |
| `temperature` | float | `0.2` | 采样温度 |

设为空对象 `{}` 则继承全局 `llm` 配置。

### Pipeline 配置（mining.pipeline）

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_rounds` | integer | `3` | 反思环最大轮次（1-20） |
| `max_retries` | integer | `2` | 单步最大重试次数（0-20） |
| `retry_backoff_seconds` | float | `0.8` | 重试退避时间（0-300 秒） |
| `oneshot_timeout_seconds` | integer | `1800` | 单次挖掘超时（30-86400 秒，默认 30 分钟） |
| `step1_validation_retries` | integer | `1` | Step1 校验失败重试次数（0-10） |
| `strict_step1` | boolean | `true` | Step1 校验失败是否中止本轮 |
| `benchmark_target_total` | integer | `16` | Benchmark 目标题目总数（1-500） |
| `benchmark_difficulty_dist` | string | `"easy:4,medium:7,hard:5"` | 难度分布 |
| `benchmark_max_turns` | integer | `5` | 多轮 Benchmark 最大对话轮次（1-50） |

### Prompts 配置（mining.prompts）

共 10 个可编辑 Prompt：

| Prompt ID | 说明 |
|-----------|------|
| `sample_package` | 样本包构建 |
| `semantic_discovery` | 语义发现 |
| `evaluation_compiler` | Skill 与评测编译 |
| `benchmark_generation` | Benchmark 出题 |
| `benchmark_usage` | Benchmark 单轮作答 |
| `benchmark_participant` | Benchmark 模拟参与者 |
| `benchmark_skill_reply` | Benchmark 被测 Skill 回复 |
| `benchmark_judge_single` | Benchmark 单轮裁判 |
| `benchmark_judge_dialogue` | Benchmark 多轮裁判 |
| `trajectory_benchmark_generation` | 轨迹 Benchmark 挖掘 |

前三个 Prompt 从对应的 `*_agent_prompt.py` 文件加载默认值，后七个为动态生成默认 Prompt。在控制台中修改后保存到 `mining.prompts` 配置节。

## 产物目录结构

运行时生成的产物目录默认被 Git 忽略：

```
teamEvolver/skillminer/
├── data/input/              # 待挖掘的领域文档（用户提供）
├── sample_packages/         # Step 1 产物
├── semantic_reports/        # Step 2 产物
├── compiled_skill/          # Step 3 产物：SKILL.md、EVALUATION.md
├── reflection_rounds/       # 反思环各轮中间产物
├── run_history/             # 新任务启动时保存的上一批生成物
├── benchmark_sessions/      # 多次构建快照、交集、稳定性复跑
├── trajectory_benchmarks/   # 轨迹 Benchmark 产物
├── lift_datasets/           # LIFT 待审核草稿和已发布快照
├── logs/                    # 运行日志
└── .hermes_home/            # 项目 Hermes 独立配置（不应提交）
```

清理运行产物（保留 data/input/ 和 .hermes_home/）：

```bash
bash clean_artifacts.sh
```

删除 `.hermes_home/` 后，下次运行会从 `hermes/config.yaml.example` 重新初始化。

## 桥接层

`teamEvolver/proxy/skillminer_bridge.py` 提供主服务与 Skill Miner 之间的 HTTP API 桥接，包括：

- 挖掘任务提交和状态查询
- 轨迹 Benchmark 接口
- LIFT 集成接口
- 产物列表和下载
- 白盒配置读写

主服务的 `/api/mining/*` 路由通过此桥接层调用 `teamEvolver/mining_lifecycle.py` 中的生命周期管理函数。

## 数据安全

- 仅使用有权处理和分发的领域文档
- 不要提交 `.env`、`.hermes_home/`、模型响应、运行日志或生成产物到版本控制
- 生成的 Skill 与 Benchmark 应经过领域专家审核后再用于生产环境
- 轨迹 Benchmark 挖掘会自动脱敏密钥、手机号、邮箱和本机用户路径；源轨迹原文不落盘，`manifest.json` 只保留 ID、数量和 SHA-256 摘要
