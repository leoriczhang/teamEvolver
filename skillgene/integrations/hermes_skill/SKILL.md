---
name: skillgene-feed
version: 1.0.0
description: "把每次 Hermes 对话结束后的会话自动投喂给 SkillGene 进化服务（/ingest_session）。首次使用时记录你的用户名并写入记忆，之后每轮对话结束自动以该用户名上报会话，用于技能自进化。当用户提到 skillgene 投喂 / 会话进化 / 自动上报对话 / ingest_session / 进化用户名时使用。"
metadata:
  hermes:
    tags: [SkillGene, Evolution, Session, Hook, Automation]
  requires:
    bins: ["python3"]
---

# skillgene-feed — 把 Hermes 对话自动投喂给 SkillGene

这个 skill 让 **每次对话结束后**，Hermes 自己把刚结束的会话上报给 SkillGene 进化服务
（`POST <base_url>/ingest_session`）。SkillGene 拿到会话后跑技能自进化，产出的候选技能在
看板里由人工决定是否发布。**可分发**：同一份 skill 可装到任意机器的 Hermes 上，
所有主机相关设置（服务地址 / 用户名 / DB 路径 / 密钥）都不写死，全部可配置。

原理：注册一个 `on_session_end` shell hook，会话一结束就运行
[`push_session.py`](push_session.py)。该脚本用 stdlib（`sqlite3` + `urllib`，零第三方依赖）
从 Hermes 的 `state.db` 读出该 session 的消息，折叠成 SkillGene 的
`turns:[{prompt_text, response_text}]`，带上配置里的用户名 POST 过去。
队列以 `session_id` 为 key（覆盖写），所以每轮都推是**幂等**的——只会刷新为最新、最全的快照。

## 一、安装（推荐：用 install.py 一键装）

第一次使用时，**MUST 先向用户询问两个必填项**（都没有默认值，不要擅自假设）：

1. **用户名** `user_alias`——会显示在 SkillGene 看板“会话历史”里，标明会话是谁发的；
2. **SkillGene 服务地址** `base_url`——形如 `http://<host>:52010`，本机部署可能是
   `http://127.0.0.1:52010`，远程则填对应主机。**服务地址必须问用户确认，不要写死成本机。**

然后运行安装脚本（它会拷贝文件、写 `feed.json`、把 hook 并入 `config.yaml`，
并写入一条**仅针对本 hook 的授权**，使其无需 TTY 交互即可生效）：

```bash
# --user 和 --url 都是必填
python3 install.py --user <USERNAME> --url http://<host>:52010

# 服务端设了 EVOLVE_INGEST_API_KEY 时再加 --api-key
python3 install.py --user <USERNAME> --url http://<host>:52010 --api-key <KEY>

# 自定义 Hermes home（默认 $HERMES_HOME 或 ~/.hermes）
python3 install.py --user <USERNAME> --url http://<host>:52010 --hermes-home /path/to/.hermes
```

> ⚠️ **为什么必须授权**：Hermes 会把每个 shell hook 用
> `<hermes-home>/shell-hooks-allowlist.json` 做首次使用授权，**未授权的 hook 会被静默跳过**。
> 当 Hermes（Agent）非交互地安装本 skill 时，没有 TTY 来弹出授权，hook 就永远不会触发。
> 所以 `install.py` 会顺带写入一条**只针对 `(on_session_end, 本命令)`** 的授权（不是全局
> auto-accept，不影响任何其他 hook）。若你确实想走 Hermes 原生 TTY 授权，加 `--no-approve`。

装完后 **MUST** 调用 memory 工具把这个进化用户名写进记忆，例如：
`memory(action=add, content="SkillGene 会话投喂用户名（user_alias）= <USERNAME>")`
这样即使 `feed.json` 丢失也能快速重建。

## 二、手动安装（不想用 install.py 时）

1. 把本目录（`SKILL.md` + `push_session.py`）拷到 `<hermes-home>/skills/skillgene-feed/`。
2. 在同目录写 `feed.json`（`<...>` 换成实际值）：

   ```json
   {
     "user_alias": "<USERNAME>",
     "base_url": "http://<host>:52010",
     "api_key": ""
   }
   ```

   - `base_url`：SkillGene 进化服务地址（**必填，无默认**，向用户确认后填入）。
   - `api_key`：仅当服务端设了 `EVOLVE_INGEST_API_KEY` 时才填，否则留空。
3. 在 `<hermes-home>/config.yaml` 加入（若已有 `hooks:` 块则并入）：

   ```yaml
   hooks:
     on_session_end:
       - command: "python3 <hermes-home>/skills/skillgene-feed/push_session.py"
         timeout: 20
   ```

4. 在 `<hermes-home>/shell-hooks-allowlist.json` 加入一条授权（若文件不存在则新建，
   `approvals` 为数组），否则 hook 会被 Hermes 静默跳过：

   ```json
   {
     "approvals": [
       {
         "event": "on_session_end",
         "command": "python3 <hermes-home>/skills/skillgene-feed/push_session.py",
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
真实验证：正常对话一轮后，看 SkillGene 看板“会话历史”出现这条会话
（提交人 = 设置的用户名，状态 queued）。

也可手动干跑一条真实会话（`<SID>` 换成 `state.db` 里的某个 session id）：

```bash
echo '{"session_id":"<SID>"}' | python3 <hermes-home>/skills/skillgene-feed/push_session.py
```

## 四、配置优先级（都不写死）

`push_session.py` 按以下顺序取值，靠前的覆盖靠后的：

1. 环境变量：`SKILLGENE_URL` / `SKILLGENE_USER` / `HERMES_STATE_DB` / `EVOLVE_INGEST_API_KEY` / `SKILLGENE_FEED_CONFIG`
2. 同目录 `feed.json`（`user_alias` / `base_url` / `api_key` / `state_db`）
3. 内置兜底：**仅** `state_db`（默认 `<hermes-home>/state.db`）。
   `base_url` 和 `user_alias` **没有兜底**——两者缺任一，hook 直接静默跳过，绝不猜测本机地址。

## 五、脚本行为要点

- **只读** `state.db`（`mode=ro`），绝不写 Hermes 状态。
- 折叠规则：`user` → `prompt_text`，`assistant` → `response_text`；`system` / `tool` 消息不进正文；连续同角色合并，不丢内容。
- `title` 取首条用户消息首行（≤120 字），SkillGene 原样展示。
- 服务不可达 / 无 turns / 未配用户名或服务地址时**静默跳过**（只在 `errors.log` 记一行），绝不影响 Hermes 正常运行。

## 六、改配置 / 停止

- 换用户名 / 服务地址 / 密钥：改 `feed.json`（或用上面的环境变量覆盖），换用户名记得同步更新记忆。
- 停止投喂：从 `config.yaml` 的 `hooks.on_session_end` 移除该条，或 `hermes hooks revoke`。
