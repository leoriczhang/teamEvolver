# 单篇 Ontology 真实联调补充（2026-09-24）

## 环境与输入

延续 [首轮验收](./sit-20260924.md)，使用隔离租户／OV account `te-acceptance-20260924`。来源为 `viking://resources/team/acceptance-20260924/交接说明.md`，冻结原文 115 个 Unicode 字符。内容为合成运单交接、带明确时间的签收及验收说明，无真实业务资料。

来源集合：`ont_5351c08d4f3acf3ab88d1b36`；TE 任务：`ont_93b91a685042201bf1bd322f`。

## 真实执行证据

- 来源冻结成功，单文件构建受理成功；未批准提交返回 409 `APPROVAL_REQUIRED`，普通身份读取编译输入返回 403。
- 首次 Compile `cmp_f6b6777e8f924e4e93d8bc9bbe64da0f` 于 14:51:01 开始，14:58:44 最终报告 `BOT_RESTARTED`。这是用户告知重启完成后本次新任务返回的结果，不能记为成功。
- Trace `6b83b40f3f3e8d8145c1b8d621378581`：主 Agent 两次模型调用后派发子任务；子任务一次模型调用约 360 秒、输出 17,561 Token，随后写出 JSON，但未完成正式 Compile。
- 确认旧任务 failed 后重试得到 `cmp_de1f5e42e5ce43dfae0a95af92bf328a`（15:00:09 开始），trace `2d9cd92de9bf87962711b10eee3e14d7`。
- 重试追踪显示反复经历：纯 JSON 草稿无法登记 → 补 Markdown → 缺 type/description → sources 为字符串列表 → sources 对象字段不匹配 → 修复与再次提交。中途也有工具 ERROR。所有这些都发生在原生 Compile 中，尚不能算 TE 候选审核通过。
- 最终追踪记录模型调用 43 次，其中 42 次有用量；累计输入 1,069,512、输出 74,279 Token。输入按每次调用累加，包含重复上下文，不等于独立资料量或计费 Token。“单篇快速完成”未达到。

## 源码核验和本地修复

OV 的 `bot/vikingbot/agent/tools/compile.py` 在资源草稿登记时仅收集 Markdown；`compile_merge.py` 的 `_source_uris` 要求 frontmatter 的 `sources` 为非空对象列表，每项必须有非空 `resource` 字符串。原 `ontology-extraction-v1` 未说明此兼容约束。

TE 自带 Skill 已补充短 Markdown 草稿和完整 frontmatter 示例（type/title/description/sources.resource），保留原来的 JSON 产物要求；同时明确 digest/revision 是来源回执标识，避免模型尝试不同哈希编码。未修改 OV 代码。

此变更**只在本地**。TE 会校验 Skill 摘要，需要将 TE 代码及 OV 上安装的 Skill 配套升级；不能只覆盖线上 Skill。当前运行的 SIT 任务使用旧 Skill，不能用它的结果证明新指引有效。

还完成 ZIP 请求媒体类型修复：本地真实 HTTP 服务上四个传输 POST 的 multipart ZIP 均返回 415，正确 JSON 导入 200；相关 Python 测试 87 通过。全量 Python 仍为 900 通过、64 跳过、5 个既有失败。新 Skill 的真实模型验收待配套部署，不将模型桩验证记为真实通过。

## 其他复测与限制

- OV 重启后，隔离经验同步仍出现 504 `DEADLINE_EXCEEDED`，正文确认但索引待确认；已再次关闭该隔离租户的同步。
- 浏览器控制仍出现窗口变更、空状态及内置页面绑定超时；已请求用户协助保持测试标签页可操作，未收到恢复确认。
- 未生成有效候选前，不执行 Schema 确认、审批或正式发布；没有手工伪造 Compile 产物，也没有绕过证据校验。

## 最终状态与后续门槛

15:30 TE 返回 `failed / COMPILE_DEADLINE_EXCEEDED`；OV 任务最终 `cancelled`，未遗留本次运行任务。capabilities 确认正式 generation 仍为 0，未发生审批或发布。隔离经验同步最终 `enabled=false`。

**本轮完整验收未通过，不能报告已跑通。** 部署修复后的 TE，并同步安装与其摘要一致的 `ontology-extraction-v1` 后，重新开始单篇验收，才能验证新 Skill 是否消除草稿协议重试；Schema 确认、候选审核、发布、查询、撤回／回滚仍待真实执行。代码和材料按后续指令提交、推送；本轮未部署服务或更新线上 Skill。

本地修复的真实模型表现仍未验证；不能断言配套升级后一定能在目标时限内完成。现有时限和证据门禁保持不变，没有通过延长时限或跳过校验来改变验收结论。
