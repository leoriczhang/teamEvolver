// 术语库：控制台内部/英文术语的中文解释。
// 界面中直接出现英文或工程概念时，用 <Term>/<InfoTip> 挂上这里的解释，
// 避免管理员需要翻文档才能理解。
export const GLOSSARY: Record<string, { label: string; desc: string }> = {
  "true-replay": {
    label: "True Replay（真回放）",
    desc: "在真实 Agent Runtime 中并行执行“当前技能（基线）”与“候选技能”，对比交互轮次、工具调用与 Token，是发布前的客观验证手段。",
  },
  checklist: {
    label: "Checklist（完成门禁）",
    desc: "从会话中提炼的完成条件清单。真回放中由裁判逐条判定是否满足，全部满足才算通过门禁；它是完成门禁，不是加权评分。",
  },
  "baseline-candidate": {
    label: "基线 / 候选（A/B）",
    desc: "基线（A）指当前线上技能；候选（B）指待评审的新技能版本。两者在同一批会话上回放做对比。",
  },
  ingest: {
    label: "Ingest（会话入队）",
    desc: "真实会话写入进化队列的入口阶段，随后进入分析、评分与分组。",
  },
  analyze: {
    label: "Analyze（会话分析）",
    desc: "一次 LLM 调用完成价值分类、轨迹摘要与质量评分，决定会话是否值得进入进化。",
  },
  group: {
    label: "Group（按技能分组）",
    desc: "按会话中引用或注入的技能归类，把同一技能的会话聚合成一个进化分组。",
  },
  evolve: {
    label: "Evolve（改进技能）",
    desc: "基于会话证据改进已有技能：优化正文、改写描述，或判断无需变更。",
  },
  create: {
    label: "Create（新建技能）",
    desc: "针对未命中任何技能的会话，判断是否存在可复用模式并生成新技能。",
  },
  merge: {
    label: "Merge（冲突合并）",
    desc: "同名技能出现两个进化版本时，合并为一个更优版本，避免互相覆盖。",
  },
  dataset: {
    label: "Dataset Synthesis（测试集生成）",
    desc: "从会话采样生成回放测试集，包含复现指令与 Checklist 完成条件，供真回放校验使用。",
  },
  validate: {
    label: "Validate（真回放校验）",
    desc: "在真实 Runtime 中并行重放基线与候选，逐条校验 Checklist 并比较三项效率指标。",
  },
  publish: {
    label: "Publish（发布）",
    desc: "通过校验的候选写入技能库并同步给 Agent；未通过则进入人工复核。",
  },
  "session-judge": {
    label: "Session Judge（会话裁判）",
    desc: "用 LLM 对会话做分类与质量评分（含中文评分理由），结果用于筛选进入进化的会话并展示评审结论。",
  },
  evidence: {
    label: "Evidence（证据）",
    desc: "从会话中提取、可追溯的事实依据，用于指导技能改进；“跨周期 Evidence”表示改进时会同时参考历史周期的证据。",
  },
  bundle: {
    label: "Bundle（技能包）",
    desc: "一次技能发布对应的完整文件集合，通常包含 SKILL.md、files/ 与 manifest.json 等。",
  },
  dreamcycle: {
    label: "DreamCycle（团队记忆维护）",
    desc: "团队 Memory 的自动维护周期：跨用户聚合、增量编译，以及 Memory 回放校验。",
  },
  "valuable-chitchat": {
    label: "valuable / chitchat",
    desc: "会话价值判别结果：valuable 进入进化队列，chitchat 不参与进化，仅保留审计记录。",
  },
};