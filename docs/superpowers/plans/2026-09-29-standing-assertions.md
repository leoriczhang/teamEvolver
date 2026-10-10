# 常设断言（Standing Assertions）实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Assertion.valid_from` 双侧放宽为可空（None=常设事实），使无时间/弱时间文档（FAQ 语料）能产出非零断言并完成构建-发布全流程。

**Architecture:** 同一 Assertion 契约在 TE（`team_ontology/wire.py`）与 OV（`openviking/ontology/contracts.py`）各有一份实现，同步放宽；TE 侧 `temporal_issues` 增加确定性时区归一化（裸时间→Asia/Shanghai +08:00）；OV 查询引擎 as-of 过滤对 null `valid_from` 跳过下界；契约产物（JSON Schema/OpenAPI/manifest）由 OV 的 `export_contracts.py` 重新生成并同步 TE 镜像。

**Tech Stack:** Python 3.12 / pydantic v2 / pytest。TE 测试：`TE_REPO/.venv/bin/python -m pytest`；OV 测试：`OV_REPO/.venv/bin/python -m pytest`。

**执行约束（必须遵守）：**
- **禁止 `git commit`**——两仓库都只 `git add` 暂存（TE 仓库路径 `/Users/01444803/workspace/code/inc-aiagent-core-team-evolver`；OV 仓库路径 `/Users/01444803/workspace/code/inc-aiagent-core-openviking`，是独立 git 仓库）
- 部署顺序：OV 先行（旧 OV 会拒绝 TE 产出的 standing 断言；反向无害）
- 按流程校准记忆执行：任务合并为大批次派发，每批次一次质量审查，不逐任务双审

**Spec:** `docs/superpowers/specs/2026-09-29-standing-assertions-design.md`（TE 仓库）

**测试基线:** TE `tests/test_ontology_compile.py + tests/test_ontology_deadline.py` = 115 passed；全量 `tests/` = 5 failed（既有基线）/692 passed/25 skipped。OV 现无 ontology 测试（`tests/ontology/` 新建）。

---

## Chunk 1: OV 仓库（inc-aiagent-core-openviking）

### Task 1: Assertion 契约放宽 + 模型测试

**Files:**
- Modify: `openviking/ontology/contracts.py:122-134`（Assertion 模型）
- Create: `tests/ontology/test_assertion_contract.py`（目录不带 `__init__.py`，与 `tests/observability/` 惯例一致）

- [ ] **Step 1: 写失败测试**

```python
"""Standing assertions: valid_from nullable, dated semantics unchanged."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from openviking.ontology.contracts import Assertion

UTC = timezone.utc
SUPPORT = [["v1"]]


def _dated(**overrides):
    payload = {
        "assertion_id": "ast_1", "subject": "e1", "predicate": "status", "value": "signed",
        "valid_from": "2026-09-01T00:00:00Z", "support_sets": SUPPORT,
    }
    payload.update(overrides)
    return payload


def test_dated_assertion_still_accepted():
    fact = Assertion.model_validate(_dated())
    assert fact.valid_from == datetime(2026, 9, 1, tzinfo=UTC)


def test_standing_assertion_allows_null_valid_from():
    fact = Assertion.model_validate(_dated(valid_from=None))
    assert fact.valid_from is None and fact.valid_to is None


def test_standing_omission_and_upper_bound():
    fact = Assertion.model_validate({k: v for k, v in _dated().items() if k != "valid_from"})
    assert fact.valid_from is None
    ended = Assertion.model_validate(
        _dated(valid_from=None, valid_to="2026-10-01T00:00:00Z")
    )
    assert ended.valid_to is not None


def test_timezone_still_required_when_present():
    with pytest.raises(ValidationError):
        Assertion.model_validate(_dated(valid_from="2026-09-01T00:00:00"))


def test_interval_still_validated_when_both_present():
    with pytest.raises(ValidationError):
        Assertion.model_validate(_dated(valid_to="2026-08-31T23:00:00Z"))
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/01444803/workspace/code/inc-aiagent-core-openviking && .venv/bin/python -m pytest tests/ontology/test_assertion_contract.py -q`
Expected: FAIL — standing 用例报 `valid_from: Field required` / timezone 用例照旧通过

- [ ] **Step 3: 修改 Assertion 模型**

把（contracts.py，Assertion 类内）：

```python
    valid_from: datetime
    valid_to: datetime | None = None
    # One proof is an AND of evidence IDs. Multiple independently checked proofs are OR.
    support_sets: list[list[str]] = Field(min_length=1, max_length=20)
    premises: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def interval(self):
        if self.valid_from.tzinfo is None or (self.valid_to and self.valid_to.tzinfo is None):
            raise ValueError("timezone required")
        if self.valid_to and self.valid_to <= self.valid_from:
            raise ValueError("empty validity interval")
        return self
```

替换为：

```python
    # None marks a standing assertion: valid from the source document snapshot until
    # valid_to or a later revision supersedes it; dated assertions keep as-of semantics.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    # One proof is an AND of evidence IDs. Multiple independently checked proofs are OR.
    support_sets: list[list[str]] = Field(min_length=1, max_length=20)
    premises: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def interval(self):
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            raise ValueError("timezone required")
        if self.valid_to is not None and self.valid_to.tzinfo is None:
            raise ValueError("timezone required")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("empty validity interval")
        return self
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/ontology/test_assertion_contract.py -q`
Expected: `5 passed`

- [ ] **Step 5: 暂存（不 commit）**

```bash
git add openviking/ontology/contracts.py tests/ontology/
```

### Task 2: 查询引擎 null-safe + 过滤函数单测

**Files:**
- Modify: `openviking/ontology/service.py`（`_query` 内 ~653-657 行的 as-of 过滤；提取为模块级函数）
- Test: `tests/ontology/test_assertion_contract.py`（追加）

- [ ] **Step 1: 写失败测试（追加到 tests/ontology/test_assertion_contract.py）**

```python
from openviking.ontology.service import fact_visible_at


def _fact(valid_from, valid_to=None):
    return {"valid_from": valid_from, "valid_to": valid_to}


def test_fact_visible_at_dated_window():
    assert fact_visible_at(_fact("2026-09-01T00:00:00+00:00"), datetime(2026, 9, 2, tzinfo=UTC))
    assert not fact_visible_at(_fact("2026-09-01T00:00:00+00:00"), datetime(2026, 8, 31, tzinfo=UTC))


def test_fact_visible_at_standing_has_no_lower_bound():
    assert fact_visible_at(_fact(None), datetime(2026, 1, 1, tzinfo=UTC))
    assert fact_visible_at(_fact(None), datetime(2030, 1, 1, tzinfo=UTC))


def test_fact_visible_at_upper_bound_applies_to_standing():
    fact = _fact(None, "2026-10-01T00:00:00+00:00")
    assert fact_visible_at(fact, datetime(2026, 9, 30, tzinfo=UTC))
    assert not fact_visible_at(fact, datetime(2026, 10, 1, tzinfo=UTC))
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/ontology/test_assertion_contract.py -q`
Expected: FAIL — `ImportError: cannot import name 'fact_visible_at'`

- [ ] **Step 3: 实现**

在 `service.py` 模块级（`_query` 所属类定义之前或之后均可，靠近使用处）新增：

```python
def fact_visible_at(fact, instant):
    """As-of visibility: standing facts (null valid_from) have no lower bound; valid_to still applies."""
    start = datetime.fromisoformat(fact["valid_from"]) if fact["valid_from"] else None
    end = datetime.fromisoformat(fact["valid_to"]) if fact["valid_to"] else None
    return not ((start and instant < start) or (end and instant >= end))
```

`_query` 内把：

```python
                start = datetime.fromisoformat(fact["valid_from"])
                end = datetime.fromisoformat(fact["valid_to"]) if fact["valid_to"] else None
                if instant < start or (end and instant >= end):
                    continue
```

替换为：

```python
                if not fact_visible_at(fact, instant):
                    continue
```

- [ ] **Step 4: 运行确认通过**

Run: `.venv/bin/python -m pytest tests/ontology/test_assertion_contract.py -q`
Expected: `8 passed`

- [ ] **Step 5: 暂存**

```bash
git add openviking/ontology/service.py tests/ontology/test_assertion_contract.py
```

### Task 3: 重新生成契约产物 + OV 回归

**Files:**
- Regenerate: `openviking/ontology/contracts/*.schema.json`、`openapi.json`、`manifest.json`

- [ ] **Step 1: 运行导出脚本**

Run: `cd /Users/01444803/workspace/code/inc-aiagent-core-openviking && .venv/bin/python -m openviking.ontology.export_contracts`
Expected: `Exported 11 models and OpenAPI`（数字以现有输出为准）

- [ ] **Step 2: 验证 diff 形状**

Run: `git diff openviking/ontology/contracts/ | head -60`
Expected（关键断言，逐条核对）：
- `candidate_bundle.schema.json` / `artifact.schema.json`：`valid_from` 从 `required` 数组**消失**；出现 `anyOf: [{"format": "date-time", "type": "string"}, {"type": "null"}]` 形状（与现有 `valid_to` 的可空表示一致）
- `manifest.json`：对应文件 sha256 更新
- `openapi.json`：Assertion 定义同步
- **无其他意外 diff**（若出现无关文件变化，停下排查）

- [ ] **Step 3: OV 回归**

Run: `.venv/bin/python -m pytest tests/ontology/ tests/observability/ -q`
Expected: 全部通过（新 8 个 + observability 既有数量）

- [ ] **Step 4: 暂存**

```bash
git add openviking/ontology/contracts/
```

---

## Chunk 2: TE 仓库（inc-aiagent-core-team-evolver）

### Task 4: wire.py Assertion 放宽（镜像 OV）

**Files:**
- Modify: `team_ontology/wire.py:112-132`
- Test: `tests/test_ontology_schema.py` 或新增到 `tests/test_ontology_compile.py`（追加到 `tests/test_ontology_deadline.py` 同级的现有文件更合适——追加到 `tests/test_ontology_schema.py`）

- [ ] **Step 1: 写失败测试（追加到 `tests/test_ontology_schema.py`，import 区按需补 `pytest`/`ValidationError`/`Assertion`）**

```python
def test_standing_assertion_allows_null_valid_from():
    fact = Assertion.model_validate({
        "assertion_id": "ast_1", "subject": "e1", "predicate": "status", "value": "signed",
        "valid_from": None, "support_sets": [["v1"]],
    })
    assert fact.valid_from is None and fact.valid_to is None


def test_assertion_timezone_required_when_valid_from_present():
    with pytest.raises(ValidationError):
        Assertion.model_validate({
            "assertion_id": "ast_1", "subject": "e1", "predicate": "status", "value": "signed",
            "valid_from": "2026-09-01T00:00:00", "support_sets": [["v1"]],
        })
```

- [ ] **Step 2: 运行确认失败**

Run: `cd /Users/01444803/workspace/code/inc-aiagent-core-team-evolver && .venv/bin/python -m pytest tests/test_ontology_schema.py -q`
Expected: 第一个 FAIL（`valid_from: Field required`），第二个 PASS

- [ ] **Step 3: 修改 wire.py Assertion（与 OV Task 1 逐字相同的两处替换：字段默认 + 校验器三行）**

```python
    # None marks a standing assertion: valid from the source document snapshot until
    # valid_to or a later revision supersedes it; dated assertions keep as-of semantics.
    valid_from: datetime | None = None
    valid_to: datetime | None = None
```

校验器：

```python
    @model_validator(mode="after")
    def interval(self):
        if self.valid_from is not None and self.valid_from.tzinfo is None:
            raise ValueError("timezone required")
        if self.valid_to is not None and self.valid_to.tzinfo is None:
            raise ValueError("timezone required")
        if self.valid_from is not None and self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("empty validity interval")
        return self
```

- [ ] **Step 4: 运行确认通过**：`.venv/bin/python -m pytest tests/test_ontology_schema.py -q` → 全绿（含既有用例）

- [ ] **Step 5: 暂存**：`git add team_ontology/wire.py tests/test_ontology_schema.py`

### Task 5: temporal_issues 重写（standing 放行 + 时区归一化）

**Files:**
- Modify: `team_ontology/compile_contract.py:4`（import）、`:151-172`（temporal_issues）
- Test: `tests/test_ontology_compile.py`（改写参数化用例 + feedback limit 输入 + 新增正例）

- [ ] **Step 1: 改写测试（三处）**

(a) `test_all_invalid_dates_send_safe_bounded_repair_feedback` 的参数表（~1145-1151 行）从 5 例改为 3 例（删除 `({"valid_from": None}, "ASSERTION_VALID_FROM_REQUIRED")` 和 `({"valid_from": "2026-09-01T00:00:00"}, "ASSERTION_TIMEZONE_REQUIRED")` 两行，standing/归一化不再报错）：

```python
@pytest.mark.parametrize("dates,code", [
    ({"valid_from": "secret-not-a-date"}, "ASSERTION_DATETIME_INVALID"),
    ({"valid_to": "2026-09-01T00:00:00Z"}, "ASSERTION_INTERVAL_EMPTY"),
    ({"valid_to": "2026-08-31T23:00:00Z"}, "ASSERTION_INTERVAL_EMPTY"),
])
```

(b) `test_temporal_repair_feedback_limit`（~1177 行）输入从 `{"valid_from": None}` 改为 `{"valid_from": "not-a-date"}`：

```python
    issues = [issue for i in range(30) for issue in temporal_issues({"valid_from": "not-a-date"}, i)]
```

(c) 文件末尾新增两个正例：

```python
def test_temporal_issues_standing_and_normalization():
    from team_ontology.compile_contract import temporal_issues

    standing = {"assertion_id": "a", "valid_from": None}
    assert temporal_issues(standing, 0) == []
    assert standing["valid_from"] is None  # standing stays untouched

    naive = {"valid_from": "2026-04-15T00:00:00"}
    assert temporal_issues(naive, 0) == []
    assert naive["valid_from"] == "2026-04-15T00:00:00+08:00"  # normalized in place

    ended_standing = {"valid_from": None, "valid_to": "2026-10-01T00:00:00Z"}
    assert temporal_issues(ended_standing, 0) == []
```

- [ ] **Step 2: 运行确认失败**：`.venv/bin/python -m pytest tests/test_ontology_compile.py -q` → **仅新增正例 `test_temporal_issues_standing_and_normalization` FAIL**（standing 放行与归一化断言）。改写过的参数化用例、feedback limit 用例、1113 隔离用例在旧实现下**仍通过**——它们断言的是错误码/行为中尚未变化的部分。只有新正例红。

- [ ] **Step 3: 实现**

`compile_contract.py` import 行（第 4 行）改为：

```python
from datetime import datetime, timedelta, timezone
```

`temporal_issues`（151-172 行）整体替换为：

```python
_DATE = TypeAdapter(datetime)
# Business corpus dates are Beijing time; timezone-free values normalize here, not in the model.
DEFAULT_TZ = timezone(timedelta(hours=8))


def temporal_issues(raw, index):
    """Validate and normalize assertion validity fields in place.

    A missing valid_from marks a standing assertion (no issue). A timezone-free
    date is normalized to DEFAULT_TZ instead of rejected. Unparseable values and
    inverted intervals (both bounds present) still produce bounded repair issues.
    """
    issues, dates = [], {}
    for field in ("valid_from", "valid_to"):
        value = raw.get(field)
        if value is None:
            continue
        try:
            parsed = _DATE.validate_python(value)
        except ValidationError:
            issues.append({"path": ["assertions", index, field], "code": "ASSERTION_DATETIME_INVALID"})
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=DEFAULT_TZ)
            raw[field] = parsed.isoformat()
        dates[field] = parsed
    if (
        dates.get("valid_from") is not None
        and dates.get("valid_to") is not None
        and dates["valid_to"] <= dates["valid_from"]
    ):
        issues.append({"path": ["assertions", index, "valid_to"], "code": "ASSERTION_INTERVAL_EMPTY"})
    return issues
```

（注意：新实现对 `valid_to` 同样做 +08:00 归一化——spec §4 只提及 `valid_from`，但两个时间字段按同一规则处理是一致且安全的：归一化先于 `Assertion.model_validate`，写回保证下游拿到带时区的值。）

- [ ] **Step 4: 运行**：`.venv/bin/python -m pytest tests/test_ontology_compile.py -q` → **仅 `test_partial_invalid_dates_are_quarantined_without_model_recompile`（1113 区）一个失败**（standing 重复断言不再被隔离，1139 行断言数将变为 3）——Task 8 改写它。其余全绿。

- [ ] **Step 5: 暂存**：`git add team_ontology/compile_contract.py tests/test_ontology_compile.py`

### Task 6: SKILL.md 时间契约重写

**Files:**
- Modify: `team_ontology/skills/ontology-extraction-v1/SKILL.md`（引言两行 + "## Temporal contract" 整段 + JSONL 断言示例补 standing 例）

- [ ] **Step 1: 三处修改**

(a) 引言（"Use only document_fact..." 段内）：先 `grep -n "Unknown time" team_ontology/skills/ontology-extraction-v1/SKILL.md` 定位——该句**跨两行**（约 25-26 行，在 "omit an assertion" 后折行），读取确切换行位置后，把完整两句（含换行）：

```
Unknown time must not be invented: omit an assertion
whose required validity date cannot be supported, and record a coverage gap instead.
```

（以文件实际折行为准）替换为：

```
Facts split into two kinds: time-anchored facts carry valid_from; standing facts
(definitions, rules, attributes without inherent time) omit valid_from entirely.
Never fabricate date or timezone information the source does not support.
```

(b) "## Temporal contract (validate before submitting)" 整段（从标题下第一条到段尾）替换为：

```
- Time-anchored facts MUST have `valid_from` as ISO 8601. A date without a
  timezone is accepted and interpreted as Beijing time (UTC+8); TE normalizes
  it deterministically. Use only a time supported by that assertion's evidence.
- Facts without inherent time semantics (definitions, policies, product rules,
  organizational attributes) are standing assertions: omit `valid_from`
  entirely. A standing assertion is valid from its source document snapshot
  until `valid_to` or a later revision supersedes it; it is not a downgrade and
  requires the same evidence support as any other assertion.
- Never fabricate precision: if the source only states a year or year-month
  (for example "founded in 1993"), emit a standing assertion and keep the
  partial date in the fact value or qualifiers.
- `valid_to` is optional for both kinds. Use null when the source provides no
  explicit end. An instantaneous event such as signing at 10:00 has that time
  as `valid_from` and `valid_to: null`; do not duplicate its timestamp into
  both fields. A supplied end must be strictly later than the start.
- Never borrow a date from a different fact, source revision, current time or
  compilation time.
- Before writing the final JSONL, validate every temporal field and interval
  with code, not manual character counting or another full model pass.
- A repair instruction may contain `repair_issues` with zero-based assertion
  indexes, field paths and stable codes. Fix those errors from source evidence.
  Codes include ASSERTION_DATETIME_INVALID and ASSERTION_INTERVAL_EMPTY. If a
  dated fact cannot carry a supported time, emit it as standing rather than
  inventing a date; report genuinely unsupported content as a coverage gap.
```

(c) JSONL 断言示例在 SKILL.md ~122 行，锚点（紧凑 JSON、完整字段、`+08:00` 偏移）：

```
{"kind":"assertions","value":{"assertion_id":"a-1","subject":"shipment-1","predicate":"status","value":"signed","polarity":"positive","qualifiers":{},"epistemic_kind":"document_fact","valid_from":"2026-09-01T00:00:00+08:00","valid_to":null,"support_sets":[["ev-1"]],"premises":[]}}
```

在该行之后追加一行 standing 示例（**同样的紧凑 JSON 与完整字段结构**）：

```
{"kind":"assertions","value":{"assertion_id":"a-2","subject":"shipment-1","predicate":"official_website","value":"www.sf-express.com","polarity":"positive","qualifiers":{},"epistemic_kind":"document_fact","valid_from":null,"valid_to":null,"support_sets":[["ev-1"]],"premises":[]}}
```

- [ ] **Step 2: 验证**：`.venv/bin/python -c "from team_ontology.compile_contract import compile_skill_digest; from pathlib import Path; print(compile_skill_digest(Path('team_ontology/skills/ontology-extraction-v1/SKILL.md').read_text())[:16])"` → 输出新 digest 前缀（记录，部署时 OV 侧 SKILL 需配套同文件）

- [ ] **Step 3: 暂存**：`git add team_ontology/skills/ontology-extraction-v1/SKILL.md`

### Task 7: 契约镜像同步（OV 生成物 → TE）

**Files:**
- Modify: `team_ontology/contracts/`（candidate_bundle.schema.json / artifact.schema.json / openapi.json / manifest.json 及其他被重新生成的文件）

- [ ] **Step 1: 复制**

```bash
cp /Users/01444803/workspace/code/inc-aiagent-core-openviking/openviking/ontology/contracts/*.json \
   /Users/01444803/workspace/code/inc-aiagent-core-team-evolver/team_ontology/contracts/
```

（TE 特有的 `compile_build.schema.json` / `schema_confirm.schema.json` / `source_collection.schema.json` 不在 OV 目录中，不受影响）

- [ ] **Step 2: 验证**

```bash
cd /Users/01444803/workspace/code/inc-aiagent-core-team-evolver
git diff --stat team_ontology/contracts/
.venv/bin/python -c "import json; m=json.load(open('team_ontology/contracts/manifest.json')); print('files:', len(m['sha256']))"
```
Expected: diff 仅涉及 valid_from 相关的 schema/openapi/manifest；manifest 可解析且列出各文件 sha256

- [ ] **Step 3: 暂存**：`git add team_ontology/contracts/`

### Task 8: 集成测试改写 + 全量回归

**Files:**
- Modify: `tests/test_ontology_compile.py:1113-1141`（quarantine 测试）、`tests/test_ontology_v5.py:603`（检查后按需）

- [ ] **Step 1: 改写 quarantine 测试**

`test_partial_invalid_dates_are_quarantined_without_model_recompile` 中（~1124 行）把：

```python
        duplicate["value"].update(assertion_id="undated", valid_from=None)
```

改为：

```python
        duplicate["value"].update(assertion_id="undated", valid_from="not-a-date")
```

对应断言（~1130、~1140 行）中 `"ASSERTION_VALID_FROM_REQUIRED"` 全部改为 `"ASSERTION_DATETIME_INVALID"`。（测试意图不变：部分无效日期被隔离且不触发重编译——只是无效形态从"缺日期"换成"不可解析日期"，缺日期在新语义下是合法的 standing）

- [ ] **Step 2: test_ontology_v5.py:603 保持原样（已预判定）**

该行是纯展示 fixture 数据：`approve()`（service.py:329-330）只检查 `result.get("gaps")` 的**存在性**，从不检查 gap code 的值——保留 `"ASSERTION_VALID_FROM_REQUIRED"` 字符串无行为影响。无需修改。

- [ ] **Step 3: TE ontology 回归**

Run: `.venv/bin/python -m pytest tests/test_ontology_compile.py tests/test_ontology_deadline.py tests/test_ontology_schema.py tests/test_ontology_v5.py tests/test_ontology_publication_config.py tests/test_ontology_logging.py -q`
Expected: 全绿。计数核对：`test_ontology_compile.py` **88 → 87**（Task 5 净变化：+1 新函数 `test_temporal_issues_standing_and_normalization`、−2 参数化用例）；`test_ontology_schema.py` 既有数 **+2**（Task 4 两个新用例）；deadline 27、其余文件维持原数。

- [ ] **Step 4: TE 全量回归**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 与基线一致（5 failed 既有 / 无新增失败）

- [ ] **Step 5: 暂存**：`git add tests/test_ontology_compile.py tests/test_ontology_v5.py`

### Task 9: API 文档更新

**Files:**
- Modify: `docs/en/api/18-ontology-operations.md:20` 及中文对应文件（grep 定位）

- [ ] **Step 1: 更新两侧文档中"valid_from is required with a timezone"表述**为双轨语义（dated 必带时区；standing 省略 valid_from、自文档快照起有效；无时区日期按北京时间归一化）。中英文档表述对齐。

- [ ] **Step 2: 暂存**：`git add docs/en/api/18-ontology-operations.md docs/zh/api/18-ontology-operations.md`

---

## 部署与验收（代码完成后）

1. **OV 先行**：OV 仓库暂存的改动由用户提交并部署测试 OV（重启 VikingBot）
2. **SKILL 配套安装**：Task 6 的新 SKILL.md 安装到 OV（digest 必须与 TE 捆绑版一致——TE 会在 stage_inputs 校验）
3. **TE 部署**：TE 仓库暂存改动提交部署
4. **验收**（spec §8）：用 `openviking/faq-md-20260912-short` 3 篇重建构建——预期非零断言（调价→dated 归一化 +08:00；愿景/准则/资费规则→standing），走完确认→审核→发布，`/ontology/query` 对 standing 在任意 instant 返回
