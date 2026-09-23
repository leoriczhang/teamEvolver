---
name: teamEvolver-feed
version: 1.0.0
description: 在 Hermes 会话结束时上报 Session v2。用户要求安装、检查或修改 teamEvolver 会话反馈 hook 时使用。
metadata:
  hermes:
    tags: [teamEvolver, Evolution, Session, Hook, Automation]
  requires:
    bins: ["python3"]
---

# teamEvolver-feed

使用已有服务地址、租户 Key 和 user_id 安装 hook；只询问尚缺的信息。
安装目录名称沿用 `teamEvolver-feed`，由安装器复制本目录中的文件。

```bash
# --user-id、--url、--tenant-token 都是必填（tevt_ 开头的租户机器凭证）
python3 install.py --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID" --tenant-token "$TEAMEVOLVER_TENANT_TOKEN"

# 自定义 Hermes home（默认 $HERMES_HOME 或 ~/.hermes）
python3 install.py --url "$TEAMEVOLVER_URL" --user-id "$TEAMEVOLVER_USER_ID" \
  --tenant-token "$TEAMEVOLVER_TENANT_TOKEN" --hermes-home /path/to/.hermes
```

feed.json 的身份配置只有 `base_url`、`tenant_token`、`user_id`，权限为 0600。
环境变量可用 `TEAMEVOLVER_URL`、`TEAMEVOLVER_USER_ID`、`TEAMEVOLVER_TENANT_TOKEN`。
安装器同时配置 Context provider；`--no-context-provider` 可关闭。
租户 Key（`tevt_...`，从控制台租户页或单租户部署的 `TEAMEVOLVER_TENANT_TOKEN` 取得）
只保存在配置中，不能写进用户记忆或日志。

> ⚠️ **为什么必须授权**：Hermes 会把每个 shell hook 用
> `<hermes-home>/shell-hooks-allowlist.json` 做首次使用授权，**未授权的 hook 会被静默跳过**。
> 当 Hermes（Agent）非交互地安装本 skill 时，没有 TTY 来弹出授权，hook 就永远不会触发。
> 所以 `install.py` 会顺带写入一条**只针对 `(on_session_end, 本命令)`** 的授权（不是全局
> auto-accept，不影响任何其他 hook）。若你确实想走 Hermes 原生 TTY 授权，加 `--no-approve`。

`on_session_end` hook 只读 Hermes state.db，以 v2 envelope 上报真实轨迹与观测指标。
所有 Context 操作携带 user_id，spool 的 producer_id 仅为本地队列标识。
`--no-approve` 留待 Hermes 交互授权；默认仅授权安装的 hook 命令。

## 二、手动安装（不想用 install.py 时）

1. 把本目录（`SKILL.md` + `push_session.py` + `hermes_delivery.py`）拷到 `<hermes-home>/skills/teamEvolver-feed/`。
2. 在同目录写 `feed.json`（`<...>` 换成实际值）：

   ```json
   {
     "user_id": "<USER_ID>",
     "base_url": "http://<host>:52010",
     "tenant_token": "tevt_<tenant-machine-credential>"
   }
   ```

   - `base_url`：teamEvolver 进化服务地址（**必填，无默认**，向用户确认后填入）。
   - `user_id`：租户内的用户 ID（**必填，无默认**）。
   - `tenant_token`：租户机器凭证（`tevt_...`），用于 Session v2 上报与 Context Workspace。
     这是**全权的租户密钥**；`feed.json` 必须以 `0600` 权限保存，
     不要提交进版本库、不要贴到日志或看板里。缺失或不是 `tevt_` 开头时 hook 直接静默跳过。
3. 在 `<hermes-home>/config.yaml` 加入（若已有 `hooks:` 块则并入）：

   ```yaml
   hooks:
     on_session_end:
       - command: "python3 <hermes-home>/skills/teamEvolver-feed/push_session.py"
         timeout: 20
   ```

4. 在 `<hermes-home>/shell-hooks-allowlist.json` 加入一条授权（若文件不存在则新建，
   `approvals` 为数组），否则 hook 会被 Hermes 静默跳过：

   ```json
   {
     "approvals": [
       {
         "event": "on_session_end",
         "command": "python3 <hermes-home>/skills/teamEvolver-feed/push_session.py",
         "approved_at": "<UTC ISO8601，如 2026-07-22T07:00:00Z>",
         "script_mtime_at_approval": null
       }
     ]
   }
   ```

   `command` 必须与 `config.yaml` 里那条**逐字一致**（Hermes 按 `(event, command)` 精确匹配）。
   这只授权这一个 hook，不影响其他 hook。也可在有 TTY 的环境里正常对话一轮，
   由 Hermes 弹出授权提示手动同意。

## 三、验证

```bash
hermes hooks list                  # 应能看到 on_session_end -> push_session.py
hermes hooks test on_session_end   # 用合成 session_id 干跑一遍
```

`hermes hooks test` 用的是合成 `session_id`（DB 里没有），脚本会打印
`no foldable turns; skipped` —— 这是**正常**的，说明脚本被正确调用了。
真实验证：正常对话一轮后，看 teamEvolver 看板“会话历史”出现这条会话
（提交人 = 设置的 user_id，状态 queued）。
无凭证或服务不可达时检查本地 errors.log；修复配置后重试。

也可手动干跑一条真实会话（`<SID>` 换成 `state.db` 里的某个 session id）：

```bash
echo '{"session_id":"<SID>"}' | python3 <hermes-home>/skills/teamEvolver-feed/push_session.py
```

## 四、配置优先级（都不写死）

`push_session.py` 按以下顺序取值，靠前的覆盖靠后的：

1. 环境变量：`TEAMEVOLVER_URL` / `TEAMEVOLVER_USER_ID` / `HERMES_STATE_DB` / `TEAMEVOLVER_TENANT_TOKEN` / `TEAMEVOLVER_FEED_CONFIG`
2. 同目录 `feed.json`（`user_id` / `base_url` / `tenant_token` / `state_db`）
3. 内置兜底：**仅** `state_db`（默认 `<hermes-home>/state.db`）。
   `base_url` 和 `user_id` **没有兜底**——两者缺任一，hook 直接静默跳过，绝不猜测本机地址。

旧字段名（`user_alias` / `workspace_token` / `TEAMEVOLVER_USER` / `TEAMEVOLVER_WORKSPACE_TOKEN`）
在 rollout 期间仍会被读取，但新安装一律写 `user_id` / `tenant_token`。

## 五、脚本行为要点

- **只读** `state.db`（`mode=ro`），绝不写 Hermes 状态。
- 折叠规则：`user` → `prompt_text`，`assistant` → `response_text`；`system` / `tool` 消息不进正文；连续同角色合并，不丢内容。
- `title` 取首条用户消息首行（≤120 字），teamEvolver 原样展示。
- 服务不可达 / 无 turns / 未配 user_id、服务地址或租户 Key 时**静默跳过**（只在 `errors.log` 记一行），绝不影响 Hermes 正常运行。

## 六、改配置 / 停止

- 换 user_id / 服务地址 / 租户 Key（`tenant_token`）：改 `feed.json`（或用上面的环境变量覆盖），换用户记得同步更新记忆。
- 停止投喂：从 `config.yaml` 的 `hooks.on_session_end` 移除该条，或 `hermes hooks revoke`。
