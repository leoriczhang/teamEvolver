# 顺丰 Ontology：研究与实施设计包

**日期：2026-09-18。定位：完整设计与离线契约参考，不是已部署系统。**

优先阅读 `docs/25_顺丰Ontology完整实施方案_TE入口_OV编译存储服务_V3_20260918.md`。

## 文档导航

| 文档 | 内容 |
| --- | --- |
| 23 | 代表性论文、HugAgentOS等工程、选型结论与一手来源目录 |
| 24 | 飞虎20页材料精读、14业务域映射、共享元模型、12类CQ与试点 |
| 25 | TE入口、OV Compile、结构化存储、发布、增量、权限、Agent接口完整V3 |
| 26 | 原生与新增接口区别、请求响应、错误契约、36项系统验收矩阵 |

四份文档已创建到个人知识库对应顺丰企业知识库目录，均已回读并核对SHA256；截至回执检查时，语义与向量索引仍为queued。详见 `results/personal_kb_receipts.json`。回执仅保留相对目录，不包含个人账号标识。

## 契约与示例

`contracts/` 提供4个Draft 2020-12 JSON Schema，以及仅描述Query／Compose两个接口的OpenAPI 3.1草案。它们不是整个系统的最终API实现。`examples/` 为合成数据及原生Compile字段示例，URI、摘要、规则、scope均不可直接用于生产。

`reference/validator.py` 演示有限的来源、时间、限定、引用、三值逻辑、预算与可见性不变量。可信源目录和认证观察在测试中由独立fixture模拟，**不能在生产中从不可信候选自身生成“可信目录”**。没有实现IAM、签名校验、数据库事务、完整证明DAG或模型抽取。

## 重跑离线检查

使用Python 3.11或更高版本，在隔离环境安装 `requirements.txt` 中记录的验证依赖后执行：

```bash
python reference/run_tests.py
```

结果写入 `results/contract_test_results.json`，失败时进程返回非零退出码。当前已执行40项，40项通过。它们不覆盖TE／OV联调、权限审计或性能；26号中的36项系统测试仍为not_run。

需要重新生成契约时运行 `python reference/build_contracts.py`，会覆盖本包contracts与examples中的生成文件；不会访问网络、个人知识库或真实业务服务。

## 实施约束

本地DevSpace三个连接均失败，本轮没有读取或修改部署源码，没有部署、注册真实工具或调用业务动作。P0必须核对实际TE／OV版本、Compile持久化／凭证／制品能力和原始来源权限。文档中所有enterprise接口、sf-ontology-build Profile及业务工具名均是提案。

论文与第三方材料以原始链接和研究提炼保存，未复制论文、第三方许可或飞虎PDF全文。正式复制任何开源代码前需完成固定版本许可与依赖审查。
