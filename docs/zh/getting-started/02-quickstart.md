# 快速开始

本指南帮助你在本地启动 teamEvolver，连接 OpenViking，并进入可执行的 Skill / Memory 进化工作流。

## 前置条件

- Python 3.10+
- Node.js 18+（仅在修改前端时需要，普通安装使用预构建产物）
- 一个可访问的 OpenViking 实例（本地或云端）
- 一个 LLM API Key（Doubao/OpenAI 兼容接口）

## 1. 安装

```bash
pip install -e ".[all]"
```

安装完成后，`teamEvolver` CLI 可用。

## 2. 最小配置

```bash
# 设置服务端口
teamEvolver config service.port 52010
teamEvolver config service.host 0.0.0.0

# 配置 OpenViking 后端
teamEvolver config sharing.enabled true
teamEvolver config sharing.backend viking
teamEvolver config sharing.viking_deployment local
teamEvolver config sharing.viking_endpoint "http://localhost:1933"
teamEvolver config sharing.viking_account "default"
teamEvolver config sharing.viking_user "team"
teamEvolver config sharing.viking_team_api_key "your-service-or-admin-key"

# 配置进化用 LLM
teamEvolver config llm.api_base "https://ark.cn-beijing.volces.com/api/v3"
teamEvolver config llm.api_key "your-llm-key"
teamEvolver config llm.model_id "doubao-seed-evolving"
```

配置文件保存在 `~/.teamEvolver/config.yaml`。

## 3. 启动服务

```bash
teamEvolver start --daemon
```

验证服务运行：

```bash
curl http://localhost:52010/health
# {"status":"ok"}

curl http://localhost:52010/status
# {"running":true,...}
```

自建 OpenViking 在另一台机器时，将 `sharing.viking_endpoint` 改为该机器可达的 URL，例如 `http://10.0.0.8:1933`。也可以在登录后通过「平台治理 → 运行状态 → OpenViking 部署」保存并热重载这些设置。

## 4. 初始化并打开控制台

浏览器访问 [http://localhost:52010/](http://localhost:52010/)。首次访问会显示管理员初始化页，默认表单值为 `admin`；生产环境请设置强密码。完成初始化后可以看到：

- 「技能挖掘」中的知识源和挖掘任务
- 「进化闭环」中的运行总览、候选评审、Langfuse 和 Skills / 团队 Memory 自进化
- 「资产中心」中的 Agent 工作空间、Skill Lab、Memory Lab 和平台资产
- 「平台治理」中的模型、用户与权限、OpenViking 部署和运行状态
- 内置中英文文档阅读与搜索

![控制台总览](/assets/teamEvolver-console-dashboard.png)

## 5. 接入 Hermes Agent（可选）

如果你有 Hermes Coding Agent，可以安装同步和回流 Hook：

```bash
export TEAMEVOLVER_URL="http://localhost:52010"
export TEAMEVOLVER_USER="your-name"
export HERMES_HOME="$HOME/.hermes"

# 安装团队技能同步 hook
python teamEvolver/integrations/hermes_skill_sync/install.py \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --backend service \
  --url "$TEAMEVOLVER_URL" \
  --user "$TEAMEVOLVER_USER"

# 安装会话回流 hook
python teamEvolver/integrations/hermes_skill/install.py \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --user "$TEAMEVOLVER_USER" \
  --url "$TEAMEVOLVER_URL"
```

安装后重启 Hermes，新会话结束时会自动上报到 teamEvolver。详细说明见 [Hermes 接入指南](../agent-integrations/03-hermes)。

## 6. 手动触发进化

```bash
curl -X POST http://localhost:52010/trigger
```

这会立即触发一次进化周期：从队列取出 Session → 提取 Evidence → 生成 Candidate。`validated` 模式下 Candidate 随后进入 True Replay 与发布门禁。

## 7. 停止服务

```bash
teamEvolver stop
```

## Docker Compose

不需要本机 Python/Hermes 环境时，可使用仓库自带镜像。镜像会构建控制台、安装完整依赖并捆绑 OpenViking CLI：

```bash
docker compose up -d --build
docker compose ps
```

服务仍通过 `http://localhost:52010/` 访问，配置和 SkillMiner 产物持久化在 `runtime/`。

## 下一步

- [核心概念](../concepts/01-architecture)：深入理解进化闭环、Skill、Memory、True Replay
- [配置参考](../guides/01-configuration)：所有配置项的完整说明
- [Agent 接入](../agent-integrations/01-overview)：将你自己的 Agent 接入 teamEvolver
