---
name: te-agent-evolution-setup
description: 将真实业务 Agent 的 AgentScope preset、Langfuse 会话、Prompt 和上下文组件接入 teamEvolver，验证会话归属及 True Replay。用于新增业务 Agent、共用数据源和排查匹配失败。
---

## 收集一次真实调用

确认租户、TE 地址、业务 Agent 标识、Runtime、执行身份 user_id、preset 的项目、code 和确切版本。业务 Agent ID 与 Replay Adapter 可执行的 ID 必须一致。user_id 来自执行身份配置，最终客户的用户 ID 可单独保留在 Trace。认证从本机已配置的环境或 Secret Ref 获取。用同一次调用记录渲染前的变量、最终模型请求和实际返回。变量取自业务调用参数；保留换行、空格、类型和消息顺序。

在本地创建私有 sample.json。下例演示字段，把示例值换成一次真实调用的值。input_messages 是此前已经发给模型的公开历史，保留实际说话人前缀。current_user_message 是本次业务输入，final_request 是模型传输层收到的 JSON，去掉 HTTP 头。defaults 仅用于 preset 没有定义默认值的系统变量，按实际业务配置填写。若业务代码会在当前输入前加前缀，提供 message_prefixes。

```json
{
  "variables": {"rule": "按任务逐项判定", "query": "请判断这段对话"},
  "defaults": {"rule": "按任务逐项判定"},
  "input_messages": [],
  "current_user_message": "请判断这段对话",
  "final_request": {"model": "业务实际模型ID", "messages": [
    {"role": "system", "content": "规则：按任务逐项判定"},
    {"role": "user", "content": "请判断这段对话"}
  ]}
}
```

## 核对模板并登记

从本仓库根目录、安装了 TE 的 Python 环境运行脚本。线上读取 preset 时提供平台签发的 PRESET_TOKEN 和项目 ID。离线使用同一项目导出的 DTO，将 --preset-url 换为 --preset-file。脚本核对 preset code/version、模板渲染后整串文本、消息角色和顺序，再生成登记 JSON。模型参数来自最后实际发送的请求。遇到额外的 system/developer 消息、工具交互或不支持的参数时，先补齐业务适配器的执行契约。

```bash
PYTHONPATH=. python .agents/skills/te-agent-evolution-setup/scripts/prepare_profile.py \
  --preset-url "$PRESET_BASE" --project "$PRESET_PROJECT" --code "$PRESET_CODE" --version "$PRESET_VERSION" \
  --profile-id "$TE_PROFILE" --agent-id "$BUSINESS_AGENT_ID" --user-id "$EXECUTION_USER_ID" \
  --runtime-type "$BUSINESS_RUNTIME" --sample sample.json --out registration.json \
  --te-url "$TE_URL" --tenant "$TE_TENANT"
```

默认只预览。要通过此命令登记，在核对同一份输入后指定新的 --out 路径并加 --register。命令行登记使用管理员本机的 TEAMEVOLVER_ROOT_API_KEY，并明确发送 X-Tenant-Id。也可以省略 --te-url，以普通租户管理员身份在「登记现有业务 Agent 执行基线 → 从登记 JSON 预览」导入并登记；业务进程使用本租户的机器令牌。纯 Prompt 使用空的 affected_skill_ids。userPromptTemplate 渲染后的内容保留在 user 消息，逐次变化的值随 Session 上报。

在执行配置中点「以此配置登记变量上下文」，选择需要独立演进的规则、说明等变量，输入每个变量的真实取值。预览会生成新的 profile_id；按「登记」后获取新配置对应的 integration，并让 Reporter 记录实际加载的 release。API 使用同一请求体先 POST 到 execution-profiles/{id}/variable-contexts/preview，再 POST 到 variable-contexts。传 source_release_id、派生 profile_id、managed_variables、variable_values 和 evolution_scope。variable_values 按变量名提供取值数组。对话和当前用户输入随每条会话保留。

## 共用一个 Langfuse 项目

同一项目、同一凭证可以共用一个连接。运维复制 `session_ingestion/adapters/example-shared-langfuse.py`，将副本的 `SOURCE["enabled"]` 设为 `True`，在 `trace_names` 写入每个 Agent 实际使用的 Trace 名称，配置项目和凭证引用，再通过 Root 源码 API 登记文件。租户管理员在「数据源接入」选中已登记的文件和版本、保存绑定并测试连接。首次选择小时间窗。若需指定一个 Agent，使用 `agent_id` 过滤器匹配 Trace 上报的业务身份。接不同项目且需要独立凭证、过滤或计划时，使用独立命名连接。

在业务进程的公共调用入口接入 examples/prompt-evolution/java。用实际加载的 release 与组件信息构造 teamevolver_execution 元数据，在 Langfuse 的 GENERATION 记录最终 messages 和输出。管理员可用 integration 查看登记信息。业务 Reporter 使用机器令牌从 GET /sync/skills 读取已发布组合，核对服务端 tenant_id。每次换版等新模板真正加载后再切换上报的 release。跨租户、无法确认实际使用版本、缺少观测或内容不符时保留诊断，禁止给旧会话补贴新版身份。业务现有 SDK 负责记录 GENERATION。Trace 元数据可以由同一 SDK 写入或通过 Reporter 的 upsert 写回同一个 trace_id。

## 从拉取验收到 Replay

1. 在数据源页使用窄时间窗预览，确认两个业务 trace_name 都在白名单；提交一次同窗拉取，保存返回的 job_id。
2. 运行 `teamEvolver datasource --tenant "$TE_TENANT" --url "$TE_URL" watch "$JOB_ID"`。检查该次的归档、跳过、重复和失败。相同窗口重拉时，未变化的源快照应计入重复。
3. 在每个执行配置的「接入真实 Agent 的会话」中分别填入两个已归档的 TE Session ID，点「检查会话匹配」。两份配置与两条会话合计四个组合，各自的组合匹配、交叉组合不匹配。匹配表示原始请求和配置一致，进入进化还要满足价值判定等条件。
4. 绑定支持两个业务 ID 的 Replay Adapter。用各自来源 Case 验证，保留 Baseline/Candidate 的同一套变量、公开历史及输入，检查两侧实际执行回执、目标组件和模型。适配器与执行配置的查验细节见仓库根目录的 PROMPT_AGENT_MANUAL_SETUP.md。
5. 汇报分列入队、归档、会话匹配、True Replay 与效果判定。单条技术验收用于证明链路连通；业务效果使用冻结数据集及所选评分器。发布由业务另行操作。
