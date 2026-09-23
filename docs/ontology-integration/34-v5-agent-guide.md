# Agent 基于本体使用 Viking：HTTP 与 MCP

Agent 直接连接 OpenViking。teamEvolver 是运营入口，读取已发布本体不经过 TE。先用 OV 原生认证取得受限用户或应用凭证，再给主体开通本体 `read` 权限；`feedback` 单独授予。trusted 身份头仅供可信后端从认证上下文生成，不能让外部调用者自由指定 account/user。不要把 Root Key 发给 Agent。

## 两条使用路径

本体增强检索：调用 `POST /api/v1/enterprise/ontology/search`，传 `query/target_uri/limit/token_budget`。服务只用当前可见且已发布的实体与别名扩展问题，调用原生 OV 文档检索，再组合带证据的事实。无匹配实体时返回 `mode=document_only`，没有把普通文档包装成验证事实。原生 `find/search` 默认行为不变。目前扩展使用实体与已审核别名，尚无完整概念层级推理或跨域别名消歧。

结构化诊断：`ontology/resolve` 找实体，`ontology/query` 作有界类型查询，`context/compose` 或 `ontology/evaluate` 返回事实、受限规则判断、冲突、缺口和证明。`ontology/explain` 与 query 使用同一事实服务。TRUE/FALSE/UNKNOWN 是规则真值，冲突状态单独表达；未查到事实不是 FALSE。客户端不要把客户陈述转成系统事实。

## HTTP 示例

用运行环境提供 `OV_ONTOLOGY_AGENT_KEY`，不要把 key 写进代码或日志。

```python
import os
import httpx

with httpx.Client(base_url=os.environ['OV_URL'],
                  headers={'Authorization': 'Bearer ' + os.environ['OV_ONTOLOGY_AGENT_KEY']},
                  timeout=15) as ov:
    result = ov.post('/api/v1/enterprise/ontology/search', json={
        'query': '运单 001 的签收争议有什么证据？', 'limit': 10, 'token_budget': 4000,
    })
    result.raise_for_status()
    packet = ov.post('/api/v1/enterprise/context/compose', json={
        'entity_ids': ['Shipment:001'], 'claim': '客户陈述未收到，待核实',
        'token_budget': 4000,
    })
    packet.raise_for_status()
    # 原样保留限定、证据和 UNKNOWN；不要仅摘出一个 status 值。
    print(packet.json()['prompt_fragment'])
```

仓库脚本 `scripts/ontology_agent_example.py --lab` 使用隔离只读主体。业务写入口未提供。

## MCP 配置与调用

连接 OV 已有 Streamable HTTP `/mcp`，通过客户端安全凭证功能注入同一原生 Bearer token。下列 JSON 表达配置结构，`${...}` 必须由客户端或部署系统安全替换，不能假设所有客户端自动支持环境插值。

```json
{"mcpServers":{"ov":{"url":"https://OV_HOST/mcp","headers":{"Authorization":"Bearer ${OV_ONTOLOGY_AGENT_KEY}"}}}}
```

新增工具：`ontology_capabilities`、`ontology_read(operation, arguments)`、`ontology_feedback(result_ref, note)`。

```json
{"operation":"search","arguments":{"query":"签收单 001","limit":10,"token_budget":4000}}
```

```json
{"operation":"compose","arguments":{"entity_ids":["Shipment:001"],"token_budget":4000}}
```

`ontology_read` 仅接受 capabilities/resolve/search/query/compose/evaluate/explain；没有 approve/commit/rollback 或业务动作工具。HTTP/MCP 共享当前主体、ACL、服务和响应结构，MCP 协议外壳可为 JSON 文本块，事实集合保持一致。

## 返回和预算

每个响应固定 `semantic_generation` 和 `schema_revision`。事实带 valid/system time、qualifiers、source_id/revision/digest、原文 quote/start/end。`snapshot_ref` 指向 OV 不可变证据端点，访问仍需当前认证；历史链接不能绕过撤回。ContextPacket 的 `prompt_fragment` 来自同一事实集合，整体移除超预算事实及证明；文档结果也按预算整体裁剪并标记 `documents_truncated`。token_budget 当前采用保守 UTF-8 字节估算，不是精确模型 tokenizer 计数。

查询有扫描、遍历、结果、时间上限。检索期间 generation 或 epoch 改变返回 409，客户端重试完整查询，不拼接两次不同版本的事实。`postgres_ready` 与原生文档索引状态分开说明。来源不可用/过期/无权访问不会变成肯定事实。反馈只进入运营评审；本次 UI 支持查看反馈，尚无工单分派与闭环 SLA。
