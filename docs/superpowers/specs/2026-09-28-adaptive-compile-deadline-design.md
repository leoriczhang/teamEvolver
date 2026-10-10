# 自适应编译墙钟（Adaptive Compile Deadline）设计

- 日期：2026-09-28
- 状态：已批准（用户确认）；spec 审查问题已修正
- 影响范围：`team_ontology`（TE 侧），不改 OpenViking
- 关联问题：任务 `ont_dde095f84a6109dfabe26ee3`（submission_key `9d742adb-c058-402d-abf3-d64323d5f406`）`COMPILE_DEADLINE_EXCEEDED` 失败分析

## 1. 背景与问题

SIT 环境 Ontology 工作台创建的构建任务因固定超时 `compile_timeout_seconds: 1800`（30 分钟）失败。实测证据（trace `78db812d26b59e9ba5ad30f241f14098`）：

- OV compile agent 全程正常工作，无错误无重试；时间耗在单次抽取生成调用上：3 个文档（15,033 字符）一次生成 94,293 output tokens，`glm-5.3-flash` 经模型网关实际吞吐 ≈ 60 token/s，该调用耗时 26.3 分钟。
- 成功对照（09-24，单文档）总耗时 24.2 分钟——固定 30 分钟预算在输入接近零时就已经贴线。
- 测得编译耗时模型：`抽取 output ≈ 60,000 + 2.3 × 字符数`（tokens），即存在 ~60K token 的固定生成地板；`耗时 ≈ 1,000 + 0.038 × 字符数`（秒）。

结论：编译时长由 `输出 token ÷ ~60 tok/s` 的物理规律决定，随输入规模无界增长，固定墙钟必然在某个规模之上误杀正常任务。

## 2. 目标与非目标

**目标**

1. 任意大小输入（直至 TE 现有硬上限：1000 文件 / 20MB / 单文件 2MB）不因固定超时失败。
2. 保留挂死检测能力（单元输入有界 ⇒ 墙钟可预测）。
3. 创建构建时向用户展示预测时长（知情同意，不是拦截）。
4. 轮询接口提供进度与截止信息。

**非目标（明确不做）**

- 不改 OV 侧任何行为（80K 字符打包、`source_concurrency=6`、merge 并发 10 等保持不变）。
- 不做 TE 侧预先分批 / 多次 compile 编排——保持单次 OV compile、现有流程时序（一次编译抽完全部 → 人工确认 schema → 审核 → 发布）。
- 不加 TE 侧总字符数上限或告警拦截。
- 不做运行中任务的暂停/续跑。

**已接受的已知风险**

- 单次 compile 架构下，任何失败（超时、OV 重启如 `BOT_RESTARTED`、模型错误）导致整编重跑。100 批次规模跑到后期失败需全部重来。
- agent 阶段无子进度信号（OV 任务记录只在阶段切换时更新：`collecting_context → agent → rendering → writing/committing`；草稿留在 OV 容器本地 sandbox，不上传资源树）。UI 只能展示"已运行 X / 预计 Y"。
- 预测模型基于 2 个实测数据点标定，外推到大批次数有不确定性；靠第 7 节的标定回流逐步修正。

## 3. 机制设计

### 3.1 批次数预测（复刻 OV 打包）

TE 在 `stage_inputs` 冻结来源时已知每个文件的精确字符数（`characters`，见 `compile_flow.py` 冻结逻辑）。按 OV `pack_source_batches` 的语义估算批次数：

```
对每个文件按暂存顺序：
  字符数 > 80_000 的文件先按 80K 上限切成若干段（对齐 OV split_source 的粗粒度近似）
  段按顺序贪心装入当前批次，装不下则开新批次
N = 最终批次数
```

这是**估算**而非契约——与 OV 实际打包允许有偏差（OV 的 split 按标题/段落偏好切分），偏差只影响 deadline 的宽裕度，不影响正确性。

### 3.2 自适应墙钟公式

```
deadline_seconds = safety_factor × ( base_seconds
                                   + ceil(N / 6)  × wave_seconds
                                   + ceil(N / 10) × merge_wave_seconds )
```

- `ceil(N/6)`：OV 抽取子 agent 并发 6，批次按波次执行。
- `ceil(N/10)`：merge 子 agent 并发 10。
- 常数来源：`wave_seconds = 4500`（单满批 80K 字符抽取实测 ~4,100s + 轮次开销）；`merge_wave_seconds = 1800`（每个合并波次：单 merge child 输入上限 60K 字符、生成量与输入同量级、按 10 并发折算到波次）；`base_seconds = 1800`（固定地板 ~1,000s + 父流程 ~500s + 零头）；`safety_factor = 1.5`（吞吐波动 50-60 tok/s）。

按上述常数标定（公式精确输出）：

| N | 总字符（约，按 2.5 B/字） | deadline_seconds | 时长 |
|---|---|---|---|
| 1 | ≤80K | 12,150 | ~3.4h |
| 6 | ~480K | 12,150 | ~3.4h |
| 36 | ~2.9M | 54,000 | 15h |
| 100 | ~8M | 144,450 | ~40h |

注：批次数取决于语料的字节/字符比。20MB 纯 ASCII 语料 ≈ 20M 字符 ≈ 250 批 → deadline ≈ 353,700s ≈ 98h——公式同样覆盖，不加拦截。

### 3.3 poll 阶段改造

现有检查（`compile_flow.py` poll 阶段）：

```python
if time.time() - result["submitted_at"] > self.cfg.compile_timeout_seconds:
    await self.cancel_remote(...)
    invalid("COMPILE_DEADLINE_EXCEEDED")
```

改为读取 `result["deadline_seconds"]`（提交时算好并随 job 持久化）。错误码 `COMPILE_DEADLINE_EXCEEDED` 与取消远端任务的行为**保持不变**——自适应后它只在真异常（预测墙钟内没跑完）时触发。

**重算规则**：`deadline_seconds` 在**每次 stage_inputs → submit 转换时重算并覆盖**（不是"缺失才补"）。原因：repair rounds（`new_round` / `confirm` 带受影响来源子集）和 `retry` 都会回到 `stage_inputs` 并重置 `submitted_at`，此时 `selected_refs` 可能只是子集——沿用 round-0 的旧 deadline 会错。`compile_flow.py` 现有的提交转换字段清理逻辑同步覆盖该字段。

### 3.4 兼容性

`compile_timeout_seconds` 配置项移除。已有 job 记录里没有 `deadline_seconds` 字段的（升级前创建、升级后仍在 poll 的存量任务），回退用旧固定值 1800——存量任务本来也只可能在旧预算内存活。

## 4. 配置

`config/config_sit.yaml`、`config/config.yaml`、`config/config_prd.yaml` 及 `team_ontology/config.py`：

```yaml
ontology:
  # 替换 compile_timeout_seconds
  compile_deadline:
    base_seconds: 1800        # 固定成本：抽取地板 + 父流程 + 零头
    wave_seconds: 4500        # 每个抽取波次（6 批并行，单满批实测 ~4100s）
    merge_wave_seconds: 1800  # 每个合并波次（10 并发）
    safety_factor: 1.5        # 吞吐波动余量
```

`team_ontology/config.py` 的 `OntologyConfig` 增加嵌套配置组（dataclass），删除 `compile_timeout_seconds` 字段。

**部署耦合**：`OntologyConfig.from_host` 以 `cls(**raw)` 构造——若部署实例仍挂载含 `compile_timeout_seconds` 的旧 yaml 而代码已删字段，启动即 TypeError 崩溃。`from_host` 必须兼容处理：忽略 legacy `compile_timeout_seconds` 键，并把扁平/嵌套的 `compile_deadline` 构造成嵌套组；部署时同步更新挂载的 yaml 并重启（与 `.env_sit` 既有约定一致）。

## 5. API 变化

### 5.1 创建构建 `POST /te/enterprise/v1/jobs`

前置事实：该端点要求集合已处于 `sources_ready`（来源已全部冻结，`api.py` create handler），因此**创建时即可计算预测**：

- **新集合（任意类型）**：roots 文件的精确 `characters` 在冻结时已持久化；references 的字符总数在集合收集的初始步骤持久化为 `reference_characters`（该步骤本就读取全部 reference 冻结文本做尺寸校验，零额外 OV 调用）→ POST 响应给出**精确**预测，`estimated: false`。
- **存量集合**（升级前创建、无 `reference_characters` 字段且请求含 references）：用 `chars ≈ bytes / 2.5` 估算（实测本语料 bytes/char 稳定在 2.48-2.57），`estimated: true`。
- `stage_inputs → submit` 转换处仍按实际 `source_map`（repair 轮可能为来源子集）重算并覆盖为精确值。

`predicted` 的写入语义（实现定稿）：**单一权威写入点在 `stage_inputs → submit` 转换**（按当轮 `source_map` 精确计算并持久化，repair 子集天然正确）；`POST /jobs` 响应注入**ephemeral 预览**——仅在响应中尚无持久化块时（幂等重放过 submit 的任务时持久化块胜出），不落库。因此创建与 submit 之间（stage_inputs 窗口内）的 GET 不带 `predicted`；POST 与 GET 在 submit 之后同构。响应块形状：

```json
{
  "result": {
    "predicted": {
      "batches": 36,
      "total_characters": 2900000,
      "duration_seconds": 54000,
      "note": "预计编译 ~15 小时",
      "estimated": false
    }
  }
}
```

`total_characters` 使标定数据集（第 7 节）完全可通过 API 获取，无需直查数据库。

### 5.2 轮询 `GET /te/enterprise/v1/jobs/{job_id}`

`predicted` 对象（含 `batches`、`duration_seconds`、`estimated`）持久化在 `result` 中：submit 转换写入精确值（`estimated: false`，含 references 型——`reference_characters` 使创建预览也精确，仅存量集合的 POST 预览为估算）。`result` 另增加：

- `deadline_seconds: int`（与 `predicted.duration_seconds` 同值）
- `deadline_at: ISO8601`（`submitted_at + deadline_seconds`，随 `deadline_seconds` 重算同步更新）
- `elapsed_seconds: int`（服务端计算 `now − submitted_at`，仅运行中有意义；终态任务不返回该字段，UI 用既有时间戳）

`public_job` 是黑名单裁剪（仅剔除 `grant`/`payload`/`source_map`/`previous_output`），新字段自动放行、无需改动；api.py 的实际工作量在 POST handler 组装 `predicted` 块。状态机与既有语义不变（`running → schema_review → review_ready → publishing → published / failed / cancelled`）。

**缓存命中路径注**：`compile_model_revision` 非空时 `build()` 可从 `stage_inputs` 直接跳到 `schema_review` 而不经过 submit（跨任务缓存命中），此时 `deadline_seconds` 与 `predicted` 均不会持久化（仅 POST 响应携带 ephemeral 预览）——无害（deadline 只对运行中任务有意义），当前所有环境 `compile_model_revision: ""` 该路径未启用。

## 6. 失败与重试语义

与现状完全一致：超时 → `cancel_remote` → job `failed / COMPILE_DEADLINE_EXCEEDED` → 用户走现有 `POST /jobs/{id}/retry` 重跑整个编译（round 递增，复用现有 repair 机制）。不做自动重试。

## 7. 标定数据回流

**触发方式**：用户提出"根据标定数据修正本体构建时间估算数据"（或同义说法）即执行本节流程；本节自包含执行所需的全部判据。

每次完成的编译在 job 记录中已有 `token_usage`、`submitted_at`、`compile_completed_at`；本设计新增持久化的 `predicted`（含 `total_characters`、`batches`）与 `deadline_seconds`，全部经 `public_job` 透出，标定数据集**纯 API 可取**。

**修正流程（人工离线，常数不自动调整）**：deadline 是安全边界而非优化目标，失败模式不对称（偏大几乎无害、偏小即误杀），因此常数只人工修正。判据：

1. 按 `predicted.batches` 分桶，计算每桶 `实际耗时 ÷ deadline_seconds` 的 p50/p90/max（实际耗时 = `compile_completed_at − submitted_at`，只取 completed 样本）。
2. 某桶 p90 > 0.7 → 上调常数：偏差随 N 增长 → 调 `wave_seconds`（或 `merge_wave_seconds`，看斜率对应的并发段）；各桶普遍偏高 → 调 `base_seconds` 或 `safety_factor`。
3. 某桶 p90 < 0.2 且样本 ≥ 5 → 才允许下调对应常数。
4. 桶内样本 < 3 → 数据不足，维持现值并明说。
5. 常数变更同步更新：`config/config*.yaml` + 本文档 §3.2 标定表，按部署约定挂载重启。

## 8. 测试

1. **打包估算单测**：顺序贪心、80K 上限、单文件超 80K 切段、空文件；与 OV `pack_source_batches` 行为对齐的对照用例（同输入同批次数，允许段切分近似差异）。
2. **deadline 公式单测**：N=1/6/7/36/100/250 边界（含上表精确值断言）；safety_factor 应用；常数可配置。
3. **重算规则测试**：repair round / retry 回到 stage_inputs 后 `deadline_seconds` 按当前 `selected_refs` 重算，不沿用旧值。
4. **poll 超时集成测试**（假 OV transport，沿用 `tests/test_ontology_compile.py` 的 `httpx.MockTransport` 模式）：`deadline_seconds` 持久化、超时触发 cancel + `COMPILE_DEADLINE_EXCEEDED`、未超时继续轮询；存量无 `deadline_seconds` 字段回退 1800；`from_host` 对 legacy `compile_timeout_seconds` 键的兼容。
5. **API 契约测试**：POST 响应含 `predicted`（roots 型 `estimated:false`；references 型 `estimated:true`）、GET 响应含进度字段与修正后的 `predicted`、`public_job` 既有裁剪不受影响。

## 9. 实现落点（供计划阶段展开）

- `team_ontology/config.py`：`CompileDeadlineConfig` 嵌套组 + `from_host` legacy 键兼容。
- `team_ontology/compile_flow.py`：
  - 打包估算纯函数（输入：`[(uri, characters)]`；输出：批次数）；
  - deadline 计算纯函数（输入：N + 配置；输出：秒）；
  - **stage_inputs → submit 转换处**（`compile_flow.py` 设置 `submitted_at` 的位置，约 481-505 行——注意这与既有的 publication 操作 `POST /jobs/{id}/prepare`（OV `/assets/prepare`，`review_ready → prepared` 链路）无关联，仅指 compile 流程内部的阶段名）计算并持久化 `deadline_seconds` 与 `predicted`；
  - poll 阶段改读 job 内 `deadline_seconds`，存量字段缺失回退 1800。
- `team_ontology/api.py`：POST handler 组装 `predicted` 响应块（读取集合 `files[].characters` 或聚合 `bytes`）。
- `config/config*.yaml`：配置替换。
- 测试：`tests/` 下新增对应单测/集成测试。
