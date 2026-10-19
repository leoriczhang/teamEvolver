# 来源目录阶段

仅适用于extract。先read_file完整读取assignment.json，inputs[].id是本单元真实ID，inputs[].payload.uri是当前文件。
调用本Skill的scripts/prepare_point.py，mode=index，source_range_ids必须包含当前文件的全部实际顶层ID。
直接emit脚本返回的record_response_ref，不手写RecordResponse、不抽取正文。

脚本对original/产生完整原文件目录；知识点表还给出真实ID、名称、章节目录。
对revision/仅登记当前路径和可明确识别的知识点ID，不读取教学内容作为事实；对supplements/登记当前修订要求。
不要根据全局目标为其他文件造目录，不把系统提示词的示例提取成知识点任务。
脚本输出的引用区间为空，原始权限由完整inputs承载，不填写整表行号形式的evidence_spans。
脚本失败时报告实际错误，不能自行生成另一套配置或把空目录当作没有业务数据。
