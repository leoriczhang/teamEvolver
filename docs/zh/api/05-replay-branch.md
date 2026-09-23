# ReplayAdapterFactory 接口与控制面

True Replay 通过租户绑定的 Python 工厂执行客户 Agent。
系统负责 A/B、Checklist 裁判和拟人化用户反馈；适配器仅负责真实 Agent 执行。
运行接口只有 `open(context)`、`send(user_message)` 和 `close()`。
实现：[hooks](../../../team_replay/hooks.py)、[加载器](../../../team_replay/adapter_runtime.py)、
[engine](../../../team_replay/engine.py)。

## 工厂与分支

Python 文件放在部署配置 `replay.adapters_dir`，默认目录含预装示例。
`replay.adapter` 是 default 租户绑定；其他租户显式配置扁平字段 `replay_adapter`，不继承 default 绑定。
管理员在健康页选择预装适配器。客户文件导出 `REPLAY_ADAPTER` 和 `build_replay_adapter(config)`。

```python
from team_replay.hooks import AgentObservation

REPLAY_ADAPTER = {"label": "Customer Agent", "enabled": True}

def build_replay_adapter(config):
    return CustomerFactory(config)

class CustomerFactory:
    def __init__(self, config):
        self.config = config

    def open(self, context):
        # Create an isolated customer conversation/workspace, apply treatment,
        # and return a Session implementing send(message) and close().
        raise NotImplementedError("Connect your customer runtime here")
```


`ReplayContext` 只含 request_id、runtime_type、treatment、materials、context_snapshot 和 timeout_seconds。
首轮 query 仅通过 `send()` 传入；后续用户反馈同样调用 send，适配器保持对话连续性。
每个分支必须独立会话/工作区，模型、工具、初始材料、上下文及限制保持一致；唯一差别为 treatment。
不能保证隔离时返回 unsupported。

一个 treatment 可以是单个 Skill，也可以是没有主从关系的 Skill Set。Test Dataset
通过顶层 `skills[]` 声明关联集合，每个 Replay Case 通过 `skill_ids[]` 选择本任务
实际安装的子集。Baseline 与 Candidate 必须解析并记录相同的 Skill 集合；实验允许
替换其中一个或多个发生变更的 Skill。

`AgentObservation` 含必填 response，以及可选 messages、artifacts 和 metrics。
Token、API 调用等值必须来自运行时真实观测，缺失记为 unavailable，不补零。
任何轮次都不向适配器传 Checklist、裁判结论或未披露要求。
系统用独立裁判核验具体证据，再由独立用户模拟器生成自然反馈；失败时不比较效率。
Candidate 未完成而 Baseline 完成时 reject；Candidate 独自完成时 accept；
都完成才比较客观效率；都未完成或裁判不可用时 inconclusive。

## 控制面

| Method | Endpoint | Permission |
| --- | --- | --- |
| GET / PUT | `/api/replay-adapter` | Console admin: describe / bind `{"file":"customer.py"}` |
| GET | `/api/replay-adapter/code?file=customer.py` | Root: read source/revision |
| PUT | `/api/replay-adapter/code` | Root: `{file, code, expected_revision}` |
| POST | `/api/replay-adapter/code/test` | Root: `{file, code, context, query}` |


Python 在服务进程内执行，源码读写/测试仅 Root 可用，AST 校验不是沙箱。
测试必须显式提供 query 与 context（runtime_type、可选 skill/materials/context_snapshot/timeout_seconds），
不接触发布数据。源码更新使用 revision 乐观锁，冲突返回 409。
预装包装器覆盖 TurnBased、MappedHttp 和 DEAP；旧 branch-only 协议仅作显式 legacy 包装。
Validation runtime 由 `validation.runtimes` 配置，不依赖 Agent 注册。

## DEAP/upclaw 响应契约

`POST /deapAgent/blocking/message` 的兼容响应保留顶层 `answer`，并应返回当前请求这一轮的 Replay 观测：

```json
{
  "answer": "final answer",
  "traceId": "trace-123",
  "replay": {
    "messages": [{"role": "assistant", "content": "..."}],
    "metrics": {"tool_call_count": 2, "total_tokens": 841},
    "artifacts": []
  }
}
```

成功 Replay 的 `tool_call_count` 和 `total_tokens` 必须是非负数，且只统计当前轮；teamEvolver 负责跨轮累加。`messages` 只包含本轮完整 assistant/tool 轨迹。上游旧版本未返回 `replay` 时回答仍可使用，但结果会记录 `metrics_incomplete=true`、`metrics_incomplete_reason` 和缺失指标列表，不会补 0。`traceId` 仅用于关联排障，不要求 teamEvolver 再异步查询 Langfuse 才能完成判定。

当前仓库实现的是 DEAP 响应映射与兼容处理；upclaw 服务端必须在其自身仓库实现上述返回字段。
