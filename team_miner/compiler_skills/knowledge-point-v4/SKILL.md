---
name: skill-miner-knowledge-instructor-v4
description: 从课程原表按知识点生成教官Skill；由Skill内脚本完整取材和生成配置，模型负责教学流程，增量只改受影响知识点。
---

# 按知识点构建与修订教官 Skill

te-native-mining.v1；skill-miner.plan.v1；te-knowledge-point-skill-mining.v4。
completion_policy: te-compile-finished.v1

章节包含多个知识点，知识点包含多个任务，场景可以关联多个知识点。任务和场景的事实只来自各自原表，
共用教官系统提示词是运行规则，其中示例不属于当前知识点。旧产物只作修订对照。
不生成benchmark、验证案例、METHOD.json或EVALUATION.md，不发布，不改变TE的完成策略。

## Planner

只使用已支持的操作：
```python
indexes = p.map(sources, task=contract.extract)
groups = p.shuffle(indexes, by=contract.routing)
batches = p.reduce(groups, task=contract.reduce)
files = p.map(batches, task=contract.synthesize)
p.finalize(files, into=target)
```
extract.execution=agent,input_unit=file,output=records,fields={}；routing.mode=all；
reduce.execution=agent,output=records,fields={}；synthesize.execution=agent,output=files。
整个文件的全部分片作为一个提取单元。不能按知识点筛掉原始表的分片，不能复制整轮教学写作职责给提取Agent。
以下三段必须完整写入对应instructions，保留脚本调用、全部原文件权限和分段写入约束。

extract.instructions：这是来源目录阶段，先读references/source-index.md和assignment.json。
将本单元inputs[].id的完整列表传给run_skill_script(path="scripts/prepare_point.py",
data={"mode":"index","source_range_ids":[本单元全部真实ID]})。
它只产生当前文件的一条目录，原表包含完整文件权限，知识点表另含真实知识点目录；旧稿只输出路径对照。
直接emit result_ref=脚本返回的record_response_ref，结束本单元。不要自行抽取、概括或重写任何任务、场景、原始表或旧稿；
不手写records，不新增evidence_spans，不把全局任务提到的其他文件当作当前分配。

reduce.instructions：这是派发阶段，先完整read_file assignment.json并读references/point-planning.md。
直接读取inputs[].payload.fields，不调用read_evidence，不重读整库正文。首次按真实knowledge_catalog及mining_intent
选择知识点，增量按本轮明确反馈确定write_set，保持不动的知识点不派发。
每个point-batch的inputs必须同时引用六份原始材料的complete-original-file目录记录，不能只挂知识点表和系统提示词，
不能按文件名去重后丢分片。业务任务与场景的筛选由写作阶段的Skill脚本按真实所属知识点字段完成，派发不猜关联。
增量还引用实际反馈和仅属于当前知识点的context-locator，不附其他知识点旧稿。
payload只写kind=point-batch、knowledge_id、knowledge_name、skill_slug=knowledge-原ID、operation=create/revise、
round_mode和短change_request，不能把source_ranges数组或原文正文复制进批次。
超过4条批次时先write_file建立dispatch.json的records数组及唯一尾标记；每次响应只edit_file追加一条，
每次新增不超过1200字符，单条过长分段补齐，不能减少来源。edit成功后直接下一条，不逐条回读。
全部批次完成后才闭合JSON、完整回读并一次emit result_ref；小名单可直接emit完整records。
emit前核对知识点ID集合和write_set一致、每个ID恰好一条、每条都有六份完整原文目录。
禁止整张派发表一次write_file，禁止先emit第一条再打算补后续，禁止为满足总数量重写未修改知识点。

synthesize.instructions：这是当前知识点的教学流程写作，先读references/point-authoring.md及assignment.json。
调用run_skill_script(path="scripts/prepare_point.py", data={"mode":"prepare","input_record_id":"本次分配的真实顶层point-batch ID"})。
脚本只读取该批次已有原文权限，按原始字段取当前知识点的真实任务和场景，写好合法JSON配置和SKILL骨架，
旧稿仅作对照；原表缺失或权限不足时明确报错，禁止改用提示词示例。先使用脚本返回的数据，不自行编造ID或关联。
create时只编辑骨架的__MINER_TEACHING_METHOD__，写简明可执行教学流程；不重建配置、不重抄长业务原文。
读取prepared/instructor-rules.txt理解教官规范，但其中示例只能解释格式，不能作为当前tasks/scenes。
revise时脚本已将当前知识点旧SKILL完整复制到本Agent scratch；仅edit_file修改反馈涉及位置，保留原有行，
不重新生成整篇，不读其他知识点旧稿。未要求改配置就不返回配置。
每次只调用一次write_file/edit_file、新增不超过1200字符。最终只返回实际变更文件，正文使用本Agent的content_ref，
inputs只引用本次point-batch ID。create通常三份：SKILL.md、references/knowledge-config.json及analysis说明；
revise只交改动文件和analysis。脚本返回的script_prepared_publish_files必须按原路径引用，不要用模型重新抄写内容。
路径固定compiled_skill/knowledge-原ID/，SKILL.md大写，分析在根analysis/knowledge-batches/。
不生成benchmark、验证案例，不把prepared/临时文件列为产物。

## 执行边界

每个Agent只完成当前阶段。脚本只能使用本单元的真实输入ID；不能猜文件系统路径、跨Agent引用scratch或更改运行时。
只运行本Skill随附的prepare_point.py，不写临时代码替换它，不复制原表进入长回复。
未解决的实际材料问题如实记录，不能用临近知识点、同号任务/场景或提示词示例补造事实。
TE只冻结输入、运行Compile并记录完成；不添加TE业务匹配、内容验收或分发逻辑。旧版完整保留。
