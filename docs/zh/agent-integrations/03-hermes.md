# Hermes 接入

准备服务地址、租户机器凭证和该租户内的 `user_id`。
Hermes 不需要 Agent 注册，也不持有 OpenViking Root Key。
在仓库根目录执行：

```bash
export TEAMEVOLVER_URL="https://teamevolver.example.com"
export TEAMEVOLVER_USER_ID="alice"
# TEAMEVOLVER_TENANT_TOKEN is supplied securely by your operator.

python session_ingestion/push/hermes/install.py   --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID"   --tenant-token "$TEAMEVOLVER_TENANT_TOKEN"

python teamEvolver/integrations/hermes_skill_sync/install.py   --backend service --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID"   --tenant-token "$TEAMEVOLVER_TENANT_TOKEN"

hermes hooks list
hermes hooks test pre_llm_call
hermes hooks test on_session_end
```


feed 安装器同时安装 Context provider；`--no-context-provider` 可只安装 Session hook。
三个身份设置为 `base_url`、`tenant_token`、`user_id`，配置文件权限为 0600。
`--hermes-home` 可选择其他 Hermes 目录。安装器只授权安装的 hook 命令；`--no-approve` 留待交互授权。

Session hook 使用 v2 envelope，Context 九个端点携带 user_id。
本地 spool 的 `producer_id` 只用于幂等与队列分区，不作为服务端身份。
Skill pull 缓存版本与内容哈希，未变化的文件保持不动；最小间隔 15 秒，在下一次模型调用前刷新。

合成 `on_session_end` 测试没有真实 DB Session，skipped 是预期结果。
验收需要实际完成一个 Session，确认服务端出现对应 user 的 Session；
再发布一个 Skill，并在后续模型调用前确认本地已拉到新版本。
详见 [v2](./06-protocol-v2.md)、[Context](../api/04-context-workspace.md) 与 [Skill pull](../api/06-skill-sync.md)。
