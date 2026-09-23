# True Replay

True Replay 对每个 Test Dataset Case 打开相互隔离的 Baseline/Candidate 会话，
使用相同的 query、材料、Context、模型、工具及限制，唯一差异为 treatment。

1. 工厂创建会话；第一轮只发送数据集 query。
2. 收集真实 Agent 回复、轨迹、产物和指标。
3. 独立裁判按 Checklist 给出逐项证据，确认完成门禁。
4. 未完成时确定性选择当前允许披露的要求，独立用户模拟器生成自然反馈。
5. 反馈写入 user 历史，继续执行；完成或达到限制后结束并清理会话。

客户适配器不接触 Checklist、裁判状态或未披露要求。
反馈只表达当前目标与缺口，不含条目 ID、评分和 A/B 内部词，也不虚构肯定。
缺失原始指标记为 unavailable；裁判失败时 fail closed。
效率比较基于交互次数、单调时钟耗时、真实工具调用与 Token 等客观数据。

Candidate 独自完成则 accept，Baseline 独自完成则 reject；双方未完成或裁判不可用则
inconclusive；双方完成后才比较效率。每个结论保留证据、对话、披露与工厂源码 revision。
接口、权限及绑定见 [Replay API](../api/05-replay-branch.md)。
