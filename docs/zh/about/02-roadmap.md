# 路线图

## 当前能力

- Agent 协议：租户 Key + `user_id` 鉴权的 Session 上报、Agent Context、Replay 分支与 Skill 拉取
- 数据源接入：PostgreSQL 版本化 Adapter、预览、拉取与每日定时拉取
- Skill 进化：Session 分析、按 Skill 分组、Agent 循环改进与新建、冲突合并、测试集生成
- True Replay：Baseline 与 Candidate 独立工作区对比，Checklist 门禁与效率比较
- Skill 版本：追加式版本链、幂等变更、回滚与 tombstone，Agent 通过 pull 获取
- 团队 Memory：跨用户聚合、DreamCycle 维护、变更记录与 Memory Replay
- 成功经验：经验库、Pattern 编译与 OpenViking 投影
- 知识库挖掘：文档上传转换、OpenViking Compile 与 Wiki 图谱
- 控制台：实验工作台、Prompt Studio、个人与团队资产、平台资产、租户与成员管理
- 运行时：API 与 Worker 角色分离，PostgreSQL 任务队列、租约与围栏，支持多副本

## 待完成

- 真实 OpenViking 环境的端到端验收（Compile、检索清理、ACL）

## 长期方向

- 跨组织的 Skill 验证与共享
- 自动发现团队能力瓶颈并发起进化
