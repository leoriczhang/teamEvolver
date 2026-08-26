# 安装部署

## 环境要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 运行 teamEvolver 后端 |
| pip | 23.0+ | 安装 Python 包 |
| Node.js | 18+ | 仅修改前端时需要 |
| OpenViking | 兼容当前 Content、Session、Snapshot 与 Compile API | 持久化后端，本地、远程自建或火山云 |
| 操作系统 | Linux / macOS | Windows 需 WSL2 |

## 安装方式

### PyPI 安装

```bash
pip install teamEvolver
```

### 源码安装（使用当前仓库版本）

```bash
git clone https://github.com/leoriczhang/teamEvolver.git
cd teamEvolver
pip install -e ".[all]"
```

可选依赖分组：

| extra | 包含内容 |
|-------|---------|
| `sharing` | OpenViking 共享存储支持（boto3） |
| `mining` | Skill Miner 文档挖掘（hermes-agent） |
| `validation` | True Replay 验证（openai SDK） |
| `truereplay` | 完整 True Replay 能力 |
| `dev` | 开发依赖（pytest、anyio） |
| `all` | 全部依赖 |

### 前端构建（仅开发者需要）

普通安装使用预构建的前端产物，无需构建。如果你修改了 `web-ui/` 源码：

```bash
cd web-ui
npm install
npm run build
# 产物输出到 teamEvolver/web/dist/
```

### Docker Compose

仓库根目录提供 `Dockerfile` 与 `compose.yaml`。镜像会构建控制台、安装 `.[all]` 依赖并捆绑固定版本的 OpenViking CLI：

```bash
docker compose up -d --build
docker compose ps
```

默认映射宿主机端口 `52010`，可通过 `TEAMEVOLVER_PORT` 修改。配置与 SkillMiner 运行产物保存在 `runtime/` 挂载目录；升级镜像不会删除这些数据。

## 初始配置

使用 `teamEvolver config <key> <value>` 写入配置，或直接编辑 `~/.teamEvolver/config.yaml`。CLI 不是交互式向导；第一次执行任意设置命令时会创建配置文件。

### 最小可运行配置

```yaml
service:
  port: 52010
  host: 0.0.0.0

llm:
  provider: custom
  model_id: doubao-seed-evolving
  api_base: https://ark.cn-beijing.volces.com/api/v3
  api_key: "your-llm-api-key"

sharing:
  enabled: true
  backend: viking
  viking_deployment: local
  viking_endpoint: http://localhost:1933
  viking_account: default
  viking_user: team
  viking_team_api_key: "your-service-or-admin-key"

evolve:
  publish_mode: validated
  human_review_enabled: true

validation:
  enabled: true
  mode: true_replay

aggregation:
  enabled: true
  shared_knowledge_prefix: shared-knowledge
```

### 配置项位置

| 路径 | 说明 |
|------|------|
| `~/.teamEvolver/config.yaml` | 主配置文件 |
| `~/.hermes/skills/` | 默认本地 Skill 目录 |
| `~/.teamEvolver/aggregation/` | 团队记忆聚合 Skill、指纹与增量状态 |
| `~/.teamEvolver/teamEvolver.pid` | Daemon PID 文件 |
| `~/.teamEvolver/teamEvolver.log` | 默认 Daemon 日志 |

## 启动与管理

### 前台启动（调试用）

```bash
teamEvolver start
```

### 后台守护进程

```bash
teamEvolver start --daemon
```

### 查看状态

```bash
teamEvolver status
```

### 停止服务

```bash
teamEvolver stop
```

### 查看日志

```bash
# Daemon 模式下
tail -f ~/.teamEvolver/teamEvolver.log
```

## 验证安装

```bash
# 1. 检查服务健康
curl -fsS http://localhost:52010/health

# 2. 检查状态
curl -fsS http://localhost:52010/status | python -m json.tool

# 3. 访问控制台
# 浏览器打开 http://localhost:52010/

# 4. 运行测试（源码安装时）
python -m pytest tests/ -v
```

## 升级

```bash
pip install --upgrade teamEvolver
# 或源码安装：
cd teamEvolver && git pull && pip install -e ".[all]"

# 重启服务
teamEvolver stop && teamEvolver start --daemon
```

## 相关文档

- [配置参考](../guides/01-configuration)：所有配置项的完整说明
- [生产部署](../guides/02-deployment)：生产环境部署最佳实践
