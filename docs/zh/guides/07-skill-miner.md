# 知识库挖掘指南

知识库挖掘把 OpenViking 中的原始知识目录编译为结构化 Wiki。编译由 OpenViking 原生 Compile 使用挖掘 Skill 执行；teamEvolver 负责受理、权限、任务持久化、取消与结果核对。代码位于 `team_miner/`，控制台页面位于 `team_miner/frontend/`。接口细节见 [知识库挖掘 API](../api/22-knowledge-mining.md)。

## 前置条件

- 租户已配置 OpenViking 部署、Account 与 Root Key（「运行状态」→「OpenViking 部署」）
- OpenViking 服务端支持 Compile；默认挖掘 Skill `viking://agent/skills/llm-wiki`
  缺失时由 teamEvolver 自动安装，自定义 Skill 仍需预先安装
- 当前用户是租户管理员
- Worker 角色正在运行，负责执行 `miner.track_compile` 与 `miner.rebuild_index` 任务

未配置 OpenViking 时，控制台的「知识库挖掘」菜单显示未配置标记，页面引导到「运行状态」。

## 目录约定

一次挖掘涉及三个 URI，均来自租户 `miner` 模块配置：

| 字段 | 默认值 | 作用 |
|------|--------|------|
| `source_uri` | `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | 知识源，存放原始文档转换后的 Markdown |
| `target_uri` | `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | 知识库，存放编译产出的 Wiki |
| `skill_uri` | `viking://agent/skills/llm-wiki` | 编译使用的挖掘 Skill |

`source_uri` 与 `target_uri` 必须位于 `viking://resources/` 下，`skill_uri` 必须位于 `viking://agent/skills/` 下。配置没有写入接口，由运维直接修改 `tenant_configs` 中 `module='miner'` 的行；配置中出现凭证字段或未知字段时，所有挖掘接口返回 503。

teamEvolver 在首次浏览、上传或启动挖掘前按父目录到子目录的顺序检查
`source_uri` 与 `target_uri`；目录不存在时自动创建，已存在时直接复用且不覆盖内容。

## 操作流程

### 1. 上传知识源

在「知识源」文件树中选择目标目录，上传文件或整个文件夹。支持格式：`.md`、`.markdown`、`.txt`、`.docx`、`.xlsx`、`.pdf`、`.csv`、`.pptx`。单次最多 200 个文件，单文件 ≤20 MiB，合计 ≤100 MiB。

上传受理为后台任务。Worker 逐个文件转换为 Markdown（扩展名替换为 `.md`）并写入 OpenViking，每个文件完成后记录检查点；任务中断后重试会跳过已写入的文件。两个文件转换后同名时请求被拒绝。

### 2. 整理知识源

文件树支持预览、编辑与删除文件或目录。根目录本身不能删除。编辑后的内容直接写回 OpenViking。

### 3. 启动编译

填写「本次挖掘要求」（可选，≤4000 字符），选择增量编译起点后启动。首次运行为全量编译；之后默认使用上次成功编译的开始时间作为 `last_compile_time` 水位，只处理新增或变更内容。

编译任务的执行过程：

1. API 持久化请求并入队 `miner.track_compile`，返回 `job_id`。
2. Worker 以 `job_id` 作为幂等键向 OpenViking 提交 Compile，并保存上游 `task_id` 回执。
3. Worker 轮询上游任务直到结束。进程重启后从回执恢复，不会重复提交。
4. 成功时更新水位，并派生 `miner.rebuild_index` 任务重建 Wiki 链接索引。

页面显示 OpenViking Compile 阶段进度。点击停止会同时取消上游任务和本地任务。

### 4. 查看知识库

编译完成后在「知识库」文件树浏览 Wiki 页面，查看反向引用。点击生成图谱会根据 Wiki 链接构建知识库 HTML 图谱。

## 并发与幂等

- 所有写操作支持 `Idempotency-Key` 请求头：相同 Key 加相同请求返回同一任务，不同请求返回 409。
- 同一 `(account_id, source_uri, target_uri, skill_uri)` 范围的水位与索引更新使用 PostgreSQL advisory lock 串行化。
- 只有与当前水位对应的编译任务会写入索引，较早完成的任务不会覆盖较新的索引。

## 故障排查

| 现象 | 处理 |
|------|------|
| 所有接口返回 503 | 检查 `miner` 模块配置是否缺失、包含凭证字段或未知字段 |
| `403 TENANT_ACCOUNT_MISMATCH` | 请求中的 `account` 与租户生效的 OpenViking Account 不一致 |
| 任务一直排队 | 确认 Worker 角色在运行，用 `teamEvolver status` 查看进程 |
| 任务失败 `miner_compile_failed` | 在 OpenViking 侧查看上游任务日志；自定义挖掘 Skill 需确认已安装 |
| 上传失败 `miner_normalized_upload_limit` | 转换后的 Markdown 合计超过 100 MiB，拆分上传 |

## 相关文档

- [知识库挖掘 API](../api/22-knowledge-mining.md)
- [Workspace 与平台资产 API](../api/23-workspace-assets.md)
- [Web 控制台用户指南](./03-console.md)
