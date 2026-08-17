# 安装部署

## 环境要求

| 组件 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 运行 teamEvolver 后端 |
| pip | 23.0+ | 安装 Python 包 |
| Node.js | 18+ | 仅修改前端时需要 |
| OpenViking | 0.4+ | 持久化后端，本地或云端 |
| 操作系统 | Linux / macOS | Windows 需 WSL2 |

## 安装方式

### pip 安装（推荐）

```bash
pip install teamEvolver
```

### 源码安装

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

## 初始配置

运行 `teamEvolver config` 交互式设置，或直接编辑 `~/.teamEvolver/config.yaml`。

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
  viking_api_key: "your-openviking-key"

evolve:
  enabled: true
  publish_mode: validated
  human_review_enabled: true

validation:
  enabled: true
  mode: true_replay
```

### 配置项位置

| 路径 | 说明 |
|------|------|
| `~/.teamEvolver/config.yaml` | 主配置文件 |
| `~/.teamEvolver/skills/` | 本地 Skill 缓存目录 |
| `~/.teamEvolver/teamEvolver.pid` | Daemon PID 文件 |
| `~/.teamEvolver/logs/` | 日志目录 |

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
tail -f ~/.teamEvolver/logs/teamEvolver.log
```

## 验证安装

```bash
# 1. 检查服务健康
curl -fsS http://localhost:52010/health

# 2. 检查状态
curl -fsS http://localhost:52010/status | python -m json.tool

# 3. 访问控制台
# 浏览器打开 http://localhost:52010/console

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
