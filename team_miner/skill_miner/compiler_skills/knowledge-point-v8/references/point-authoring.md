# 集中交付与局部修订

prepare_collection只调用一次，处理本次唯一point-collection。脚本顺序读取已分配原文，生成独立知识点目录。
每份配置保留真实任务、场景、原始字段和完整教官规则，基础SKILL引用这些原文并给出可执行分支流程。
首次构建不再使用模型改写基础流程，避免产生额外难度门槛和客户姓名自称，直接交付脚本准备的文件引用。

增量仅复制write_set对应旧SKILL及已有Markdown资源供局部修改，新配置留在prepared/仅作核对，不能返回原配置。
旧正文不能缩写重写。保持原始ID、难度标签、场景背景、答案、数字和规则，未涉及的文件不交付。
重复反馈已满足时保持正文及已有资源字节不变，只交付本轮分析；不能为显示工作量追加同义规则。
需新增调用约定时标明是本轮约定；不能冒充原文，不添加评分任务或完成门槛。

每次最多一次write_file/edit_file，新增不超过1200字符；长新增资源分段写。
最后调用manifest，extra_files只列当前write_set目录内新增references/*.md；已有Markdown若修改会自动列入，未改文件不返回；脚本生成合法FileResponse清单。
emit result_ref引用该清单，不手抄全部JSON。prepared/不发布，不生成benchmark或验证案例。
