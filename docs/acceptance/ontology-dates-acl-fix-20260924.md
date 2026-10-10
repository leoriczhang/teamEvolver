# Ontology 日期契约与 ACL 就绪等待修复（2026-09-24）

## 范围与证据

TE 基线为 `7a3aff4`，本报告记录其上的修复，文件摘要见 [源码证据](./ontology-dates-acl-fix-20260924-evidence.json)。只修改 TE 后处理、任务恢复、随版本管理的 Compile Skill、测试及运维说明；未修改 OV 实现，未部署 SIT 服务或触发新的真实模型编译。配套 Skill 随后经用户授权更新，见文末回执。

前序真实运行及任务标识见 [SIT 配套升级复测](./sit-20260924-paired-retest.md)。前序结果仍为失败、未发布，本次本地验证不覆盖该历史结论。

## 日期错误

真实首轮产物有 6 条断言，其中 5 条 `valid_from=null`；补编译的 7 条断言中 6 条起点为空，另 1 条结束时间等于起点。原先只反馈 `COMPILE_OUTPUT_INVALID`，无法告知 Compile 具体出错字段。

修复位于 `team_ontology/compile_contract.py`、`compile_flow.py` 和 `skills/ontology-extraction-v1/SKILL.md`：

- 日期契约保持不变：起点必填、有时区；终点可空，非空则严格晚于起点。不使用当前时间、快照日期或其他断言的日期补齐。
- 同一产物有合法事实时保留合法事实；错误日期断言转成带断言标识、来源、证据、字段及错误码的缺口。Schema 确认与缓存复用均保留缺口，发布前必须显式接受覆盖缺口。
- 全部断言日期不合法时返回 `COMPILE_ASSERTION_DATES_INVALID`。有界补编译接收最多 20 条字段路径及错误码，并保留错误总数；不携带字段原值或异常中的业务正文。
- Skill 明确不可把瞬时事件写成零长度区间；没有结束依据时终点为空，没有起点依据时不生成带虚构日期的断言。

使用前序真实冻结来源与产物在本地重放：首轮得到 **1 条合法事实、5 条明确缺口**；第二轮得到 **7 条日期问题**及具体字段反馈。未修改线上产物，未调用模型，不能将此结果写成新一轮真实抽取通过。

## ACL 就绪问题

源码链路：OV `service/fs_service.py` 的 `mkdir` 创建目录后调用 `vectorize_directory_meta`；`utils/embedding_utils.py` 将上下文处理加入队列；`storage/acl.py` 的 `_apply_subtree` 在上下文记录不存在时拒绝设置 ACL。目录已存在不代表 ACL 依赖的上下文记录已经可用。SIT 后续 stat/ACL GET 成功且仍为 inherit，与这一时序相符；实际队列积压原因仍需 OV 运行日志确认。

原 TE 仅做 6 次快速重试，累计等待约 11.5 秒。新行为：

- 仅将精确的 `COMPILE_ACL_TARGET_NOT_READY` 转为持久化等待，保存目标、开始时间、失败次数及下次重试时间。
- 任务保持 queued / stage_inputs，按 2–15 秒退避；总等待上限 300 秒，超过返回 `COMPILE_ACL_WAIT_EXHAUSTED`。不阻塞 worker 睡眠，不消耗模型补编译轮次。
- 重启续用等待状态，手动重试续用相同执行目录；取消围栏阻止迟到的 ACL 返回重新激活任务。
- 仍要求 restricted ACL 回读及普通用户拒绝探测全部通过后才能写入原文或提交 Compile；403、错误权限及其他异常不视作“索引未就绪”。

此修复处理异步就绪时序，不保证故障的 OV 索引队列在 5 分钟内恢复。超时应按运维说明检查 OV 队列与模型日志。

## 验证与剩余事项

本地 Python 3.12；专用 PostgreSQL 位于 `127.0.0.1:55496`，TE 与 OV 测试分别使用 `ontology_fix_te_0924`、`ontology_fix_ov_0924`，不连接 SIT 数据库。测试覆盖日期缺口审批、缓存复用、准确补编译反馈、等待恢复、取消竞态及权限边界。

专项运行 `tests/test_ontology_compile.py`、`tests/test_ontology_compile_pg.py`、`tests/test_ontology_v5.py`：**102 项通过、3 项被环境依赖阻塞**。其中实际双库运行的日期缺口审批测试通过。3 项分别为 MCP 共享查询、原生身份固定和 trusted Root 发布边界测试，导入 OV 模块时缺少 `volcenginesdkarkruntime`／`rapidfuzz`；补装时包下载出现 TLS 连接错误，未以 mock 绕过依赖，也不将这些项记为通过。

全量 Python 回归：909 通过、65 跳过、5 项既有失败，分别是：

- `test_routes_accept_batch_and_preserve_historical_source_after_removal`
- `test_engine_candidate_is_visible_to_tenant_validation_worker`
- `test_tenant_model_settings_are_isolated_and_secrets_are_masked`
- `test_envelope_only_prompt_title_comes_from_nested_query`
- `test_storage_status_reports_local_skills_and_pg_sessions`

Python 编译及修改文件的 Ruff 检查通过。本地隔离服务已重启，任务查询接口返回 200。没有前端代码变更。文档引用检查仍有既有的 `docs/en/api/06-skill-sync.md` 指向已不存在 `teamEvolver/skills/mutations.py` 的问题。

部署时须配套更新 TE 和 OV 中的 `ontology-extraction-v1/SKILL.md`；摘要不一致会被现有版本校验阻止。本次尚未执行修复版本的 SIT 真实模型抽取、页面审核及发布闭环，需要部署后复测。

## 配套 Skill 更新回执

用户授权后，已于 2026-09-24 17:26（Asia/Shanghai）更新 SIT OV 的 `product_agent` 账户下 `viking://agent/skills/ontology-extraction-v1/SKILL.md`，内容写入返回 200。分别以 `team` 管理员及 `te-ontology-compiler` 普通身份回读，全文及 TE 使用的规范化摘要均与仓库一致。操作 ID、摘要及回读状态见 [更新回执](./ontology-skill-update-20260924.json)。仅更新该 Skill 文件，不改动正式本体资产；异步索引完成状态未验收，Compile 使用显式 URI 读取该文件。

用户接下来只需部署对应 TE 提交，无需重新部署 OV 或重复更新 Skill。部署前避免新建本体任务，防止旧 TE 与新 Skill 摘要不匹配。部署后仍需进行一次真实构建、Schema 确认、候选审核及发布复测。
