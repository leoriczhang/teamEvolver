# 常设断言（Standing Assertion）设计

- 日期：2026-09-29
- 状态：方向已获用户批准（方向 A），按"一口气实现"指令推进
- 影响范围：TE `team_ontology`（wire 契约/校验/SKILL）+ OV `openviking/ontology`（契约/JSON Schema/查询引擎）
- 关联问题：FAQ 语料（`openviking/faq-md-20260912-short`）构建本体时模型按时间契约产出零断言（trace 实锤：37 实体/60 证据/0 断言）→ `NO_VALID_FACTS` → 返修循环烧完预算终态失败

## 1. 背景与问题

当前时间契约要求每条断言必须有带显式时区的 `valid_from`（TE `team_ontology/wire.py:122` 与 OV `openviking/ontology/contracts.py:122` 双侧强制）。该契约为时变业务事实（运单/签收/调价）设计，查询引擎按 as-of 语义过滤（OV service.py:654）。无时间/弱时间文档（FAQ、公司制度、产品说明）的事实无法满足该契约：模型按"宁可无事实不编时间"的指引输出零断言，构建必然失败且返修无效。

round 1 实测（trace fdc0c2e1...，cmp_485efcd4）：模型正确抽取 37 实体、60 证据、6 类型/18 谓词 schema，主动产出 0 断言并引用时间契约作为理由。

## 2. 目标与非目标

**目标**

1. 无时间/弱时间文档能产出非零断言并完成构建-审核-发布全流程。
2. 保留时间维度价值：dated 断言的 as-of 查询语义不变。
3. 双侧契约同步放宽，存量数据零迁移。

**非目标**

- 不改变实体/证据/覆盖率的契约与校验。
- 不做断言级"推断日期"（不从上下文猜测时间，只做文档化的默认时区归一化）。
- 不改 OV Compile/vikingbot（抽取侧零改动，SKILL 文本变更走既有安装机制）。

## 3. 契约语义

`Assertion.valid_from: datetime | None`（原必填）：

- **有值 = dated**：现行为不变。必须带时区（缺省由 TE 归一化为 Asia/Shanghai，见 §4）；参与 as-of 过滤：`valid_from <= instant < valid_to`。
- **None = standing（常设事实）**：语义为"自来源文档快照起有效，直到 `valid_to` 或后续修订取代"。查询时跳过下界检查：`valid_to` 存在时仍检查上界，否则恒匹配。
- 不引入独立 `temporal` 标记字段：语义由 `valid_from` 是否为空完整推导，避免双源真相。

`valid_to` 保持 `datetime | None`（语义不变，与 standing 正交）。

## 4. 时间归一化（TE 侧，确定性）

`temporal_issues`（compile_contract.py）改为：

- `valid_from` 缺失 → **不再报错**，判为 standing（`ASSERTION_VALID_FROM_REQUIRED` 退役）。
- `valid_from` 有值但无时区 → **归一化为 Asia/Shanghai (+08:00)** 后写回（`ASSERTION_TIMEZONE_REQUIRED` 退役）。理由：业务语料中的裸时间即北京时间，这是文档化解释规则而非编造；确定性归一化优于依赖模型遵循提示。
- 解析失败 → 仍报 `ASSERTION_DATETIME_INVALID`。
- `valid_to <= valid_from`（两者都有值时）→ 仍报 `ASSERTION_INTERVAL_EMPTY`。

OV 侧不做归一化：TE 上传前已归一化，OV 只接收"带时区或 null"。

## 5. SKILL 时间契约段重写要点

- 双轨定义：有时间锚点的事实 → dated（必填 valid_from，无时区可省——会被按北京时间归一化）；无时间语义的事实（定义、规则、属性、关系性描述）→ standing（省略 valid_from）。
- 部分精度日期（只有年/年月）→ standing，不编造日/时精度；日期信息保留在断言值/qualifiers 中。
- standing 不是"降级"：与 dated 同等是一等断言，必须有完整证据支撑（support_sets 规则不变）。

## 6. 改动清单

### TE（inc-aiagent-core-team-evolver）

| 文件 | 改动 |
|---|---|
| `team_ontology/wire.py` | `valid_from: datetime \| None = None`；interval 校验器改为"存在才查时区；两者都有才查区间" |
| `team_ontology/compile_contract.py` | `temporal_issues` 按 §4 重写（含归一化写回 raw）；`candidate_from_drafts` 的 NO_VALID_FACTS 判定不变（standing 计入断言总数） |
| `team_ontology/skills/ontology-extraction-v1/SKILL.md` | 时间契约段按 §5 重写，**范围含引言段（"Unknown time must not be invented..."两行，与新语义矛盾）**；JSONL 断言示例补充 standing 例（现仅 dated 例） |
| `team_ontology/contracts/` | **从 OV 重新生成的产物同步**（candidate_bundle.schema.json / artifact.schema.json / openapi.json / manifest.json——extractor.py 直抽路径运行时加载校验并作为 LLM 结构化输出 schema；TE 侧无导出脚本，是 OV 生成物的字节级镜像） |
| `docs/en/api/18-ontology-operations.md` 及中文对应文档 | valid_from 必填的表述同步更新 |
| 测试 | temporal 归一化（无时区→+08:00）、standing 断言通过校验、dated 现行为回归、NO_VALID_FACTS 仅在零断言时触发；**改写断言了退役行为的既有用例**（如 test_ontology_compile.py 断言 `ASSERTION_VALID_FROM_REQUIRED`、test_ontology_v5.py 引用 ASSERTION_* 码） |

### OV（inc-aiagent-core-openviking）

| 步骤 | 改动 |
|---|---|
| 1. 模型 | `openviking/ontology/contracts.py` Assertion 同 TE 放宽（镜像契约） |
| 2. **重新生成契约产物** | 运行 `openviking/ontology/export_contracts.py`——从 pydantic 导出 `contracts/*.schema.json`、`openapi.json`、`manifest.json`（含 sha256）。**手改产物是错的**：会被下次导出覆盖、manifest hash 过期、偏离生成形状（pydantic 对可空导出 `anyOf: [{string, date-time}, {null}]`，非 `type: ["string","null"]`） |
| 3. 查询引擎 | `openviking/ontology/service.py` as-of 过滤 null-safe：`valid_from` 为 null 时跳过下界检查，`valid_to` 检查保留。query/explain/compose 三端点都路由经 `_query`（service.py:654/722/794），改一处全生效；rollback 与 artifact 重建走 CandidateBundle pydantic 校验，模型放宽自动覆盖 |
| 4. 测试 | OV 现无 ontology 查询测试（仅 tests/observability/）——新建 `tests/ontology/` 放断言模型与 as-of 过滤用例（pydantic 直测 + _query 过滤逻辑单测，不建重型 harness） |
| 5. 同步 TE 镜像 | 将 OV 重新生成的 `contracts/` 产物复制回 TE `team_ontology/contracts/`（字节级镜像，含 manifest hash） |

## 7. 兼容性与部署

- **存量数据**：全部有 `valid_from`（dated），语义不变，无迁移。
- **契约版本**：保持 `sf.ontology.v1` / `sf.te.ontology.drafts.v1`——放宽是超集，双侧同步部署后无中间态消费者（OV 由用户自行部署，无协调成本）。
- **SKILL 安装**：SKILL.md 变更导致 digest 变化，需按既有机制（TE 校验摘要匹配）配套安装到测试 OV——部署步骤，非代码。
- **顺序**：先 OV 后 TE 或同仓同时部署均可（OV 放宽后旧 TE 数据照常；TE 放宽后若 OV 未更新，发布会被旧校验拒绝——**建议 OV 先行**）。

## 8. 验收标准

用 `openviking/faq-md-20260912-short` 3 篇文档重新构建：

1. 产出非零断言：FAQ-0000 调价（2026年4月15日0点）→ dated，归一化为 `2026-04-15T00:00:00+08:00`；愿景/价值观/资费规则/加盟模式 → standing。
2. 走完 schema 确认 → 候选审核 → 发布全流程，OV `/ontology/query` 对 standing 断言在任何 instant 返回、对 dated 断言按区间过滤。
3. 两侧测试套件全绿（TE 115+，OV ontology 相关）。

## 9. 已接受的已知风险

- standing 断言"漏抽时间"与"真常设"不可区分（无标记字段）——接受：两者都证据锚定且为真，语义污染有限。
- 归一化假设裸时间=北京时间，若未来有多时区语料需按语料元数据扩展——接受，当前语料全部中文业务文档。
