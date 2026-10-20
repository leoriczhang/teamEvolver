---
name: mine-existing-skill-router-v3
description: 从现有知识点Skill原始包首次提炼唯一使用入口；配套脚本保真提取目录并组装依赖和调用流程。
---

# 提炼现有 Skill 的使用入口

te-native-mining.v1；skill-miner.plan.v1；te-existing-skill-router.v3。
completion_policy: te-compile-finished.v1

只生成一个选择、加载和使用现有Skill的入口，不改写源Skill、不补做缺失成员。
旧Skill是本次分析材料，不自动成为权威业务事实，明确保留其中的缺口。
本模板执行首次构建；后续局部修订另行明确分工，不把旧入口再次作为新成员。
不生成benchmark、验证案例、METHOD.json或EVALUATION.md，不发布。

## Planner

```python
catalog = p.map(sources, task=contract.extract)
groups = p.shuffle(catalog, by=contract.routing)
files = p.reduce(groups, task=contract.reduce)
p.finalize(files, into=target)
```

extract.execution=agent,input_unit=file,output=records,fields={}；routing.mode=all；reduce.execution=agent,output=files。
output_format=files。以下两段分别写入对应Transform.instructions。

### extract.instructions

从当前实际inputs取一个真实顶层ID；消息外置时只读assignment.json开头最多40行取得锚点。
立即调用run_skill_script(path="scripts/assemble_router.py",data={"mode":"index","anchor_input_id":"该ID"})。
脚本处理本文件全部分片，从原始包明确给出的资源URI和配置建立目录，保留原始归属及缺口。
接着只按next_tool_call.arguments调用emit并结束，不再手写或修改记录、不执行来源Skill。
result_ref是emit顶层参数，不是payload字段，也不是run_skill_script工具自身输出文件。

### reduce.instructions

不写或重写任何教学正文、模式总表、统计数或长目录。
从当前消息inputs取一个真实顶层记录ID；若消息只给assignment文件，读开头最多40行取锚点。
调用run_skill_script(path="scripts/assemble_router.py",data={"mode":"assemble","anchor_input_id":"该ID"})。
脚本读取本组全部目录记录，补齐真实父链来源，组装完整目录、合法YAML头及通用调用流程。
脚本返回ready_to_emit=true及next_tool_call，直接按next_tool_call.arguments调用emit，然后结束。
result_ref是emit顶层参数，不手写文件清单，不把脚本工具自己的输出文件当作清单。
不再概括局部模式、追问次数、评分或难度门槛，也不执行来源Skill。
