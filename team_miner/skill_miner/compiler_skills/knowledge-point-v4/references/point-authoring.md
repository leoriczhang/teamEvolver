# 教学流程写作阶段

仅适用于synthesize。读取assignment.json，使用本次唯一point-batch的实际顶层ID调用prepare_point.py，mode=prepare。
不要传URI、原文行号或新编ID。脚本只使用当前输入已继承的原始权限，不能替它绕过缺失的原表。

脚本准备的knowledge-config.json具有稳定结构：knowledge_point、tasks、scenes、chapters、coach_source_rows、
instructor_policy、source_manifest。任务和场景保持真实原始ID；描述、话术、背景、难度、人物键和原始字段完整保存。
任务表和场景表才提供业务配置；instructor_policy.text中的业务示例不能替换当前配置，也不能产生额外任务。
源码列数不一致的教练原始记录会保留raw_record和cells，不虚构字段对应关系。

create时SKILL.md已具备frontmatter、配置入口及事实边界，替换唯一__MINER_TEACHING_METHOD__即可。
阅读脚本返回的真实任务/场景及prepared/instructor-rules.txt，写清：选择场景和任务、确定互动模式与角色、
开场、逐任务提问引导、完成度判断、纠错与确认、偏离处理、下一任务推进、结束和JSON输出约定。
教学步骤应引用当前配置，不重抄长业务答案，不硬编码系统提示词里的示例、任务数量、人物或案例参数。
尊重原文的豁免、动作和条件，评分与教练职责分开，不添加原文未规定的通用完成门槛。

revise时原SKILL全文已经复制进当前scratch。先读取，再用edit_file插入或替换确需修改的位置，不能缩写重写。
原配置没有变更要求就不返回它；新增使用约定标为本轮约定，不冒充原材料事实。
不要修改未派发知识点。只输出本知识点变更文件及脚本准备的analysis说明。

每次响应只做一次write_file/edit_file，新增最多1200字符。长文件继续分段，不能无工具预演整篇。
FileResponse使用files=[{path,content_ref,inputs}]，content_ref来自当前Agent真实scratch，inputs为唯一point-batch ID。
配置和analysis引用脚本准备的文件，不重新抄写。SKILL.md大写，目录compiled_skill/knowledge-原始ID/；
analysis放根analysis/knowledge-batches/。prepared/是内部读写文件，不发布，不生成benchmark或验证案例。
