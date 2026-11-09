# Prompt 进化操作 README

适用对象：已经部署 TE、PostgreSQL、OpenViking、Langfuse 和业务 Agent，希望把业务 Prompt 与规则、示例等注入内容接入现有进化链路的管理员和开发者。

本文操作顺序是：准备接入 → 登记 Prompt → 登记上下文 → Agent 产生真实会话 → TE 拉取并匹配 → 生成候选 → True Replay → 人工审阅 → 发布 → Agent 拉取采用。

## 1. 先回答：能不能全部通过界面完成

**首次接入不是从零开始的全界面部署。完成一次性的运维和 Agent 适配以后，日常登记、拉取、进化、评分配置、审阅与发布可以通过界面完成。**

| 工作 | 当前怎么做 | 成功标志 |
| --- | --- | --- |
| 部署 TE、数据库、OV、Langfuse、业务 Agent | 按现有部署流程，由运维完成 | API 和 Worker 可用，Worker 能访问外部服务 |
| 首次登记三类 Python 适配器文件 | 运维通过服务 Root Key 调用源码 API | 文件出现在对应租户的下拉列表 |
| 选择已登记的数据源和 Replay 适配器 | 控制台 | 保存绑定后显示已配置 |
| 登记业务 Prompt、上下文变量取值 | 控制台「进化链路」 | 预览通过，生成新的执行配置 |
| 让 Agent 上报实际模板、变量、模型请求 | 业务 Agent 开发者一次性适配 | Langfuse 有原始 GENERATION 请求，TE 检查匹配成功 |
| 设置 Replay 所需的特殊运行参数和凭证 | 首次由运维配置；界面绑定只选择文件 | 两个分支真正运行且实际加载验证通过 |
| 拉取会话、立即进化、评估候选、审阅和发布 | 控制台 | 对应任务、候选和 Replay 结果可查看 |
| 把发布内容加载到线上 Agent | Agent 开发者接好拉取与加载逻辑 | 后续实际请求使用新版本，产生真实反馈 |

控制台登录账号与服务 Root Key 是不同的身份。普通管理员能选择已登记文件；源码操作要求服务 Root Key，因为这些 Python 文件在 Worker 内执行。**不要把 Root Key 填进浏览器、适配器源码或 Prompt。**

把 `.py` 放进 TE 项目目录不会自动出现在下拉框。源码版本和绑定以 PostgreSQL 为准。发布也不会自动改写业务 Agent 的源码或重启业务 Agent。

## 2. 开始前准备这张清单

在同一个目标租户准备：

- 业务 Agent 的稳定 ID、Runtime 类型、执行用户 ID。
- 当前正在使用的业务 System/Developer Prompt 原文，保留 `{{变量名}}`。
- 每个变量的实际默认值；要进化的规则、示例、参考资料分别有哪些原始取值。
- Agent 实际模型及采样配置；如有工具，准备实际工具配置。
- 能读取业务 Agent 项目的 Langfuse 数据源适配器。
- 能创建隔离会话、覆盖候选组件并观测最终模型请求的 Replay 适配器。
- 选择外部评分时，准备评分器地址、输入输出协议和必要的 Secret Ref。

只做 Prompt 进化时，关联 Skill 可以为空。业务 Agent 的 Prompt 与 TE 自己用于分析、生成候选、裁判的 Prompt 是两类内容，本文登记的是前者。

浏览器能访问 TE，不代表 TE Worker 能访问 Langfuse 或 Agent。远程部署中的 `127.0.0.1` 指对应服务所在机器；容器中的它指容器自己。地址应从 Worker 的网络视角填写。

先看「运行状态」中的服务、模型与 OpenViking 状态。已有服务正常时继续使用现有部署。需要从零部署时，参见[部署指南](docs/zh/guides/02-deployment.md)。

## 3. 分清三种适配器

| 适配器 | 做什么 | 文件入口 | 界面入口 |
| --- | --- | --- | --- |
| 数据源适配器 | 从 Langfuse 拉取原始 Session | `SOURCE` + `build_adapter()` | 「数据源接入」 |
| 业务 Agent Replay 适配器 | 用原版本和候选版本分别运行同一道题 | `REPLAY_ADAPTER` + `build_replay_adapter(config)` | 「运行状态 → Replay 运行环境」 |
| 外部评测器适配器 | 把两边真实回复交给评分 Agent，转换其返回格式 | `EVALUATOR_ADAPTER` + `evaluate(request, config, *, secret="")` | 「进化链路 → A/B 回放评价方式」 |

数据源连通不等于 Replay 连通；评分器也不负责生成业务 Agent 的回答。三个文件的职责应分别实现。

### 3.1 数据源文件怎么写

使用内置 Langfuse 传输，保存为 `business-langfuse.py`：

```python
from session_ingestion.adapters._shared.builders import build_langfuse_adapter

SOURCE = {
    "label": "业务 Agent Langfuse",
    "provider": "langfuse",
    "host": "https://langfuse.example.com",
    "enabled": True,
    "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    # 改成业务 Agent 在 Langfuse 中实际使用的 Trace 名称。
    return build_langfuse_adapter(SOURCE, "business-agent-turn")
```

运维在 **Worker 进程环境**中配置 `LANGFUSE_PULL_HOST`、`LANGFUSE_PULL_PUBLIC_KEY`、`LANGFUSE_PULL_SECRET_KEY`。实际连接由这些环境变量决定，`SOURCE.host` 用于描述，单改它不能替代连接配置。Public/Secret Key 必须属于业务会话所在项目。新增或修改进程环境后按部署流程更新 Worker；已有连接可直接复用。

TE 自身的 Langfuse 出站观测与这里的入站拉取相互独立，不要混用两套项目凭证。多项目接入可通过 `build_langfuse_adapter` 的 `options` 参数从各自的受控进程环境读值，参考[数据源指南](docs/zh/guides/10-datasource-onboarding.md)。

### 3.2 业务 Agent Replay 文件怎么写

优先对接已有业务 Agent，参考[Agent Lab 实现](team_replay/adapters/examples/agent_lab.py)。它展示了隔离会话、冻结计划、发送消息、验证远端真实请求和清理会话的完整做法。该文件默认只接受其列明的 Agent ID 与 Runtime；接其他 Agent 要修改文件中的绑定和接口映射，不能直接原样套用。

你的文件必须实现下面这几个动作：

1. `build_replay_adapter(config)` 返回工厂；`config` 是受理时冻结的 Replay 配置。
2. `open(context)` 为当前分支创建独立会话，应用 `context.execution_plan`。分别使用 `baseline`、`candidate` 分支的完整组合。
3. `send(user_message)` 继续这个会话，返回真实 `AgentObservation`；后续轮次沿用这个会话。
4. 在**最终模型传输处**记录实际请求，返回可验证的 `execution_receipt`。
5. `close()` 清理临时会话和工作区。

编写时重点核对：

- 工厂声明支持 `execution_plan`、`agent_prompt`、`context_fragment` 及对应请求观测能力。
- `context.execution_plan` 已物化本题变量；使用 `render_components` 或兼容装配器，保证上下文在原变量位置替换。
- 原版本和候选使用相同模型、工具、变量、公开历史及限制，只改变声明的候选组件。
- 本地掌握最终 HTTP 传输时，用 `context.request_observer.dispatch` 捕获并原样发送请求字节，再用 `make_execution_receipt` 生成回执。
- 业务 Agent 在远端时，观测点必须装在实际调用模型的传输处；参考 `accept_attested` 与 [远端观测代码](team_replay/remote_capture.py)。只把“候选已加载”写进 JSON 不构成实际加载证明。
- 原回答、人工标准答案、用户纠正、Checklist 和评分理由不能作为新回答的输入。
- 轮次、Token、工具调用数量填写真实值，缺失保留未知。

具体接口和能力声明见 [Replay API](docs/zh/api/05-replay-branch.md)，完整接口类型见 [hooks.py](team_replay/hooks.py)。`agent_lab.py` 所使用的远端服务需要提供创建 Replay 会话、发送消息、清理会话以及最终请求签名能力；普通聊天接口没有这些能力时，业务 Agent 开发者必须先补齐。

如果只是没有工具的模型请求，可以使用下面的**直接模型参考传输**，保存为 `business-replay.py`：

```python
from team_replay.adapters.examples.observed_http import ObservedHttpFactory

REPLAY_ADAPTER = {
    "label": "业务 Prompt 模型请求参考传输",
    "enabled": True,
    "capabilities": {
        "execution_plan": "teamevolver.execution-plan.v1",
        "component_kinds": ["agent_prompt", "context_fragment"],
        "request_observation": "local_dispatch",
        "skill_treatment": True,
    },
}


def build_replay_adapter(config):
    return ObservedHttpFactory({
        "endpoint": "https://model.example.com/v1/chat/completions",
        "runtime_type": "observed_http",
        "auth_profile": "BUSINESS",
    })
```

这个参考传输在 Worker 内调用配置的模型接口，鉴权由已有 `TEAMEVOLVER_AGENT_BUSINESS_REPLAY_API_KEY` 进程环境提供。它要求注册时提供 `environment.request`；**它验证的是这个模型请求，不代表已经重跑了具有检索、工具或脚本的完整业务 Agent**。完整业务 Agent 接入应采用前述同引擎方式。不要为了让测试通过，把原回答直接从文件读出来当作本次回复。

### 3.3 外部评测器文件怎么写

若评分服务原生接收 TE 的两分支协议，可以直接在界面选择「标准 HTTP 接口」，不需要额外文件。格式见 [HTTP 示例](team_replay/adapters/examples/http_evaluator.py)。

若评分器一次只评一个回答，保存下面的文件为 `business-grader.py`，修改 `/score` 的字段映射即可：

```python
import json

import httpx

EVALUATOR_ADAPTER = {"label": "业务评分器：逐分支调用", "enabled": True}


def evaluate(request, config, *, secret=""):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    result = {"request_id": request["request_id"]}
    with httpx.Client(timeout=config["timeout_seconds"], follow_redirects=False) as client:
        for branch in ("baseline", "candidate"):
            payload = {
                "case_id": request["case"]["case_id"],
                "query": request["case"]["query"],
                "inputs": request["case"]["inputs"],
                "agentOutput": request["branches"][branch]["final_response"],
                "rubric": config["rubric"],
            }
            body = bytearray()
            with client.stream("POST", config["url"], json=payload, headers={
                **headers, "Idempotency-Key": request["request_id"] + ":" + branch,
            }) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 1024 * 1024:
                        raise ValueError("scorer response exceeds 1 MiB")
                    body.extend(chunk)
            raw = json.loads(body)
            # 示例评分服务返回 {"score": 0或1, "reason": "实际评分依据"}。
            # 不重新编造评分规则；按你的评分器真实字段修改这两处映射。
            result[branch] = {"score": raw["score"], "reason": raw["reason"]}
    return result
```

这个文件只做接口转换。TE 要求每个分支都有 0–100 内的有限数值分数及非空理由；0/1 评分可以原样使用，界面阈值也按 0/1 量纲填写。例如最低分填 `0.8`，不是 `80`。

如果评分器需要 `expectedResult`，必须从人工标注或真实标准答案库按本题关联取得。上例可让评分服务根据 `case_id` 与 `inputs` 查询标准，或由适配器显式映射。**TE 不会自动把某个 Excel 当标准答案库，也不应从原版本或候选的回复推导标准答案。** 没有标准就报不可评分。

评分器只评分，不必接入 Langfuse。返回理由可包含真实逐项比对明细；仅返回 `score`、缺少 `reason` 时，需要在适配器里把真实明细转成理由文本。用量未知可以省略 `usage`，不要补造 Token。异常或缺分会显示无法确定，不自动换用另一评分方式。

### 3.4 运维怎样把文件登记到 TE

以下脚本保存为本地运维文件 `register_adapter.py`，使用已部署 TE 的 Root API。可以在能访问 TE 且持有对应文件的电脑运行，不要求一定登录 TE 服务器。

```python
import os
import sys
from pathlib import Path

import httpx

kind, filename = sys.argv[1:]
routes = {
    "source": "/api/datasource/code",
    "replay": "/api/replay-adapter/code",
    "grader": "/api/replay-evaluator-adapter/code",
}
file = Path(filename)
headers = {
    "Authorization": "Bearer " + os.environ["TE_ROOT_KEY"],
    "X-Tenant-Id": os.environ["TE_TENANT"],
}
with httpx.Client(base_url=os.environ["TE_URL"].rstrip("/"), headers=headers, timeout=30) as client:
    current = client.get(routes[kind], params={"file": file.name})
    if current.status_code == 404:
        revision = None
    else:
        current.raise_for_status()
        revision = current.json()["revision"]
    response = client.put(routes[kind], json={
        "file": file.name, "code": file.read_text(encoding="utf-8"),
        "expected_revision": revision,
    })
    response.raise_for_status()
    print(response.json())  # 返回文件名、revision等，不输出密钥。
```

在已安装项目依赖的 Python 环境执行：

```bash
export TE_URL='http://你的TE地址:52010'
export TE_TENANT='目标租户ID'
read -r -s -p 'TE 服务 Root Key: ' TE_ROOT_KEY
export TE_ROOT_KEY
python register_adapter.py source business-langfuse.py
python register_adapter.py replay business-replay.py
python register_adapter.py grader business-grader.py
unset TE_ROOT_KEY
```

第三个文件仅在使用自定义评分协议时必需。保存遇到 409 时重新读取后审阅修改再重试，不能盲目覆盖。三类文件各选清楚的名字；源码版本不可变，新文件版本不会自动替换正在运行的任务或既有绑定。文件保存到 PG 不需要因这一步重启 TE；变更进程环境或 TE 本身代码则按现有部署流程使 Worker 生效。

**升级 TE 仓库代码不会自动升级 PG 中已经保存的适配器源码。** 更新同名文件时，先重新运行上面的登记脚本保存新 revision，再按第 4 节明确选择新版本并保存绑定。不要只更新项目里的示例文件就认为线上适配器已经更新。

### 3.5 可选：需要鉴权时怎样取得 Secret Ref

有现成引用时直接复用正确租户、正确用途的引用；没有时，由运维在 **TE 服务器/容器内、与服务相同的数据库和主密钥环境**执行以下脚本。它是运维操作，不放进业务 Agent，不通过浏览器执行。主密钥必须与当前服务一致，不要临时另生成一套。

```python
import getpass
import os

from teamEvolver.bootstrap import load_bootstrap
from teamEvolver.database.pool import DatabasePool
from teamEvolver.database.transaction import TransactionManager
from teamEvolver.security import SecretBox, SecretRepository

cfg = load_bootstrap()
tenant = os.environ["TE_TENANT"]
master = os.environ.get("TEAMEVOLVER_SECRET_MASTER_KEY") or os.environ["TEAMEVOLVER_ROOT_API_KEY"]
plaintext = getpass.getpass("评分服务鉴权密钥（不回显）: ")
if not plaintext:
    raise ValueError("鉴权密钥不能为空")
pool = DatabasePool(dsn=cfg.storage_pg_dsn, schema=cfg.storage_pg_schema)
tx = TransactionManager(pool)
secrets = SecretRepository(tx, SecretBox(master))


async def save(conn):
    return await secrets.store(conn, tenant_id=tenant, plaintext=plaintext, purpose="external-replay-grader")


try:
    print(tx.run_tenant_tx(tenant, save).ref)
finally:
    pool.close()
```

记录打印的引用，在第 9 节的 Secret Ref 字段填写。数据库只保存密文和引用；不要输出 `plaintext`、`master` 或完整进程环境。

Replay 适配器的特殊配置同样需要运维按其源码契约保存。例如 Agent Lab 工厂读取 `replay.agent_lab.endpoint` 和 `replay.agent_lab.secret_ref`，Worker 再解析为内部 `control_secret`。该控制密钥与评分器密钥按各自用途登记。保存租户 `replay` 模块时使用 `ConfigRepository`，在租户事务中先读取当前配置，保留其他字段，再以原 revision 写入；不要改 YAML 假装完成 PG 配置，也不要直接把明文 `control_secret` 放进去。这里属于一次性运行环境接入，界面绑定按钮不负责创建这些参数。示例中的直接模型参考文件将非敏感地址写在文件里，因此不需要额外配置这个 Agent Lab 参数块。

## 4. 界面第一步：选对租户和连接

1. 登录控制台，在左侧确认目标租户。文件、Prompt、会话、候选都按租户隔离。
2. 打开「数据源接入」，在「适配器文件」选择已登记的 Langfuse 文件，在「适配器版本」核对版本，点击「保存绑定」。
3. 点击「测试已保存连接」。看到连接正常后，再填写时间窗口并预览；不要把连接测试当成会话已归档。
4. 打开「运行状态」，找到「Replay 运行环境」，选择业务 Agent 的 Replay 适配器，在版本下拉框核对版本并保存。同名文件升级时，选择标注「最新登记」的新版本；原「当前绑定」版本会保留到你明确保存为止。
5. 若适配器还需要地址、Agent SDK 路径或控制 Secret Ref，先由运维按该文件契约配置。这个下拉框只绑定文件，不会生成外部服务或替你填任意运行参数。

已有文件时，以上选择操作都可以在界面完成。没有上传/编辑按钮时，先让运维按 3.4 登记文件；不向浏览器提供 Root Key。

## 5. 界面第二步：登记当前业务 Prompt

1. 打开「进化链路 → Skills 自进化」。名称沿用原链路，业务 Prompt 也从这里进入。
2. 展开「登记现有业务 Agent 执行基线」。
3. 填「执行配置 ID」，例如 `support-prompt-base`。它是新基线的稳定标识，重复 ID 会冲突。
4. 填实际「Runtime 类型」「业务 Agent ID」「执行用户 ID」。Runtime 与 Replay 适配器支持的类型一致。
5. 纯 Prompt 场景将 Skill 名称留空；确实使用团队 Skill 时登记真实依赖。
6. 选择真实槽位，粘贴业务 Prompt 模板原文。例如：`你是售后助手，遵循以下规则：{{policyText}}`。
7. 表单识别出 `policyText` 后，填实际默认值；确实为空才点击明确使用空值。
8. 「当前受管理的业务上下文注入」用于独立附加消息。变量中的规则不要同时填进这个框，否则会重复注入；变量取值用下一节登记。
9. 填真实模型；高级执行环境按真实调用填写。参考：

```json
{
  "request": {"model": "业务Agent实际模型ID", "temperature": 0, "tools": []},
  "validation": {"strategy": "standard-v1"}
}
```

`tools: []` 只适用于确实没有工具的请求。变量默认值和共享模板契约由表单组装；高级配置可补充其他实际输入变量。不要放 `messages` 或明文凭证。模型名、工具和参数填写不真实，登记可能成功但后续 Replay 不会因此变得可用。

**现有 Agent Lab 是特殊的业务运行时，不能照抄上面的通用参数。** 它要求流式 usage 与 Agent 的说话人前缀，且不接受 `request.tools` 字段。模拟客户 `agent-one`／`agent-two` 的结构如下，模型与 `max_tokens` 必须替换为普通会话实际配置，不能为了测试任意改小再拿去发布：

```json
{
  "request": {
    "model": "Agent实际配置的模型ID",
    "max_tokens": 4096,
    "stream": true,
    "stream_options": {"include_usage": true}
  },
  "message_prefixes": {"user": "HWP：", "assistant": "C："},
  "validation": {"strategy": "standard-v1"}
}
```

`4096` 仅为数字格式示例，应替换为实际值。任务判断 `task-judge` 的 `message_prefixes` 则是 `{}`；其 Replay 文件也必须声明允许这个 Agent。先核对选定适配器和业务运行时，再决定允许哪些请求参数。上例的流式配置不是 TE 对所有 Agent 的统一要求。

10. 点击「预览业务执行基线」，核对变量、组件数量和依赖，再点击「登记业务执行基线」。
11. 确认「本次业务 Agent 执行配置」已选中该配置。

登记得到初始发布组合，用于还原现状；它不代表进化改善、自动评估或 Agent 已采用。用户提示词、当前用户问题和聊天历史属于本次请求输入，不要把一条用户问题当固定业务规则登记。

## 6. 界面第三步：登记可进化的上下文变量

如果规则、示例来自变量表，继续操作；只有固定 Prompt 时可跳过。

1. 在当前执行配置下点击「以此配置登记变量上下文」。
2. 填新执行配置 ID，例如 `support-prompt-managed`。这是派生新基线，原配置与历史候选保留。
3. 默认选择「按变量分别填写取值」。只勾选规则、示例、参考资料等可修改内容。
4. 为 `policyText` 添加实际原文取值，例如「售后退货规则」与「售后换货规则」，每个值单独一格。
5. 不把 `chatHistory`、当前用户问题、用户 ID 等每次请求输入登记成可进化规则。
6. 选择「仅上下文资产」或「Prompt 与上下文资产」。
7. 点击「预览变量上下文」，核对每组取值和全部继承组件，再点击「确认登记新基线」。
8. 后续使用新配置，并向 Agent 提供它对应的模板引用和实际加载内容。

JSON 导入也支持按变量独立提供值：

```json
{"policyText": ["实际退货规则原文", "实际换货规则原文"]}
```

四个变量各三个值是四个变量组、十二个取值成员，不登记成 81 个组合。原文逐字去重，空值不建空资产；未登记的新值仍作为普通输入，不会自动成为可编辑组件。取值通过原值哈希选择成员，编辑后保持选择器稳定。

当前每个执行配置最多 32 个组件，包含原 Prompt 和继承组件；每组最多 200 个提交值，总输入最多 1 MiB。预览里的本次提交数与包含继承成员的总数不同，应分别核对。

## 7. 业务 Agent 怎样上报才能被匹配

### 7.1 复制当前配置的接入信息

选中最终要使用的配置，展开「接入真实 Agent 的会话」，点击「复制接入元数据」。这里返回的是结构和登记默认值。Agent 必须替换成本次**实际使用**的信息，不能把默认值机械复制到所有请求。

将元数据写入每个实际交互 Trace 的 `metadata.teamevolver_execution`，结构如下。下面的 ID/hash 仅为示意，实际值使用界面或实际拉取组合中的内容：

```json
{
  "teamevolver_execution": {
    "schema_version": "teamevolver.execution-observation.v1",
    "execution_profile_id": "support-prompt-managed",
    "agent_id": "support-agent",
    "runtime_type": "my-business-agent",
    "release_id": "实际加载的release_id",
    "composition_hash": "实际加载组合的64位SHA256",
    "template_inputs": {
      "schema_version": "teamevolver.execution-inputs.v1",
      "template_refs": {
        "界面返回的组件ID": {
          "template_id": "界面返回的模板ID",
          "revision": "1",
          "content_hash": "实际模板原文的64位SHA256"
        }
      },
      "template_variables": {"policyText": "本次真正传入的原始规则值"},
      "input_messages": []
    }
  }
}
```

元数据完整列出声明的变量，包括只读请求变量；有多个业务 Prompt 模板时，`template_refs` 也完整列出。派生配置会生成新组件 ID，不能继续拿源配置的 ID 冒充新配置。

### 7.2 同时记录实际模型请求

Trace 的 `input` 记录当前问题，`output` 记录真实回复。对应 GENERATION 的输入必须保存完整、有序的实际模型消息，例如：

```json
{
  "trace_input": {"messages": [{"role": "user", "content": "我想申请退货"}]},
  "generation_input": {"messages": [
    {"role": "system", "content": "你是售后助手，遵循以下规则：本次实际渲染后的规则正文"},
    {"role": "user", "content": "我想申请退货"}
  ]}
}
```

`trace_input` 和 `generation_input` 是这里用来说明两处记录位置的名字，不是要求在 Langfuse 新增这两个字段。Agent 应写入其真实 Trace input 与 GENERATION input。

有历史时：如果历史是独立公开消息，把当时实际消息放入 `input_messages`；如果历史已装进完整用户提示词，就保留那条真实用户输入，不额外拼一份历史。当前问题只出现一次，首尾空白按原样保留。独立上下文消息的角色、顺序、正文同样要与实际请求一致。

业务 Agent 仍负责真实调用模型。用户后续纠正应按真实交互继续记录，或由已适配的数据源保留反馈；不要把事后的评分偷偷补进原模型请求。评分若只存在自定义 metadata 中，应确认数据源确实将其转换成分析可读材料；它不会因为叫 `score` 就自动成为发布指标。

一个对话框保持稳定 `sessionId`，每次新交互使用新 Trace ID。重复拉取不新增独立证据；旧上下文不会反复计成新会话。同一 Session 使用多个 Prompt 时只为各配置选取匹配的目标 Trace，其他内容保留为 Context。

直接 `POST /ingest_session` 自报的使用信息遵循 `agent_reported` 边界，不能通过伪造 `identity_verified` 或贴上同名 Prompt 绕过原始请求验证。本文真实 Prompt 接入使用已配置的数据源读取上游原始请求。详见[接入协议](docs/zh/agent-integrations/06-protocol-v2.md)。

## 8. 界面第四步：拉取并确认会话归档

1. 先在业务 Agent 中进行真实任务，产生有结果、纠正或反馈的会话。
2. 打开「数据源接入」，填覆盖这些 Trace 的开始、结束时间，带明确时区。
3. 先预览，确认项目、Trace 名称、用户和会话确实是目标业务数据。
4. 执行正式拉取；根据任务 ID 查看处理状态。
5. 等待强制分析及归档完成，再在会话列表查看正式 Session。下拉预览的短 ID 不能代替完整归档 Session ID。

`fetched` 是取到的上游数量，`enqueued` 是入队数量，`duplicate` 是相同来源快照已受理；它们都不等于已经归档。进化优先使用分析为 `valuable` 的有效证据，连接正常也不代表所有会话都应进入进化。

回到「进化链路 → 对应配置 → 接入真实 Agent 的会话」，粘贴完整归档 Session ID，点击「检查会话匹配」。成功时能看到核对的发布组合、匹配请求数与准入状态。失败则根据原因核对身份、模板引用、变量和实际请求。匹配成功仍要满足未消费、独立来源、用户数量和进化信号等原有成组条件。

## 9. 界面第五步：配置回放评价

打开「进化链路 → A/B 回放评价方式」：

- 选择「现有方式：Checklist 完成质量＋客观效率」，沿用原检查与效率判定。
- 或选择「外部评价 Agent：业务评分」，再选「标准 HTTP 接口」或「Python 适配器文件」。

外部模式依次填写已登记的文件版本、服务 URL、认证 Secret Ref、超时、评价要求、候选最低平均分、最低平均分提升，最后点击「保存回放评价方式」。适配器模式的 URL 可由文件决定是否需要。Secret Ref 应由运维提供真实已保存引用，不能编造 `sref_...` 或粘贴明文 Key。

**尚未提供独立通用凭证创建按钮时，由运维使用现有 SecretRepository 保存密文后提供引用；不能用浏览器保存 Root Key 来解锁 Python 源码操作。** 无需鉴权的评分服务可以将 Secret Ref 留空。

模式随本次任务冻结，历史结果不改写。外部模式按配对业务评分比较；Checklist、Token 和轮次继续记录，但不作为该模式的最终采纳分数。仍保留实际加载、执行与审阅检查。平均分持平不代表有提升，缺一边评分时显示无法确定。

## 10. 界面第六步：开始进化、看进度和结果

1. 在「进化链路」勾选「业务 Agent Prompt／上下文注入」。只改 Prompt 时取消 Skill；联合进化才同时选择 Skill。
2. 确认「本次业务 Agent 执行配置」是最终的受管理配置。
3. 手动测试点击「立即进化」；需要后续自动执行时，再在「默认修改范围（含自动计划）」保存模块和配置。
4. 用返回任务的状态确认完成，再看「运行总览 → 候选」以及进化审计。
5. 零候选时查看分组原因，例如独立会话数或用户数不足、没有匹配的原始请求、材料已消费，或生成器判断无需修改。
6. 候选出现后打开评估详情；未自动评估时执行「重新评估（重跑回放）」。
7. 核对实际加载验证、参与回放的当次及历史 Case、每题原版本/候选回复、外部评分及原因，或原 Checklist/效率结果。

候选正文差异、组件版本与冻结基线都可查看。材料条数不等于 Replay 题数；当次题数与历史采样沿用当前配置和原规则，不能拿几个满分 Case 代替全量材料表现。生成了候选、静态检查通过、Replay 结论改善和已发布是不同状态。

## 11. 审阅、发布及让 Agent 真正采用

1. 人工阅读候选正文和差异，确认业务条件没有被弱化或改错。
2. 核对实际 Replay 结果。没通过的候选保留供人工查看，不为了通过而替换分数或放宽门槛。
3. 按正常发布操作提交；不把「强制发布」作为接入验收步骤。
4. 在「运行总览 → Prompt 版本」查看新发布组合及依据。
5. Agent 使用租户 Key，按配置的执行用户拉取：

```bash
curl --fail-with-body --get "$TE_URL/sync/skills" \
  -H "Authorization: Bearer $TE_TENANT_KEY" \
  --data-urlencode 'user_id=实际执行用户ID' \
  --data-urlencode 'execution_profile_id=最终执行配置ID'
```

响应的 `execution.document.execution_plan` 是完整业务组件组合。纯 Prompt 的 `execution.skills` 为空是正常情况。Agent 校验组合和依赖后原子切换，新请求使用新组合，执行中的会话固定原组合；不能只抓 Prompt 新正文而继续混用未验证的上下文。

Agent 可上报下载/激活回执，接口为 `POST /sync/skills/execution-receipt?user_id=...`，正文包含 `profile_id`、`release_id`、`composition_hash`、`request_id`、`state`。回执仅是自报，最终仍看后续实际模型请求是否使用新版本。

需要回滚时，回到「进化链路」，选中执行配置，点击「查看发布与 Agent 回执」，在发布记录中选「回滚到此组合」，核对后点「确认整套组合回滚」，再让 Agent 拉取采用。只发布到 TE、但 Agent 未拉取或未加载，不算完成业务侧闭环。

## 12. 常见问题按哪一层排查

| 现象 | 先检查 |
| --- | --- |
| 下拉框没有 `.py` | 是否已通过源码 API 保存到当前租户 PG；放本地目录不等于登记 |
| 管理员看不到源码编辑按钮 | 正常控制台身份不等于服务 Root Key；由运维登记文件 |
| 更新 TE 后仍使用旧适配器 | PG 中的源码不会随仓库示例更新；重新登记文件，在界面选择新版本并保存绑定 |
| 选完文件还不能运行 | 是否保存绑定；适配器是否启用；Worker 环境和特殊参数是否齐全 |
| Langfuse 有 Trace，TE 拉不到 | 项目 Key、真实 Trace 名称、带时区窗口、Worker 网络与分页错误 |
| 拉取显示入队，列表没有会话 | 看 ingestion/Worker 阶段，等待强制分析与归档，不把 ACK 当完成 |
| 名称一样仍不匹配 | 版本/hash/实际变量、消息角色和顺序、GENERATION 原始请求是否一致 |
| 登记上下文后旧 Trace 不匹配 | 派生配置的组件 ID 已变化，检查当时真实使用的是哪个配置 |
| Prompt 候选一直为零 | 是否只选了 Skill；配置是否为空；是否有价值、未消费且满足独立来源与用户门槛的材料 |
| 原版本 Replay 比历史 Excel 分数高 | 当前模型与运行环境重新执行，历史分数与本轮配对分数不能直接互换 |
| 评分器报错/无法确定 | 两边业务执行是否成功，评分协议、Secret Ref、标准答案查找、超时及返回 JSON |
| 元数据说加载了，仍验证失败 | 实际最终模型请求没有完整包含该组合；声明不能替代真实传输证据 |
| 发版了但 Agent 还用旧内容 | Agent 拉取参数、缓存、加载指针和请求中实际 release 是否更新 |

## 13. 本轮复核记录与验收清单

2026-11-09 对本地现有部署复核：API 健康正常，`default` 已绑定数据源和 Replay 文件，但业务执行配置数量为 0，默认进化范围为 `skill`，外部评测器文件列表为空，当前评价方式为 `checklist_efficiency`。这是当前租户的未完成接入状态，不等于已有 Prompt 已接好。

现有数据源已通过 Worker 实际连接测试，远程 Agent Lab 健康正常。当前 `default` 绑定的 `agent-lab.py` 是 PG 中的旧版 v1，尚无独立变量上下文的传输投影适配。要在此租户使用这项能力，需要按 3.4 重新登记仓库内当前示例，再在界面明确绑定新版本；本轮没有替用户升级绑定。

使用仓库内当前 `agent_lab.py` 源码，通过原生适配器测试任务创建了两个独立远程诊断会话：相同模板和原始变量取值，分别加载原上下文和修改后的上下文。最终请求中的 System 内容与各自上下文完全一致，模型分别回复 `TE_CONTEXT_BASE` 和 `TE_CONTEXT_NEXT`，请求观测及加载回执均通过。这是实际模型调用与上下文加载验证，不是一次业务候选进化，也不是业务质量改善证明。此前不符合运行时参数的失败探测，以及因诊断 Token 预算过小而截断的结果，都保留在核查记录中。

本轮还在隔离 PostgreSQL 中回归注册、上下文、真实请求归属、增量、候选、HTTP Replay、外部评分、发布及拉取，共 211 项通过；完整前端 364 项通过，并补充了同名适配器版本切换的回归检查。测试模型/评分 HTTP 服务用于验证执行路径，不宣称证明真实业务效果。未清理历史实验，未替用户切换默认租户的模块、配置、评分或发布状态。

浏览器自动操作被当前 Work 设置禁用，因此本轮界面依据前端组件回归与实际接口核对，未声称完成当前浏览器逐按钮操作。正式接新业务 Agent 时还应完成下面的现场验收：

- [ ] 三类职责分别接通，所有配置属于同一目标租户。
- [ ] 最终 Prompt/上下文配置已选定，Agent 使用它对应的真实引用与内容。
- [ ] 一条真实业务会话已入 Langfuse、归档，匹配检查成功。
- [ ] 重拉同一来源不会增加独立证据；后续交互保留同一来源根。
- [ ] 候选完整保存，未选组件保持原样。
- [ ] Baseline/Candidate 都实际执行，最终请求证明候选正文及本题变量已生效。
- [ ] 采用所选评分方式，未知与失败没有被伪造为满分。
- [ ] 正常审阅发布后，Agent 拉取并在后续真实请求中采用，能继续收到真实反馈。

补充参考：[业务组合进化](docs/zh/concepts/12-execution-evolution.md)、[数据源接入](docs/zh/guides/10-datasource-onboarding.md)、[Replay 适配器与外部评分](docs/zh/api/05-replay-branch.md)、[Agent 拉取](docs/zh/api/06-skill-sync.md)。
