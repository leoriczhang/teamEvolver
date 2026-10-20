---
name: skill-miner-knowledge-instructor-v8
description: 从课程原表按知识点构建或局部修订教官Skill；支持只选一个知识点，保留原规则及材料歧义。
---

# 按知识点构建与修订教官 Skill

te-native-mining.v1；skill-miner.plan.v1；te-knowledge-point-skill-mining.v8。
completion_policy: te-compile-finished.v1

章节包含多个知识点，知识点包含多个任务，场景可关联多个知识点。各类ID独立。
只用完整原表提供事实，教官提示词中的情境示例不作为业务配置。旧产物只供修订对照。
不生成benchmark、验证案例、METHOD.json或EVALUATION.md，不发布，不增加TE内容验收。

## Planner

```python
indexes = p.map(sources, task=contract.extract)
groups = p.shuffle(indexes, by=contract.routing)
batch = p.reduce(groups, task=contract.reduce)
files = p.map(batch, task=contract.synthesize)
p.finalize(files, into=target)
```

extract.execution=agent,input_unit=file,output=records,fields={}；routing.mode=all；
reduce.execution=agent,output=records,fields={}；synthesize.execution=agent,output=files。
reduce只产一条point-collection，写明本轮write_set；synthesize只有一个写作单元，
由Skill脚本顺序交付独立Skill，不按知识点启动并行写作Agent。

当前工具消息已经给出assignment及真实inputs[].id，直接使用其中一个顶层ID调用脚本。
不必用read_file重读assignment；数据太长看不全也只需一个真实锚点，脚本读取全部所属输入。
仅在当前消息确实没有ID时读取assignment.json开头最多40行。不要分页读取完整分配或旧正文。
以下各段分别写入对应Transform.instructions；每个child只执行其当前阶段。

### extract.instructions

从本次inputs取得一个真实顶层ID，立即调用run_skill_script(path="scripts/prepare_point.py",
data={"mode":"index_assignment","anchor_input_id":"该ID"})，不读原文或附件。
脚本处理本文件全部分片；每个文件均返回非空目录，旧稿及分析不作为事实。
接着只按返回的next_tool_call.arguments调用emit，然后结束。result_ref是emit顶层参数，
不是records里的成员，也不是run_skill_script自身工具输出文件；不手写或改造返回记录。

### reduce.instructions

从本次inputs取得一个真实顶层ID，调用run_skill_script(path="scripts/prepare_point.py",
data={"mode":"inspect_assignment","anchor_input_id":"该ID"})。
按返回的原知识点目录、mining_intent及current_revision_requests选择本轮write_set。
首次可只选一个或多个知识点；target_count表示总范围，增量只选本轮有明确修改要求的成员。
重复要求已满足时，对涉及成员做revise但不追加重复内容；不为未涉及成员派发保留或检查写作。
再调用同一脚本mode=plan_collection，anchor_input_id不变，points列实际knowledge_id及create/revise。
脚本仅挂入完整原文、当前反馈及write_set所属旧Markdown。按next_tool_call.arguments调用emit，结束。

### synthesize.instructions

从本次唯一inputs取得顶层ID，调用run_skill_script(path="scripts/prepare_point.py",
data={"mode":"prepare_collection","input_record_id":"该ID"})。
create的配置、基础执行流程和analysis已完整准备，直接emit(result_ref=返回file_response_ref)，结束。
不重新概括共用规则，不手抄文件清单。歧义由配置source_issues保留，不能擅自修补成事实。
revise按当前反馈局部edit_file/write_file；只读所需旧文件或prepared/核对信息，不重新通读所有材料。
现有要求已满足就不修改正文；新增最多1200字符一次，一次只操作一个文件。
原配置不交付，未涉及原文不缩写或重写。最后调用同一脚本mode=manifest，input_record_id不变，
extra_files仅列新增references/*.md，按返回file_response_ref调用emit。细节按需读references/point-authoring.md。

## 保留边界

基础流程由Skill脚本统一生成，并引用完整原教官规则。人物、答案、数值、条件、例外和难度标签原样保留；
不凭easy/hard额外改变完成门槛、数字准确性或追问深度。扮演时自称“我”、称呼代理人“你”。
原表有重名、重复ID或归属不明时不得静默选一个；格式歧义保留来源与原行，说明影响。
脚本只访问当前真实输入及所属scratch；缺原文或权限就报运行问题，不用示例补造。
TE只负责通用冻结、提交、同步和完成记录。旧版全部保留。
