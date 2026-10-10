# 自适应编译墙钟（Adaptive Compile Deadline）实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用按预测工作量自适应的编译墙钟替换固定 30 分钟超时（`compile_timeout_seconds: 1800`），使任意大小输入不再因固定超时失败，并在创建/轮询 API 中提供预测与进度信息。

**Architecture:** TE 在 `stage_inputs → submit` 转换处按每文件精确字符数复刻 OV 的 80K 贪心打包估出批次数 N，用 `deadline = safety × (base + ceil(N/6)×wave + ceil(N/10)×merge_wave)` 算出墙钟并随 job 持久化；poll 阶段改读 job 内字段（存量任务回退 1800）。创建构建时从 `sources_ready` 集合计算 `predicted` 块（新集合精确——含 `reference_characters`；存量 references 集合用 bytes/2.5 估算）。OV 侧零改动。

**Tech Stack:** Python 3.12 / FastAPI / pytest（`.venv/bin/python -m pytest`，本仓库 venv 已装 pytest+pytest-asyncio）。

**Spec:** `docs/superpowers/specs/2026-09-28-adaptive-compile-deadline-design.md`（含 §3.2 公式常数与 §7 标定判据——实现时如与 spec 冲突以 spec 为准并回报）。

**测试基线:** `tests/test_ontology_compile.py` 当前 81 passed（改动后必须保持全绿）。

---

## Chunk 1: 全部任务

### Task 1: `CompileDeadlineConfig` 配置组 + `from_host` 兼容

**Files:**
- Modify: `team_ontology/config.py`
- Create: `tests/test_ontology_deadline.py`

- [ ] **Step 1: 写失败测试（新文件 `tests/test_ontology_deadline.py`）**

```python
"""Adaptive compile deadline: packing estimator, formula, config compatibility, prediction helpers."""

import time

from team_ontology.config import CompileDeadlineConfig, OntologyConfig


class Host:
    ontology = {}


def test_from_host_tolerates_legacy_timeout_key():
    class LegacyHost:
        ontology = {"compile_timeout_seconds": 1800, "compile_deadline": {"wave_seconds": 5000}}

    config = OntologyConfig.from_host(LegacyHost())
    assert config.compile_deadline.wave_seconds == 5000
    assert config.compile_deadline.base_seconds == 1800


def test_from_host_constructs_nested_group_from_dict():
    class YamlHost:
        ontology = {
            "compile_deadline": {
                "base_seconds": 900,
                "wave_seconds": 4500,
                "merge_wave_seconds": 1800,
                "safety_factor": 2.0,
            }
        }

    config = OntologyConfig.from_host(YamlHost())
    assert config.compile_deadline == CompileDeadlineConfig(900, 4500, 1800, 2.0)


def test_from_host_defaults_when_absent():
    config = OntologyConfig.from_host(Host())
    assert config.compile_deadline == CompileDeadlineConfig()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: FAIL — `ImportError: cannot import name 'CompileDeadlineConfig'`

- [ ] **Step 3: 实现（`team_ontology/config.py`）**

在 `OntologyConfig` 类定义**之前**新增配置组：

```python
@dataclass
class CompileDeadlineConfig:
    """Adaptive compile wall clock; constants and rationale in
    docs/superpowers/specs/2026-09-28-adaptive-compile-deadline-design.md §3.2/§4."""

    base_seconds: int = 1800
    wave_seconds: int = 4500
    merge_wave_seconds: int = 1800
    safety_factor: float = 1.5
```

将 `OntologyConfig` 中的 `compile_timeout_seconds: int = 1800`（约第 21 行）**替换为以下四行**（字段暂时保留并标记废弃——Task 5 与 poll 行替换原子化删除，保证中间提交全绿）：

```python
    # Deprecated: consumed only by the poll-stage check until that switches to the
    # per-job adaptive deadline; removed together with that switch (Task 5).
    compile_timeout_seconds: int = 1800
    compile_deadline: CompileDeadlineConfig = field(default_factory=CompileDeadlineConfig)
```

在 `from_host` 中、`result = cls(**raw)` 之前插入：

```python
        # compile_timeout_seconds was replaced by the adaptive compile_deadline group;
        # mounted yamls may still carry the legacy key across a rolling deploy.
        raw.pop("compile_timeout_seconds", None)
        if isinstance(raw.get("compile_deadline"), dict):
            raw["compile_deadline"] = CompileDeadlineConfig(**raw["compile_deadline"])
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: `3 passed`

- [ ] **Step 5: 确认现有测试不受影响**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py -q`
Expected: `81 passed`（现有测试用默认 `OntologyConfig(state_dir=...)`，不传被改动的字段）

- [ ] **Step 6: 提交**

```bash
git add team_ontology/config.py tests/test_ontology_deadline.py
git commit -m "feat(ontology): add CompileDeadlineConfig replacing fixed timeout key"
```

---

### Task 2: 打包估算与 deadline 纯函数

**Files:**
- Modify: `team_ontology/compile_flow.py`（模块常量区，`SUFFIXES` 之后）
- Test: `tests/test_ontology_deadline.py`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_deadline.py`）**

```python
import pytest

from team_ontology.compile_flow import (
    BATCH_CHAR_LIMIT,
    compile_deadline_seconds,
    estimate_batches,
    predicted_block,
)


@pytest.mark.parametrize(
    "characters,expected",
    [
        ([], 1),
        ([0], 1),
        ([80_000], 1),
        ([40_000, 40_000], 1),
        ([40_000, 50_000], 2),
        ([80_000, 1], 2),
        ([160_000], 2),
        ([80_000] * 6, 6),
        ([80_000] * 7, 7),
        ([10_000] * 8, 1),
        ([30_000] * 3, 2),
    ],
)
def test_estimate_batches(characters, expected):
    assert estimate_batches(characters) == expected


def test_batch_char_limit_matches_ov():
    assert BATCH_CHAR_LIMIT == 80_000


@pytest.mark.parametrize(
    "batches,expected",
    [(1, 12_150), (6, 12_150), (7, 18_900), (36, 54_000), (100, 144_450), (250, 353_700)],
)
def test_compile_deadline_seconds(batches, expected):
    assert compile_deadline_seconds(batches, CompileDeadlineConfig()) == expected


def test_compile_deadline_seconds_honors_configured_constants():
    config = CompileDeadlineConfig(base_seconds=900, wave_seconds=4500, merge_wave_seconds=1800, safety_factor=2.0)
    assert compile_deadline_seconds(36, config) == 70_200  # 2.0 * (900 + 6*4500 + 4*1800)


def test_predicted_block_formats_hours_and_minutes():
    hours = predicted_block(36, 2_900_000, 54_000, False)
    assert hours["note"] == "预计编译 ~15 小时"
    assert hours == {
        "batches": 36,
        "total_characters": 2_900_000,
        "duration_seconds": 54_000,
        "note": "预计编译 ~15 小时",
        "estimated": False,
    }
    minutes = predicted_block(1, 100, 600, True)
    assert minutes["note"] == "预计编译 ~10 分钟"
    assert minutes["estimated"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: FAIL — `ImportError: cannot import name 'BATCH_CHAR_LIMIT'`

- [ ] **Step 3: 实现（`team_ontology/compile_flow.py`，`SUFFIXES = {...}` 之后追加）**

```python
# OV CompileLimits mirrors: sources pack into batches of at most 80K characters;
# extraction subagents run 6 per wave and merge children 10 per wave. TE only
# mirrors these to predict a wall clock; OV remains the source of truth.
BATCH_CHAR_LIMIT = 80_000
EXTRACT_CONCURRENCY = 6
MERGE_CONCURRENCY = 10


def estimate_batches(characters):
    """Estimate OV pack_source_batches' batch count from per-file character counts.

    Greedy fill in input order, mirroring OV: an over-limit file splits into
    <=BATCH_CHAR_LIMIT chunks (OV prefers heading/paragraph boundaries; chunk
    boundaries only shift edge cases), empty files keep one range. This sizes a
    deadline; it is never a correctness contract.
    """
    batches, size = 1, 0
    for count in characters:
        remaining = max(count, 1)
        while remaining > 0:
            chunk = min(BATCH_CHAR_LIMIT, remaining)
            if size + chunk > BATCH_CHAR_LIMIT:
                batches, size = batches + 1, 0
            size += chunk
            remaining -= chunk
    return batches


def compile_deadline_seconds(batches, config):
    """Predicted wall clock: fixed cost plus one term per extraction/merge wave."""
    waves = -(-batches // EXTRACT_CONCURRENCY)
    merge_waves = -(-batches // MERGE_CONCURRENCY)
    return int(
        config.safety_factor
        * (config.base_seconds + waves * config.wave_seconds + merge_waves * config.merge_wave_seconds)
    )


def predicted_block(batches, total_characters, deadline, estimated):
    """Assemble the result.predicted payload shared by creation and submit paths."""
    note = (
        f"预计编译 ~{deadline // 3600} 小时"
        if deadline >= 3600
        else f"预计编译 ~{max(1, deadline // 60)} 分钟"
    )
    return {
        "batches": batches,
        "total_characters": total_characters,
        "duration_seconds": deadline,
        "note": note,
        "estimated": estimated,
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: `23 passed`（3 个 Task 1 测试 + 本任务 20 个：11 个打包参数 + 1 个上限 + 6 个公式参数 + 1 个常数可配置 + 1 个 predicted_block；红步的收集错误会让文件整体报错，不必解读红步数量）

- [ ] **Step 5: 提交**

```bash
git add team_ontology/compile_flow.py tests/test_ontology_deadline.py
git commit -m "feat(ontology): batch packing estimator and adaptive deadline formula"
```

---

### Task 3: 集合收集时持久化 `reference_characters`

**Files:**
- Modify: `team_ontology/compile_flow.py`（`sources()` 初始块，锚点：`for source in await self.frozen(p, result["refs"]) if result["refs"] else []`，约第 147 行）
- Test: `tests/test_ontology_compile.py`（复用 `env`/`drive` fixtures）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_compile.py`，需在文件头部 import 区加 `import time`——本任务先不加，Task 5 再加）**

```python
@pytest.mark.asyncio
async def test_reference_collection_persists_reference_characters(env):
    ops, flow, remote = env
    remote.frozen["wiki_preexisting"] = {
        "source_id": "wiki_preexisting",
        "revision": "rev-1",
        "text": "签收于 2026-09-01，由上海转运中心发出 " * 5,
        "readers": ["frank"],
        "origin_uri": "viking://resources/wiki/elsewhere.md",
    }
    row = await ops.store.submit(
        P,
        "refs",
        {
            "contract": SOURCE_CONTRACT,
            "roots": [],
            "readers": ["frank"],
            "references": [{"source_id": "wiki_preexisting", "revision": "rev-1", "digest": "unused"}],
        },
    )
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "sources_ready", row
    assert row["result"]["reference_characters"] == len(remote.frozen["wiki_preexisting"]["text"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py::test_reference_collection_persists_reference_characters -q`
Expected: FAIL — `KeyError: 'reference_characters'`

- [ ] **Step 3: 实现**

把 `sources()` 初始块中的：

```python
            for source in await self.frozen(p, result["refs"]) if result["refs"] else []:
                self.size(result, source["text"])
            return await self.save(job, result)
```

替换为：

```python
            if result["refs"]:
                references = await self.frozen(p, result["refs"])
                for source in references:
                    self.size(result, source["text"])
                # Reference texts are read here anyway; persist their character count
                # so build-time duration predictions are exact without extra OV calls.
                result["reference_characters"] = sum(len(source["text"]) for source in references)
            return await self.save(job, result)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py -q`
Expected: `82 passed`

- [ ] **Step 5: 提交**

```bash
git add team_ontology/compile_flow.py tests/test_ontology_compile.py
git commit -m "feat(ontology): persist reference_characters when collecting sources"
```

---

### Task 4: submit 转换处计算并持久化 `predicted` / `deadline_seconds` / `deadline_at`

**Files:**
- Modify: `team_ontology/compile_flow.py`（`build()` 的 stage_inputs 尾部 `result.update(stage="submit", ...)`，约第 493-505 行；文件头部 import 区）
- Test: `tests/test_ontology_compile.py`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_compile.py`）**

```python
@pytest.mark.asyncio
async def test_submit_transition_persists_predicted_deadline(env):
    ops, flow, remote = env
    row = await build(env)
    assert row["state"] == "schema_review", row
    predicted = row["result"]["predicted"]
    # Fixture corpus: index.md is 126,000 chars, body.md is 20 chars.
    # Greedy packing: 80,000 + 46,020 -> 2 batches -> deadline 1.5*(1800+4500+1800).
    expected_chars = len(remote.files["viking://resources/wiki/index.md"]) + len(
        remote.files["viking://resources/wiki/sub/body.md"]
    )
    assert predicted == {
        "batches": 2,
        "total_characters": expected_chars,
        "duration_seconds": 12_150,
        "note": "预计编译 ~3 小时",
        "estimated": False,
    }
    assert row["result"]["deadline_seconds"] == 12_150
    assert row["result"]["deadline_at"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py::test_submit_transition_persists_predicted_deadline -q`
Expected: FAIL — `KeyError: 'predicted'`

- [ ] **Step 3: 实现**

`team_ontology/compile_flow.py` 头部 import 区加：

```python
from datetime import datetime, timezone
```

把 `build()` 中 stage_inputs 尾部的 `result.update(...)`（锚点：`result.update(stage="submit", target=output_root, submitted_at=time.time(), ...)`）替换为：

```python
            characters = [s["characters"] for s in result["source_map"]]
            batches = estimate_batches(characters)
            deadline = compile_deadline_seconds(batches, self.cfg.compile_deadline)
            submitted = time.time()
            result.update(
                stage="submit",
                target=output_root,
                submitted_at=submitted,
                predicted=predicted_block(batches, sum(characters), deadline, False),
                deadline_seconds=deadline,
                deadline_at=datetime.fromtimestamp(submitted + deadline, timezone.utc).isoformat(),
                payload={
                    "from": [canonical_inputs.get("uri", input_root).rstrip("/")],
                    "to": canonical_target.get("uri", output_root).rstrip("/"),
                    "skill": canonical_skill.get("uri", skill_uri).rstrip("/"),
                    "instruction": json.dumps(instruction, ensure_ascii=False),
                },
                connection={k: v for k, v in (await self.ops.resolve(p)).items() if k in {"url", "account"}},
            )
```

（`payload`/`connection` 内容与原代码逐字一致，只是挪进同一个 `update`。）

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py -q`
Expected: `83 passed`

- [ ] **Step 5: 提交**

```bash
git add team_ontology/compile_flow.py tests/test_ontology_compile.py
git commit -m "feat(ontology): persist predicted duration and adaptive deadline at submit"
```

---

### Task 5: poll 阶段改读 job 内 `deadline_seconds` + 删除 legacy 字段

**Files:**
- Modify: `team_ontology/compile_flow.py`（poll 阶段超时检查，约第 541 行）
- Modify: `team_ontology/config.py`（删除 `compile_timeout_seconds` 字段——与本任务的行为切换**原子化**，保证每个中间提交全绿）
- Test: `tests/test_ontology_compile.py`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_compile.py`；同时在文件头部 import 区加 `import time`）**

先加一个驱动到指定 stage 的辅助函数（放在 `drive` 之后）：

```python
async def drive_until(ops, flow, identifier, stage, max_steps=60):
    for _ in range(max_steps):
        row = ops.store.rows[identifier]
        if row["state"] != "queued":
            return copy.deepcopy(row)
        if row["result"].get("stage") == stage:
            return copy.deepcopy(row)
        row.update(state="running", attempt=row["attempt"] + 1)
        await flow.step(copy.deepcopy(row))
    pytest.fail("step budget exceeded")
```

测试：

```python
@pytest.mark.asyncio
async def test_predicted_deadline_exceeded_cancels_and_fails(env):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(
        P, "deadline", {"contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0}
    )
    await drive_until(ops, flow, row["id"], "poll")
    current = ops.store.rows[row["id"]]
    assert current["result"]["deadline_seconds"] == 12_150
    current["result"]["deadline_seconds"] = 0  # expire immediately
    current.update(state="queued")
    final = await drive(ops, flow, row["id"])
    assert final["state"] == "failed", final
    assert final["error"] == "COMPILE_DEADLINE_EXCEEDED"


@pytest.mark.asyncio
async def test_poll_without_deadline_field_falls_back_to_fixed_budget(env):
    ops, flow, remote = env
    group = await collection(env)
    row = await ops.store.submit(
        P, "legacy", {"contract": BUILD_CONTRACT, "collection_id": group["id"], "expected_generation": 0}
    )
    await drive_until(ops, flow, row["id"], "poll")
    current = ops.store.rows[row["id"]]
    current["result"].pop("deadline_seconds", None)  # simulate a pre-upgrade job
    current["result"]["submitted_at"] = time.time() - 1801
    current.update(state="queued")
    final = await drive(ops, flow, row["id"])
    assert final["state"] == "failed", final
    assert final["error"] == "COMPILE_DEADLINE_EXCEEDED"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py::test_predicted_deadline_exceeded_cancels_and_fails tests/test_ontology_compile.py::test_poll_without_deadline_field_falls_back_to_fixed_budget -q`
Expected: 第一个 FAIL（当前固定 1800，deadline_seconds=0 不触发超时，任务正常完成，`final["state"]` 是 `schema_review`）；第二个 PASS（1801 > 1800 本来就超时）——**只有第一个失败是本任务要修的**。

- [ ] **Step 3: 实现（poll 行替换 + 字段删除，同一提交）**

`team_ontology/compile_flow.py` 把：

```python
            if time.time() - result["submitted_at"] > self.cfg.compile_timeout_seconds:
```

替换为：

```python
            # Jobs persisted before the adaptive deadline keep the legacy fixed budget.
            if time.time() - result["submitted_at"] > result.get("deadline_seconds", 1800):
```

`team_ontology/config.py` 删除 Task 1 标记废弃的两行（`compile_timeout_seconds` 字段及其注释），只保留：

```python
    compile_deadline: CompileDeadlineConfig = field(default_factory=CompileDeadlineConfig)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py -q`
Expected: `85 passed`

- [ ] **Step 5: 提交**

```bash
git add team_ontology/compile_flow.py team_ontology/config.py tests/test_ontology_compile.py
git commit -m "feat(ontology): poll against per-job adaptive deadline with legacy fallback"
```

---

### Task 6: 轮次重置清理 deadline 字段 + history 记录 `deadline_seconds`

**Files:**
- Modify: `team_ontology/compile_flow.py`（`new_round` 约 713-733 行、`confirm` 约 763-773 行、`retry` 约 814-822 行——行号以锚点代码为准）
- Test: `tests/test_ontology_compile.py`

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_compile.py`）**

```python
@pytest.mark.asyncio
async def test_repair_round_recomputes_predicted_for_subset(env):
    ops, flow, remote = env
    row = await build(env)
    assert row["result"]["predicted"]["batches"] == 2
    schema = copy.deepcopy(SCHEMA)
    schema["predicates"]["extra"] = {**schema["predicates"]["status"]}
    schema["revision"] = "example-v2"
    selected = row["result"]["refs"][1]["source_id"]  # body.md only -> subset re-round
    await flow.confirm(
        P,
        row["id"],
        ConfirmSchemaRequest(
            schema_body=schema,
            expected_digest=row["result"]["output_digest"],
            affected_sources=[selected],
        ),
    )
    # Round reset clears the deadline fields before re-staging.
    queued = ops.store.rows[row["id"]]["result"]
    assert "predicted" not in queued and "deadline_seconds" not in queued and "deadline_at" not in queued
    row = await drive(ops, flow, row["id"])
    assert row["state"] == "review_ready", row
    # The subset re-round staged only body.md (20 chars) -> one batch, recomputed deadline.
    assert row["result"]["predicted"]["batches"] == 1
    assert row["result"]["predicted"]["estimated"] is False
    # Round 0's snapshot feeds the calibration dataset (spec §7).
    assert row["result"]["history"][0]["deadline_seconds"] == 12_150
    assert row["result"]["history"][0]["predicted"]["batches"] == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py::test_repair_round_recomputes_predicted_for_subset -q`
Expected: FAIL — `KeyError: 'deadline_seconds'`（history 快照缺新增字段；`predicted` 本身已由 Task 4 的 submit 转换按子集 source_map 重算，无需本任务修复）

- [ ] **Step 3: 实现（三处相同模式）**

对 `new_round` / `confirm` / `retry` 三处：

1. history 快照的 key 元组**末尾**追加 `"deadline_seconds", "predicted"`（快照在 pop 之前发生，`result.get("predicted")` 归档整块——spec §7 的标定分桶依赖每轮的批次数，三处元组分别是：
   - `new_round`：`("compile_id", "target", "payload", "output_digest", "submitted_at", "compile_completed_at", "token_usage")`
   - `confirm` 与 `retry`：`("compile_id", "target", "submitted_at", "compile_completed_at", "token_usage")`）
2. 紧随其后的 `for key in (...)` 清理列表追加 `"predicted", "deadline_seconds", "deadline_at"`（三处列表分别是：
   - `new_round`：`("compile_id", "payload", "protected", "source_map", "target", "remote_status")`
   - `confirm` 与 `retry`：`("compile_id", "payload", "protected", "source_map", "target")`）

修改后 `new_round` 示例（其余两处同构）：

```python
        result.setdefault("history", []).append(
            {
                k: result.get(k)
                for k in (
                    "compile_id",
                    "target",
                    "payload",
                    "output_digest",
                    "submitted_at",
                    "compile_completed_at",
                    "token_usage",
                    "deadline_seconds",
                    "predicted",
                )
            }
        )
        for key in (
            "compile_id",
            "payload",
            "protected",
            "source_map",
            "target",
            "remote_status",
            "predicted",
            "deadline_seconds",
            "deadline_at",
        ):
            result.pop(key, None)
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py -q`
Expected: `86 passed`

- [ ] **Step 5: 提交**

```bash
git add team_ontology/compile_flow.py tests/test_ontology_compile.py
git commit -m "feat(ontology): recompute deadline per round and snapshot it in history"
```

---

### Task 7: 创建时预测 `creation_prediction` + API 集成（POST 注入 / GET `elapsed_seconds`）

**Files:**
- Modify: `team_ontology/compile_flow.py`（模块函数区，Task 2 函数之后）
- Modify: `team_ontology/api.py`（`public_job`、POST `/jobs` handler、import 区）
- Test: `tests/test_ontology_deadline.py`（单元）+ `tests/test_ontology_compile.py`（HTTP 契约）

- [ ] **Step 1: 写失败单元测试（追加到 `tests/test_ontology_deadline.py`）**

```python
from team_ontology.compile_flow import creation_prediction


def test_creation_prediction_exact_for_roots_collection():
    collection = {
        "request": {"references": []},
        "result": {"files": [{"characters": 126_000}, {"characters": 20}], "bytes": 300_000},
    }
    assert creation_prediction(collection, CompileDeadlineConfig()) == {
        "batches": 2,
        "total_characters": 126_020,
        "duration_seconds": 12_150,
        "note": "预计编译 ~3 小时",
        "estimated": False,
    }


def test_creation_prediction_exact_with_reference_characters():
    collection = {
        "request": {"references": [{"source_id": "s", "revision": "r", "digest": "d"}]},
        "result": {"files": [{"characters": 40_000}], "reference_characters": 50_000, "bytes": 200_000},
    }
    predicted = creation_prediction(collection, CompileDeadlineConfig())
    assert predicted["estimated"] is False
    assert predicted["batches"] == 2  # 90,000 chars -> two batches


def test_creation_prediction_legacy_references_estimated():
    collection = {
        "request": {"references": [{"source_id": "s", "revision": "r", "digest": "d"}]},
        "result": {"files": [{"characters": 100}], "bytes": 25_100},
    }
    predicted = creation_prediction(collection, CompileDeadlineConfig())
    assert predicted["estimated"] is True
    # Legacy fallback: 25,100 / 2.5 - 100 = 9,940 reference chars -> 10,040 total.
    assert predicted["total_characters"] == 10_040


def test_public_job_reports_elapsed_for_running_jobs():
    from team_ontology.api import public_job

    job = {"id": "j", "state": "running", "result": {"submitted_at": time.time() - 90, "stage": "poll"}}
    out = public_job(job)
    assert 80 <= out["result"]["elapsed_seconds"] <= 100
    done = {**job, "state": "failed"}
    assert "elapsed_seconds" not in public_job(done)["result"]
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: FAIL — `ImportError: cannot import name 'creation_prediction'`

- [ ] **Step 3: 实现 `creation_prediction`（`team_ontology/compile_flow.py`，`predicted_block` 之后）**

```python
LEGACY_BYTES_PER_CHAR = 2.5  # Measured on the SIT wiki corpus (2.48-2.57); legacy fallback only.


def creation_prediction(collection, config):
    """Predict compile batches/duration from a sources_ready collection.

    New collections persist exact per-file characters (files) and reference
    characters (reference_characters). Collections created before that field
    existed fall back to an aggregate-bytes estimate for their references.
    """
    result = collection["result"]
    characters = [f.get("characters") or 0 for f in (result.get("files") or [])]
    references = collection["request"].get("references") or []
    estimated = bool(references) and "reference_characters" not in result
    if references:
        if "reference_characters" in result:
            characters.append(result["reference_characters"])
        else:
            # Legacy: aggregate bytes cover files and references; subtract the exact
            # file share (bytes/char estimate) and convert the remainder.
            characters.append(int(max(0, result.get("bytes", 0) / LEGACY_BYTES_PER_CHAR - sum(characters))))
    batches = estimate_batches(characters)
    return predicted_block(batches, sum(characters), compile_deadline_seconds(batches, config), estimated)
```

- [ ] **Step 4: 实现 API 集成（`team_ontology/api.py`）**

import 区改为（新增 `time` 与 `creation_prediction`）：

```python
import time

from .compile_flow import CompileFlow, creation_prediction
```

（原 `from .compile_flow import CompileFlow` 行替换。）

`public_job` 末尾、`return result` 之前插入：

```python
    if job.get("state") == "running" and job["result"].get("submitted_at"):
        result["result"]["elapsed_seconds"] = max(0, int(time.time() - job["result"]["submitted_at"]))
```

POST `/jobs` handler（锚点：`async def create(body: CompileBuildRequest, p=dep)`）改为：

```python
    @api.post("/jobs", status_code=202)
    async def create(body: CompileBuildRequest, p=dep):
        await resolver(p)
        collection = await ops.store.get(p, body.collection_id)
        if collection["state"] != "sources_ready":
            raise HTTPException(409, "SOURCE_COLLECTION_NOT_READY")
        job = public_job(await ops.store.submit(p, body.submission_key, body.model_dump(mode="json")))
        # Ephemeral preview; the stage_inputs -> submit transition persists the
        # authoritative block (recomputed for repair-round source subsets). On an
        # idempotent replay past that transition the persisted block wins.
        if "predicted" not in job["result"]:
            job["result"]["predicted"] = creation_prediction(collection, ops.config.compile_deadline)
        return job
```

- [ ] **Step 5: 运行单元测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_deadline.py -q`
Expected: `27 passed`（Task 2 后 23 + 本任务 4 个单元测试）

- [ ] **Step 6: 写 HTTP 契约测试（追加到 `tests/test_ontology_compile.py`）**

```python
@pytest.mark.asyncio
async def test_http_job_creation_returns_predicted_duration(env, monkeypatch):
    from fastapi import FastAPI

    from team_ontology import api

    ops, flow, remote = env
    monkeypatch.setattr(api, "Operations", lambda *args, **kwargs: ops)
    app = FastAPI()

    async def principal():
        return P

    api.install(app, principal, config=OntologyConfig(enabled=True), dsn="unused", resolver=ops.resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te.test") as client:
        response = await client.post(
            "/te/enterprise/v1/source-collections",
            json={
                "contract": SOURCE_CONTRACT,
                "submission_key": "predict-sources",
                "roots": ["viking://resources/wiki"],
                "readers": ["frank"],
            },
        )
        assert response.status_code == 202, response.text
        group_id = response.json()["id"]
        await drive(ops, flow, group_id)
        response = await client.post(
            "/te/enterprise/v1/jobs",
            json={
                "contract": BUILD_CONTRACT,
                "submission_key": "predict-build",
                "collection_id": group_id,
                "expected_generation": 0,
            },
        )
        assert response.status_code == 202, response.text
        # The injected preview is ephemeral; only the submit transition persists it.
        assert "predicted" not in ops.store.rows[response.json()["id"]]["result"]
        predicted = response.json()["result"]["predicted"]
        expected_chars = len(remote.files["viking://resources/wiki/index.md"]) + len(
            remote.files["viking://resources/wiki/sub/body.md"]
        )
        assert predicted == {
            "batches": 2,
            "total_characters": expected_chars,
            "duration_seconds": 12_150,
            "note": "预计编译 ~3 小时",
            "estimated": False,
        }
```

- [ ] **Step 7: 写 HTTP 契约测试——存量 references 集合走估算路径（追加到 `tests/test_ontology_compile.py`）**

```python
@pytest.mark.asyncio
async def test_http_creation_prediction_legacy_references_estimated(env, monkeypatch):
    from fastapi import FastAPI

    from team_ontology import api

    ops, flow, remote = env
    remote.frozen["wiki_preexisting"] = {
        "source_id": "wiki_preexisting",
        "revision": "rev-1",
        "text": "签收于 2026-09-01，由上海转运中心发出 " * 5,
        "readers": ["frank"],
        "origin_uri": "viking://resources/wiki/elsewhere.md",
    }
    monkeypatch.setattr(api, "Operations", lambda *args, **kwargs: ops)
    app = FastAPI()

    async def principal():
        return P

    api.install(app, principal, config=OntologyConfig(enabled=True), dsn="unused", resolver=ops.resolve)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://te.test") as client:
        response = await client.post(
            "/te/enterprise/v1/source-collections",
            json={
                "contract": SOURCE_CONTRACT,
                "submission_key": "legacy-refs",
                "roots": [],
                "readers": ["frank"],
                "references": [{"source_id": "wiki_preexisting", "revision": "rev-1", "digest": "unused"}],
            },
        )
        assert response.status_code == 202, response.text
        group_id = response.json()["id"]
        await drive(ops, flow, group_id)
        # Simulate a collection created before reference_characters existed.
        ops.store.rows[group_id]["result"].pop("reference_characters", None)
        response = await client.post(
            "/te/enterprise/v1/jobs",
            json={
                "contract": BUILD_CONTRACT,
                "submission_key": "legacy-refs-build",
                "collection_id": group_id,
                "expected_generation": 0,
            },
        )
        assert response.status_code == 202, response.text
        predicted = response.json()["result"]["predicted"]
        assert predicted["estimated"] is True
        assert predicted["batches"] >= 1
```

- [ ] **Step 8: 运行全部确认通过**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py tests/test_ontology_deadline.py -q`
Expected: `115 passed`（= test_ontology_compile.py 88 + test_ontology_deadline.py 27，`-q` 模式下合并计数）

- [ ] **Step 9: 提交**

```bash
git add team_ontology/compile_flow.py team_ontology/api.py tests/test_ontology_deadline.py tests/test_ontology_compile.py
git commit -m "feat(ontology): predict compile duration at job creation and report elapsed"
```

---

### Task 8: 配置文件与文档替换 + 全量回归

**Files:**
- Modify: `config/config_sit.yaml:64`、`config/config_prd.yaml:64`、`config/config.yaml:78`
- Modify: `docs/ontology-integration/45-native-compile-wiki-operations.md:23`

- [ ] **Step 1: 替换三个 yaml 中的配置**

把每个文件 ontology 块里的：

```yaml
  compile_timeout_seconds: 1800
```

替换为：

```yaml
  compile_deadline:
    base_seconds: 1800        # 固定成本：抽取地板(~1000s) + 父流程(~500s) + 零头
    wave_seconds: 4500        # 每个抽取波次（6 批并行，单满批实测 ~4100s）
    merge_wave_seconds: 1800  # 每个合并波次（10 并发）
    safety_factor: 1.5        # 吞吐波动余量（实测 50-60 tok/s）
```

- [ ] **Step 2: 更新集成文档**

`docs/ontology-integration/45-native-compile-wiki-operations.md` 第 23 行附近的配置样例同步替换（`compile_timeout_seconds: 1800` → 上述 `compile_deadline` 块；若上下文是说明文字，补一句"固定超时已由按批次数自适应的墙钟替代，见 docs/superpowers/specs/2026-09-28-adaptive-compile-deadline-design.md"）。

- [ ] **Step 3: 验证配置加载**

Run: `.venv/bin/python -c "from team_ontology.config import OntologyConfig, CompileDeadlineConfig; import yaml; c = OntologyConfig.from_host(type('H', (), {'ontology': yaml.safe_load(open('config/config_sit.yaml'))['ontology']})()); print(c.compile_deadline)"`（在仓库根目录运行）
Expected: `CompileDeadlineConfig(base_seconds=1800, wave_seconds=4500, merge_wave_seconds=1800, safety_factor=1.5)`

- [ ] **Step 4: 全量回归（ontology 相关）**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py tests/test_ontology_deadline.py tests/test_ontology_schema.py tests/test_ontology_v5.py tests/test_ontology_publication_config.py tests/test_ontology_logging.py -q`
Expected: 全部通过（`test_ontology_compile.py` 基线 81 + 新增 7 = 88；`test_ontology_deadline.py` 27；其余文件维持原有通过数，不得出现新失败）

- [ ] **Step 5: 全量回归（整个 tests/ 目录）**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 与主干基线一致（仓库既有 5 个失败为已知问题，不得新增失败；若有任何新增失败，停下排查是否由本改动引入）

- [ ] **Step 6: 提交**

```bash
git add config/config_sit.yaml config/config_prd.yaml config/config.yaml docs/ontology-integration/45-native-compile-wiki-operations.md
git commit -m "chore(ontology): mount adaptive compile_deadline config in all environments"
```

---

## 完成定义（对照 spec）

- [ ] spec §3.1/3.2：打包估算 + 公式落地，常数来自配置
- [ ] spec §3.3：poll 读 job 内 `deadline_seconds`，重算规则随轮次重置生效
- [ ] spec §3.4：存量 job 回退 1800
- [ ] spec §4：三环境 yaml + `from_host` legacy 键兼容
- [ ] spec §5.1/5.2：POST 响应 `result.predicted`（新集合精确、存量 references 估算）、GET `deadline_seconds`/`deadline_at`/运行中 `elapsed_seconds`
- [ ] spec §7：`history` 快照含 `deadline_seconds`，标定数据集纯 API 可取
- [ ] 全量测试无新增失败
