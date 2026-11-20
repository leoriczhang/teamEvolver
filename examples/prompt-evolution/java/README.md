# 业务 Java 调用层的 Prompt Evolution Reporter

这个组件接入业务 Agent 的最终模型调用层。它使用明确的业务身份读取 TE 已发布组合，核对实际请求，再给真实 Langfuse Trace 附加进化归属。源码位于 `src/main/java/io/teamevolver/reporter/EvolutionReporter.java`，运行需要 Java 17 和 Jackson。业务系统可复制这个类并沿用自己的依赖管理，也可以构建本目录的独立 Maven 模块。

本目录是可编译的接入组件和本地测试。业务系统仍需把它连接到实际加载 Prompt 的代码、最终模型传输、Langfuse GENERATION 观测及原有重试机制。它没有向外部 Java 服务安装代码。

运行本地契约测试。

```bash
mvn test
```

配置示例在 `routes.example.properties`。其中两个 `TE_LOCAL_*` preset code 是本地场景测试身份，`agent-one` 和 `agent-two` 是两个隔离执行槽。业务接入时应逐项改为真实项目、精确 preset code/version、Agent、执行主体、执行配置 ID 和服务地址。`tenantTokenEnv`、`langfusePublicKeyEnv`、`langfuseSecretKeyEnv` 填进程环境变量名称，凭证由调用方传入的 resolver 在运行时读取。

附件中的真实业务线索可供开发者定位调用层。任务判断场景的显示名为 `V2-对练-任务完成判断-CT_HW`，附件记录其 profile 为 `task-judge-v2-tpl`。话术提示场景的 preset code 为 `V2_PRACTICE_PROMPT_CT_HW`、version 为 1，Trace 名为 `invoke_agent V2-对练-话术提示-CT_HW`，调用点为 `AiPracticeAigcServiceImpl.aigcMessagePrompt` 的 `PRACTICE_CT_HW` 分支。任务判断的精确 code、实际执行主体及当前部署绑定仍须从业务代码与运行配置核对。附件记录和本地演示分别保留自己的配置。

按下面顺序连接现有调用层。

1. 从业务请求和精确 preset 调用上下文构造 `Identity`。六个字段是租户、项目、preset code、preset version、Agent ID、TE 执行主体。`route(identity)` 要求配置中唯一且完整相等的映射。多个 Agent 使用同一模板时，分别配置各自映射。
2. 用 `pullPublished(identity)` 读取 `/sync/skills`。TE 租户凭证提供租户权限，执行主体和 profile 通过 query 指定。组件强制核对服务端返回的 `tenant_id`、profile、Agent、执行主体、runtime 和每个组件的内容 hash。服务端必须提供本分支新增的顶层 `tenant_id`，缺失时调用会失败并提示升级契约。返回的 `Release` 是不可变快照，业务加载器实际装载这个快照后，将它随本次调用上下文传下去。发布更新时，新调用可加载新快照，已经开始的调用继续使用原快照。
3. 模型调用前取得真实变量原值、公开历史消息、完整当前用户消息和最终模型 JSON。传给 `observe()` 的 `Release` 必须是本次实际加载的对象。变量名称须与注册契约完全相同，值从原始调用参数取。JSON 数组等复合变量使用业务原来实际传入的字符串值。用户模板在业务原有的 `user` 消息位置渲染。
4. `observe()` 核对实际最终请求的消息内容、角色、顺序、模板变量和模型参数，生成不可变 `Observation`。业务 GENERATION 应记录同一份完整最终请求和真实输出。Trace input 记录当前用户输入的消息列表，历史放进 `template_inputs.input_messages`。给真实传输传递与观测完全相同的请求。
5. 在传输已发生且 Trace ID 已确定后调用 `upsert(observation)`。它使用这个 Trace 的既有 ID、Session ID、业务用户 ID，向 Langfuse 顶层 metadata 写入 `teamevolver_execution`。异步和流式调用显式携带 `Invocation` 与 `Release`，无需 ThreadLocal。

调用形态如下。示例中的 `businessRuntime`、`generationRecorder`、`modelTransport`、`existingOutbox` 是业务现有能力，需要接到真实对象。

```java
Properties properties = new Properties();
try (var reader = Files.newBufferedReader(configPath, StandardCharsets.UTF_8)) {
    properties.load(reader);
}
EvolutionReporter reporter = EvolutionReporter.fromProperties(properties, System::getenv);

// 执行主体是部署配置，customerUserId 是本次服务的真实业务用户。
var identity = new EvolutionReporter.Identity(
    tenantId, projectId, actualPresetCode, actualPresetVersion, actualAgentId, executionUserId);
var published = reporter.pullPublished(identity);
businessRuntime.load(published); // 业务加载器须实际应用这个组合及其版本化上下文。
var loadedForThisInvocation = businessRuntime.loadedRelease();
var invocation = new EvolutionReporter.Invocation(
    identity, actualTraceId, actualSessionId, customerUserId);

// 放在真实最终模型传输 hook。数组、换行、空白均保留业务原值。
var observation = reporter.observe(invocation, loadedForThisInvocation,
    actualTemplateVariables, actualPublicHistory, renderedCurrentUserMessage, finalModelRequest);
generationRecorder.captureInput(finalModelRequest);
var reply = modelTransport.send(finalModelRequest);
generationRecorder.captureOutput(reply);
try {
    reporter.upsert(observation);
} catch (IOException transportFailure) {
    existingOutbox.persist(observation.deduplicationKey(), observation.body());
}
```

同一个 `Observation` 可重复调用 `upsert()`，Trace ID 和序列化请求保持相同，`deduplicationKey()` 可接业务现有的持久化 outbox。组件不生成新 Trace 代替重试，也不创建本地权威业务账本。进程重启后的重试应读取原 outbox 中已经冻结的 body 并原样发送。Langfuse 同一 Trace 的 upsert 没有跨客户端 CAS，业务必须保证一个原始模型交互只有一个最终归属，不能让两个不同调用复用同一 Trace ID。

Langfuse 写入使用 `POST /api/public/traces`，请求显式指定 HTTP/1.1，以兼容本次真实服务的 HTTP 前端，避免 Java HttpClient 默认 h2c 升级导致连接关闭。组件还核对成功响应里的 Trace ID。本次在独立、明确标注复制来源的传输夹具中，先读取无进化绑定的 Trace，再用 Java 连续写入两次并读取验收。实际新增绑定与预期相等，原有顶层 metadata、Session、业务用户、名称、时间、input/output 和原有 GENERATION 保持原值。该传输验证没有发起模型执行。

组件支持固定 `agent_prompt`、独立 `context_fragment` 及 `inline_variable` 上下文。后者按宿主组件、变量名、原始值 hash 精确选择，替换发生在原占位符中，metadata 保留原始变量取值，插入的文本保持字面量。变量保持 TE 的字符串、布尔值及有限数值范围。浮点数使用 Jackson 现有 Schubfach 十进制表示，再按 TE/Python JSON 的固定小数和指数阈值格式化，回归包含 `1e-6`、`0.0001`、`1e16`、负零和 `1e23`。每次仍用最终实际请求逐字核对，若业务运行时在极端浮点短表示上不同，Reporter 会给出渲染差异并停止本条上报，保留原始 Trace 供进一步核对。业务原来传入的字符串继续保持原值。当前模块用于纯 Prompt／上下文组合，遇到 Skill 依赖会要求业务提供对应执行加载器。

`/sync/skills` 返回的组合来自 TE 已发布记录，组合 hash 随该响应保留。组件校验组件正文 hash 与返回的所有执行绑定，实际请求一致性校验发生在最终传输 hook。业务应按租户、执行主体和 profile 划分凭证与加载缓存，缓存更新以完整 `Release` 为单位。Reporter 上报的是实际加载的 release，读取 preset 或读取最新 integration 模板只为配置准备提供材料。

错误消息保留 HTTP 状态和契约原因，服务端原始正文不会进入 Reporter 异常。模型回复、评分器答案及人工标注由原业务系统按原流程处理，Reporter 只接收本次执行实际使用的输入。

`RecordedRequestCheck` 可用于对接时核验既有真实请求。它从 JSON 文件读入每条原始请求及对应身份，向 TE 拉取真实已发布组合，用 Reporter 生成 metadata，与录制在 Langfuse 的 `teamevolver_execution` 逐字段比较，将结果保存到报告文件。此命令会访问 TE，模型和 Langfuse 上传不会被调用。每条输入包含 `identity`、`trace_id`、`session_id`、`customer_user_id`、`actual_variables`、`public_history`、`current_user_message`、`final_model_request`、`expected_observation` 和说明来源的 `fixture_scope`。录制请求的 release 需要仍是当前发布组合，历史 release 检查应由持有当次不可变快照的业务加载器执行。

```bash
mvn dependency:build-classpath -Dmdep.outputFile=target/classpath.txt
java -cp "target/classes:$(cat target/classpath.txt)" io.teamevolver.reporter.RecordedRequestCheck routes.properties recorded-samples.json recorded-report.json
```
