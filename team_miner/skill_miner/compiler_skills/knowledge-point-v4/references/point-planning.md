# 确定本轮知识点集合

仅适用于reduce。完整读取assignment.json，定位已在inputs[].payload.fields中，不调用read_evidence。
初次构建从知识点原表目录取ID和名称，按本次mining_intent选题；增量由当前明确修订反馈确定write_set。
target_count是任务总范围，不是增量轮必须重新生成的数量。未修改知识点不派发复制、检查或保留Agent。

每个知识点一条point-batch，operation明确create/revise。每条inputs必须包含所有六份原始文件目录，
这样脚本才能在原任务表、场景表、章节表及教官规则中独立核实，不把缺少访问权限误判成原表没有数据。
不要替写作者在本阶段猜任务或场景归属。场景可关联多个知识点，不创建任务—场景单父树。
增量仅挂当前知识点自己的旧稿目录和真实反馈；其他知识点旧稿不进入该写作者的父链。

只传知识点身份、稳定目录、操作和短修订要求，不复制原文或原始source_range数组。
write_set超过4项时必须先建dispatch.json，再逐条edit_file追加；每次最多一条且新增不超过1200字符。
单条过长可继续分段，不能截断父链。使用唯一尾标记，不逐条回读，只在最后闭合、完整回读。
最终一次emit提交完整result_ref，不能先emit第一条。小名单可直接emit完整records。
提交前检查ID集合与write_set相同且无重复，每条都含六个原文件目录；范围不明确就记录具体问题，不默认全量重写。
