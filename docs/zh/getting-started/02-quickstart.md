# 快速开始

本指南将在 5 分钟内帮你在本地跑起一个 teamEvolver 实例，接入 Hermes Agent，并完成第一次进化闭环。

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
teamEvolver config sharing.viking_endpoint "http://localhost:1933"
teamEvolver config sharing.viking_api_key "your-openviking-key"

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

## 4. 打开控制台

浏览器访问 [http://localhost:52010/console](http://localhost:52010/console)，可以看到：

- 服务运行状态、排队会话数、已注册技能数
- 会话历史队列
- 待审核的 Skill Candidate
- 进化链路配置面板

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

这会立即触发一次进化周期：从队列中取出 Session → 提取 Evidence → 生成 Candidate → True Replay 验证 → 等待审核。

## 7. 停止服务

```bash
teamEvolver stop
```

## 下一步

- [核心概念](../concepts/01-architecture)：深入理解进化闭环、Skill、Memory、True Replay
- [配置参考](../guides/01-configuration)：所有配置项的完整说明
- [Agent 接入](../agent-integrations/01-overview)：将你自己的 Agent 接入 teamEvolver
