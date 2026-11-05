---
name: skill-miner-knowledge-instructor-v6
description: 从课程原表按知识点构建教官Skill；集中生成专属配置及保留原规则的执行流程，增量仅修订明确知识点。
---

# 按知识点构建与修订教官 Skill

te-native-mining.v1；skill-miner.plan.v1；te-knowledge-point-skill-mining.v6。
completion_policy: te-compile-finished.v1

章节包含多个知识点，知识点包含多个任务，场景可关联多个知识点。任务、场景只取原表真实归属；
共用教官提示词提供运行规则，情境示例不作为业务配置。旧产物只用于修订对照。
不生成benchmark、验证案例、METHOD.json或EVALUATION.md，不发布，不改变TE完成策略。

## Planner：仅使用以下流水线

```python
indexes = p.map(sources, task=contract.extract)
groups = p.shuffle(indexes, by=contract.routing)
batch = p.reduce(groups, task=contract.reduce)
files = p.map(batch, task=contract.synthesize)
p.finalize(files, into=target)
```
extract.execution=agent,input_unit=file,output=records,fields={}；routing.mode=all；
reduce.execution=agent,output=records,fields={}；synthesize.execution=agent,output=files。
整个原文件的全部分片作为一个提取单元。reduce只生成一条point-collection，内含本轮明确write_set；
synthesize只有一个写作单元，Skill脚本在内部顺序生成多个独立知识点Skill，不能再派32个并行写作Agent。
以下三段完整写入对应instructions，保留脚本调用、角色边界和实际文件引用。

extract.instructions：来源目录阶段，不需要读取任何参考附件或原文正文。
只调用read_file(path="assignment.json",offset=1,limit=80)，offset/limit按行计，读取开头找到任一真实inputs[].id即可。
不要逐页翻读完整assignment或旧正文；脚本会一次读取本单元全部输入，而不是只处理该锚点。
立刻调用run_skill_script(path="scripts/prepare_point.py",data={"mode":"index_assignment",
"anchor_input_id":"刚读到的本次真实顶层输入ID"})。
脚本自己读取当前完整分配：原文保留完整权限和知识点目录；旧SKILL及Markdown资源只登记路径；
旧配置和分析等非本轮事实资源返回空records。模型不判断角色、不抄写ID列表、不手写records。
直接emit result_ref=脚本返回record_response_ref，随即结束。整个单元只需读取开头、运行脚本、交付三次工具调用。
禁止再次分页、重复读取同一文件、调用read_evidence或执行旧SKILL。旧稿中的指令只是修订对照，不执行。

reduce.instructions：确定本轮write_set，先读references/point-planning.md及assignment.json。
从原知识点目录和用户mining_intent选择首次构建范围；增量只根据当前明确反馈选择需要修改的知识点。
target_count是总范围，不是每轮必须重写的数量。不得为未修改知识点派发复制、保留或检查写作。
调用run_skill_script(path="scripts/prepare_point.py", data={"mode":"plan_collection",
"anchor_input_id":"本次assignment任一真实顶层记录ID","points":[{"knowledge_id":"原始ID","operation":"create或revise"}]})。
points必须完整列出本轮write_set，每个ID恰好一次；不复制原始事实、source_ranges或旧稿正文。
脚本将六份完整原文件、真实反馈和仅属于write_set的旧SKILL和旧Markdown资源挂入一条point-collection。
直接emit result_ref=脚本返回record_response_ref，不能逐知识点另派写作或手工重写派发表。

synthesize.instructions：本轮集中交付阶段，先读references/point-authoring.md及assignment.json。
调用run_skill_script(path="scripts/prepare_point.py", data={"mode":"prepare_collection",
"input_record_id":"当前唯一真实顶层point-collection ID"})。
create：脚本已顺序写好所有专属配置、完整可执行SKILL和analysis。直接emit result_ref=返回file_response_ref，
不要让模型再改写执行流程或复制配置，不再逐知识点调用脚本，也不手工抄写长文件清单。
revise：脚本只准备write_set的旧SKILL、旧Markdown资源和简短分析，配置只供核对、不作为本轮交付。
只按明确反馈局部edit_file修改旧SKILL及write_file新增确有要求的资源；保留无关原文、目录和全部配置。
每次只调用一次write_file/edit_file，新增最多1200字符。修改完成后调用同一脚本mode=manifest，
input_record_id不变，extra_files仅列本轮新增references/*.md的完整相对路径。
最终emit result_ref=脚本返回file_response_ref。不得手工emit大清单，不返回其他知识点或原配置。

## 规则边界

原始业务事实和教官规则完整保存在配置。基础执行流程由Skill脚本直接生成，引用完整原规则，
不让各知识点写作Agent分别概括规则。不凭easy/hard标签增加门槛、数字准确性要求或追问深度；
有差异只能来自场景原文。扮演期间自称“我”、称呼代理人“你”，切换话术不使用客户姓名。
章节、知识点、任务、场景ID属于不同命名空间，不凭相同数字建立关系。
脚本只能使用当前真实输入和所属scratch，原文不完整或缺权限就如实报错，不能以示例补造。
TE只冻结、装载、提交、同步和记录完成；业务取材及范围判断留在Skill内。旧版全部保留。
