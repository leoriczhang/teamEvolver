# 验证与 Candidate API

## 1. API 实现介绍

验证与 Candidate API 用于管理进化流程产生的候选 Skill。当 teamEvolver 从会话数据中挖掘出 Skill 优化建议时，会创建 Candidate 并通过 True Replay 进行验证。管理员可通过这些接口查看待审核候选、批准发布、驳回，以及查看 baseline/candidate 的 Replay 对比结果。

代码实现：`teamEvolver/proxy/routes.py`（`api_validation_candidates` 等端点）
验证存储：`teamEvolver/validation/store.py`（`ValidationStore`）
验证 Worker：`teamEvolver/validation/worker.py`（`ValidationWorker`）
True Replay 引擎：`teamEvolver/true_replay.py`
渐进式 Replay 决策：`teamEvolver/progressive_replay.py`

## 2. 接口和参数说明

所有 `/api/validation/*` 接口需要控制台管理员认证（写操作需要 admin 角色）。

---

### GET /validation/candidates

列出候选 Skill。此端点无认证要求（路径以 `/validation/` 开头而非 `/api/validation/`，与控制台共享）。完整管理接口为 `/api/validation/candidates`。

**认证：** 控制台 Cookie（`/api/validation/candidates` 需要）；无认证（`/validation/candidates` 为兼容路径）

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `scope` | string | 否 | 筛选范围：`open`（待审核，默认）、`processed`/`history`/`closed`/`decided`（已处理）、`all`（全部） |
| `limit` | integer | 否 | 每页数量，1-200，默认 20 |
| `offset` | integer | 否 | 分页偏移，默认 0 |
| `refresh` | boolean | 否 | 是否强制刷新缓存，默认 false |
| `compact` | boolean | 否 | 是否返回精简格式（不含 diff 等大字段），默认 false |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `candidates` | array | 候选列表 |
| `total` | integer | 总数 |
| `limit` | integer | 当前页大小 |
| `offset` | integer | 当前偏移 |
| `has_more` | boolean | 是否有更多数据 |

**Candidate 对象字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `job_id` | string | 任务 ID |
| `skill_name` | string | Skill 名称 |
| `proposed_action` | string | 提议操作（update/create/delete） |
| `review_status` | string | 审核状态：`open`、`published`、`rejected` |
| `rationale` | string | 优化理由 |
| `test_dataset_count` | integer | 测试数据集数量 |
| `recommended_publish` | boolean | 是否推荐发布（基于 Replay 评估） |
| `replay_verdict` | string | Replay 判定：`accept`、`reject`、`inconclusive` |
| `evaluation` | object | 评估结果（含 replay 详情） |
| `decision` | object | 审核决策（已处理时） |
| `decision.status` | string | `published` 或 `rejected` |
| `decision.accepted` | boolean | 是否通过 |
| `decision.reason` | string | 决策理由 |
| `decision.version` | integer | 发布后的版本号（通过时） |
| `candidate_skill_md` | string | 候选 SKILL.md 内容（compact=false 时） |
| `current_skill_md` | string | 当前 SKILL.md 内容（compact=false 时） |
| `skill_diff` | string | unified diff（compact=false 时） |

**缓存：** 候选列表缓存 15 秒。

代码入口：`teamEvolver/proxy/routes.py:4040` (`api_validation_candidates`)

---

### GET /api/validation/candidates/{job_id}/detail

获取候选的完整详情，包含 Skill 内容、diff 和进化上下文。

**认证：** 控制台 Cookie（管理员）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_id` | string | 是 | 任务 ID |

**响应：** 完整 candidate 对象，包含 `current_skill_md`、`candidate_skill_md`、`skill_diff` 和 `evaluation` 详情。

代码入口：`teamEvolver/proxy/routes.py:4077` (`api_validation_candidate_detail`)

---

### POST /api/validation/candidates/{job_id}/evaluate

对候选执行 True Replay 评估，生成 baseline/candidate 对比结果。

**认证：** 控制台 Cookie（管理员）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_id` | string | 是 | 任务 ID |

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `refresh` | boolean | 否 | 是否强制重新评估（忽略缓存），默认 false |

**响应字段：** 评估结果，包含：

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `evaluated` |
| `skill_name` | string | Skill 名称 |
| `recommended_publish` | boolean | 是否推荐发布 |
| `replay.verdict` | string | 判定结果 |
| `replay.no_regression` | boolean | 是否无回退 |
| `replay.cases` | array | 各测试用例的 baseline/candidate 对比 |
| `replay.cases[].baseline` | object | 基线分支结果 |
| `replay.cases[].candidate` | object | 候选分支结果 |
| `replay.efficiency` | object | 效率对比（dimensions 包含 turns/tool_calls/tokens 的 baseline/candidate/delta/winner） |
| `replay.checklist` | object | Checklist 对比 |
| `candidate_skill_md` | string | 候选内容 |
| `current_skill_md` | string | 当前内容 |
| `skill_diff` | string | diff |

**Replay 执行超时：** 由环境变量 `TEAMEVOLVER_TRUE_REPLAY_TIMEOUT_S` 控制（默认 90 秒），最大交互轮次由 `TEAMEVOLVER_TRUE_REPLAY_MAX_INTERACTIONS` 控制（默认 4）。

代码入口：`teamEvolver/proxy/routes.py:4082` (`api_validation_candidate_evaluate`)

---

### POST /api/validation/candidates/{job_id}/validate

审核候选并决定是否发布。`mode=force` 时忽略自动评估结果强制发布。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_id` | string | 是 | 任务 ID |

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `mode` | string | 否 | 审核模式：`auto`（默认，根据评估结果决定）、`force`（强制发布，忽略评估） |

**响应字段（发布成功）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `published` |
| `accepted` | boolean | `true` |
| `job_id` | string | 任务 ID |
| `skill_name` | string | Skill 名称 |
| `created` | boolean | 是否新建（Skill 之前不存在） |
| `version` | integer | 发布后的新版本号 |
| `loaded_skills` | integer | 重新加载的 Skill 数 |
| `cloud` | object | 云端同步结果 |
| `evaluation` | object | 评估结果 |

**响应字段（驳回）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `rejected` |
| `accepted` | boolean | `false` |
| `reason` | string | 驳回原因 |

代码入口：`teamEvolver/proxy/routes.py:4096` (`api_validation_candidate_validate`)

---

### DELETE /api/validation/candidates/{job_id}

删除候选记录。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `job_id` | string | 是 | 任务 ID |

代码入口：`teamEvolver/proxy/routes.py:4168` (`api_validation_candidate_delete`)

---

### POST /api/validation/candidates/{job_id}/approve

批准候选发布（`validate` 接口的便捷别名，mode=auto）。

### POST /api/validation/candidates/{job_id}/reject

驳回候选（`validate` 接口的便捷别名，强制驳回）。

## 3. 使用示例

### 列出待审核候选

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/validation/candidates?scope=open&limit=10"
```

响应示例：

```json
{
  "candidates": [
    {
      "job_id": "job-20240115-001",
      "skill_name": "database-debugging",
      "proposed_action": "update",
      "review_status": "open",
      "rationale": "添加了连接池配置指南，减少工具调用次数",
      "recommended_publish": true,
      "replay_verdict": "accept",
      "test_dataset_count": 2,
      "evaluation": {
        "replay": {
          "verdict": "accept",
          "no_regression": true,
          "efficiency": {
            "dimensions": {
              "interaction_turns": {"baseline": 3, "candidate": 2, "delta": 1, "winner": "candidate"},
              "tool_call_count": {"baseline": 5, "candidate": 3, "delta": 2, "winner": "candidate"},
              "total_tokens": {"baseline": 5200, "candidate": 4100, "delta": 1100, "winner": "candidate"}
            }
          }
        }
      }
    }
  ],
  "total": 1,
  "limit": 10,
  "offset": 0,
  "has_more": false
}
```

### 查看候选详情和 Replay 结果

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/validation/candidates/job-20240115-001/detail"
```

### 执行 Replay 评估

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/validation/candidates/job-20240115-001/evaluate"
```

### 批准发布

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/validation/candidates/job-20240115-001/validate" \
  -d '{"mode": "auto"}'
```

响应示例：

```json
{
  "status": "published",
  "accepted": true,
  "job_id": "job-20240115-001",
  "skill_name": "database-debugging",
  "created": false,
  "version": 4,
  "loaded_skills": 6,
  "cloud": {
    "synced": true,
    "action": "push",
    "event_id": "skill_evt_a1b2c3d4..."
  },
  "evaluation": {...}
}
```

### 强制发布（忽略评估）

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/validation/candidates/job-20240115-001/validate" \
  -d '{"mode": "force"}'
```

### 驳回候选

```bash
curl -X DELETE -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/validation/candidates/job-20240115-001"
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `login required` | 未登录 |
| 403 | `only admin users can perform this operation` | 非管理员执行写操作 |
| 404 | `candidate not found` | job_id 不存在 |
| 400 | `candidate missing skill payload` | 候选缺少 Skill 内容（无法发布） |

### Checklist 门禁策略

候选 Skill 的通过条件：

1. **Checklist 全通过**：所有 checklist 项必须 pass，Checklist 完成度是门禁条件而非加权分数。
2. **无效率回退**：candidate 分支在 interaction_turns、tool_call_count、total_tokens 三个维度上不劣于 baseline（`no_regression: true`）。
3. **效率比较顺序**：turns 优先，其次 tool_calls，最后 tokens。

不满足上述条件时，`recommended_publish` 为 false，`mode=auto` 会自动驳回。`mode=force` 可绕过自动检查强制发布。

代码：`teamEvolver/progressive_replay.py` (`progressive_replay_decision`)

### 已发布候选的版本管理

- 批准发布后，Skill 版本号自动递增；
- 新版本内容写入本地 skills 目录并同步到云端；
- 发布操作触发 Skill Sync webhook，通知所有已注册 Agent 更新；
- Skill Manager 自动重新加载，新 Skill 立即在下次会话中可用。
