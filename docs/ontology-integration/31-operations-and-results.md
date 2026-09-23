> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 历史 V4 记录。2026-09-20 起部署职责调整为 TE 内置构建、OV 资产后端；后续见 [V5 实施说明](32-te-ov-v5-implementation.md)。本文测试结果不自动算作 V5 验收。

# Ontology 本地部署、恢复与结果

日期：2026-09-19。仅用于隔离验收，禁止把合成 Schema/租户直接发布到生产。

## 可重现环境

三个仓库保持同级目录，或设置 ONTOLOGY_OV_REPO 与 ONTOLOGY_ENHANCER_REPO。本次使用 /tmp/te-ontology-venv，Python 3.12；隔离 PostgreSQL 17 位于 /tmp/te-ontology-lab/pg，监听 127.0.0.1:55439，不启动系统全局服务。

安装 TE、enhancer 及测试依赖后，执行：

```bash
python scripts/ontology_lab.py start
python scripts/ontology_smoke.py
ONTOLOGY_TEST_DSN=postgresql://localhost:55439/ontology_lab python -m pytest tests/test_ontology_integration.py -q
python scripts/ontology_load.py --documents 10000
python scripts/ontology_scale.py
python scripts/ontology_lab.py stop
```

端口：TE 52110、OV 52111、Runtime 52112。lab 使用和原生服务相同的新增路由，但不启动 OV 的所有既有子系统；原生 app 已启动并通过三项读取，现有 Rust invalidate_cache 缺失使 metadata worker 受阻，尚未完整验收。脚本会拒绝占用端口，PID 写到 /tmp/te_ontology_{name}.pid；stop 校验精确 PID 的命令和端口后停止 HTTP 服务。PostgreSQL保留供核验，可用 pg_ctl -D /tmp/te-ontology-lab/pg stop 停止。

credentials.json、identities.json、connections.json 由脚本随机生成，权限 0600，不入库、不写进知识库、不在日志打印。测试数据与日志仅在隔离目录。

## 开关与配置

OV_ONTOLOGY_ENABLED=1；OV_ONTOLOGY_PG_DSN；OV_ONTOLOGY_SIGNING_SECRET（至少32字符）；OV_ONTOLOGY_RUNTIME_URL；OV_ONTOLOGY_RUNTIME_GATEWAY_SECRET；OV_ONTOLOGY_BROKER_URL。

TE_ONTOLOGY_ENABLED=1；TE_ONTOLOGY_CONNECTIONS 指向 tenant/subject 独立凭证配置；TE_ONTOLOGY_STATE 指向专属本地状态目录。默认不开启。

Runtime 使用 ONTOLOGY_RUNTIME_STATE 保存有界持久化队列。真实抽取配置 ONTOLOGY_MODEL、ONTOLOGY_MODEL_BASE_URL、ONTOLOGY_MODEL_API_KEY；未配置就失败，不自动退回 fixture。只有 ONTOLOGY_DEMO_MODE=1 才允许 fixture。

## 恢复操作

- 受理超时：复用 submission_key 查询，不另起无关联任务。
- 发布超时：使用 commit_key 查询唯一回执；不要更换键盲重试。
- 基线变化：rebase 新任务，重新 Prepare 和批准。
- 取消：使用企业任务取消接口；迟到上传被状态/epoch 围栏拒绝。
- 来源撤回：先提交 source-events，再查看影响列表，选择合法新版本重建；撤回 revision 不允许重新 active。
- 来源503：unavailable 使读取 degraded/UNKNOWN；恢复事件可恢复来源可用性，不抹去历史。
- 回滚：assets/rollback-candidates → Prepare → 新批准 → Commit；撤回的来源不能被历史版本复活。
- 备份恢复：先停读取和 worker，恢复 DB、签名密钥及 Runtime 状态，并先对齐现行撤权/来源状态，才允许 enabled。跨企业授权系统恢复演练未执行，不能跳过这一步。

## 交付状态

具体数字见 results 中的真实报告。模型调用/Token 为0的 fixture 压测只能记为结构链路测试；人工审核耗时未测，不能填写0。向量 hybrid P95、生产 IAM 延迟及真实模型成本未测。

知识库新文档 create 回执与旧文档 revision proposal 回执会另行保存。连接器要求本人审核的修订建议只记为待审核，不记为已发布。

## 本次恢复演练记录

停三项隔离 HTTP 服务后，使用 PostgreSQL 17 pg_dump -Fc 备份 ontology_lab，再通过 createdb/pg_restore --exit-on-error 恢复为新库 ontology_restore。只在新库核验，不覆盖原库。结果见 results/recovery-results.json；本地授权注册表随备份恢复，生产 IAM 与 WAL 时间点恢复没有覆盖。

TE 停机结果见 results/te-outage-results.json；原生 OV 三项接口和 Rust 绑定阻塞见 results/native-smoke.json。所有 HTTP 服务已按精确 PID 重启，以装载最终代码。

万文档：10,000 文档、100 批、10,000 断言，102.340 秒；10 并发 Context P95 22.398ms。100K/1M/5M 投影查询 P95 分别 14.553/9.513/19.524ms。这些是合成、fixture/索引测试，不能写成真实模型或生产指标。

## 查询退化与修复记录

重复分档测试曾在 5M 行出现 P95=2002.126ms。EXPLAIN force_generic_plan 证实：可选过滤条件的 OR 使缓存后的通用计划按 tenant/generation 扫 facts_pkey，再过滤 subject。改为按已验证过滤条件选择固定参数化 SQL 形状后，使用 ontology_fact_subject 索引；同样 10 并发、100 请求复测为 19.524ms。保留 scale-before-query-fix.json 与 query-plan-comparison.txt，不删除退化证据。

万文档 102.340 秒 / P95 22.398ms 是该 SQL 优化前最近一次完整构建记录；优化后重新执行分档查询与 PG 回归。不能把这些本机样本当生产 SLA。

## 重启与迁移边界补证

大规模测试清理 5M 行时，首次并行回归的3个 setup 因每次启动重复运行 DDL、等待表锁而超时，日志保存在 migration-during-cleanup-failure.txt。修复为 ov_semantic.migrations 按 SQL SHA 记账，仅首次或脚本变化执行迁移；保持版本不变的重启不再重跑事实表 DDL。增加持有写锁时重启的验证，最终19项集成测试通过。真正变更 DDL 仍需要停止受理、排空任务后维护，不承诺任意在线迁移无锁。

观察句柄另绑定发布 Schema Revision，并验证谓词值类型；Schema 更换后旧句柄拒绝使用。完整外部 ToolContract 升级与业务连接器仍需专项验收。

## 环境与源码摘要（知识库同步附件）

以下是实际环境快照，不是公共 PyPI 一键安装清单。三个 editable 仓库尚未提交，须使用基线HEAD和具体文件摘要一起核验。

```text
# Recorded environment; editable source is pinned by source-manifest.json
# macOS-26.1-arm64-arm-64bit
# Python 3.12.9; PostgreSQL 17.11
APScheduler==3.11.3
Automat==25.4.16
Incremental==24.11.0
Jinja2==3.1.6
Markdown==3.10.3
MarkupSafe==3.0.3
Protego==0.6.2
PyDispatcher==2.0.7
PyJWT==2.14.0
PyYAML==6.0.3
Pygments==2.21.0
RapidFuzz==3.14.6
Scrapy==2.19.0
Twisted==26.4.0
aiohappyeyeballs==2.7.1
aiohttp==3.14.3
aiosignal==1.4.0
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.15.1
argon2-cffi-bindings==26.1.0
argon2-cffi==25.1.0
arrow==1.4.0
asyncpg==0.31.0
attrs==26.1.0
babel==2.18.0
backoff==2.2.1
backports.zstd==1.7.0
beautifulsoup4==4.15.0
brotli==1.2.0
certifi==2026.7.22
cffi==2.1.1
charset-normalizer==3.5.1
click==8.5.0
constantly==23.10.4
courlan==1.4.0
cryptography==50.0.1
cssselect==1.5.0
dateparser==1.4.3
decorator==5.3.1
defusedxml==0.7.1
distro==1.9.0
et_xmlfile==2.0.0
fastapi==0.141.1
fastuuid==0.14.0
feedparser-sgmllib==2.1.0
feedparser==6.0.14
filelock==4.0.0
firecrawl-anydoc==0.2.4
fqdn==1.5.1
frozenlist==1.8.0
fsspec==2026.7.0
google==3.0.0
googleapis-common-protos==1.75.3
grep-ast==0.9.0
grpcio==1.84.0
h11==0.16.0
hf-xet==1.6.0
htmldate==1.10.0
httpcore==1.0.9
httptools==0.8.0
httpx-sse==0.4.3
httpx==0.28.1
huggingface_hub==1.32.0
hyperlink==21.0.0
idna==3.20
importlib_metadata==8.9.0
iniconfig==2.3.0
isoduration==20.11.0
itemadapter==0.13.1
itemloaders==1.4.0
jieba==0.42.1
jiter==0.17.0
jmespath==1.1.0
json_repair==0.63.4
jsonpointer==3.1.1
jsonschema-specifications==2025.9.1
jsonschema==4.26.0
jusText==3.0.2
langfuse==4.15.4
lark-oapi==1.7.3
litellm==1.91.1
loguru==0.7.3
lxml==6.1.3
lxml_html_clean==0.4.5
markdown-it-py==4.2.0
mcp==1.30.0
mdurl==0.1.2
multidict==6.9.0
numpy==2.5.3
ontology-enhancer==0.1.0
openai==2.54.0
openpyxl==3.1.5
opentelemetry-api==1.44.0
opentelemetry-exporter-otlp-proto-common==1.44.0
opentelemetry-exporter-otlp-proto-grpc==1.44.0
opentelemetry-exporter-otlp-proto-http==1.44.0
opentelemetry-instrumentation-asyncio==0.65b0
opentelemetry-instrumentation==0.65b0
opentelemetry-proto==1.44.0
opentelemetry-sdk==1.44.0
opentelemetry-semantic-conventions==0.65b0
openviking-sdk==0.1.12
packaging==26.3
parsel==1.11.0
pathspec==1.1.1
pdfminer.six==20260107
pdfplumber==0.11.10
pillow==12.3.0
platformdirs==4.11.10
pluggy==1.6.0
propcache==0.5.4
protobuf==7.36.2
py==1.11.0
pyOpenSSL==26.4.0
pycparser==3.0
pycryptodome==3.23.0
pydantic-settings==2.15.0
pydantic==2.13.5
pydantic_core==2.46.5
pypdf==6.19.0
pypdfium2==5.13.0
pytest-asyncio==1.4.0
pytest==9.1.1
python-dateutil==2.9.0.post0
python-docx==1.2.0
python-dotenv==1.2.3
python-multipart==0.0.32
pytz==2026.3.post1
queuelib==1.10.0
rank-bm25==0.2.2
referencing==0.37.0
regex==2026.9.10
requests-file==3.0.1
requests-toolbelt==1.0.0
requests==2.34.2
retry==0.9.2
rfc3339-validator==0.1.4
rfc3987==1.3.8
rich==15.0.0
rpds-py==2026.6.3
ruff==0.16.8
service-identity==26.1.0
shellingham==1.5.4
six==1.17.0
sniffio==1.3.1
soupsieve==2.9.2
sse-starlette==3.4.11
starlette==1.6.0
tabulate==0.10.0
teamEvolver==0.1.0
tenacity==9.1.4
tiktoken==0.14.0
tld==0.13.2
tldextract==5.3.2
tokenizers==0.23.2
tomli_w==1.2.0
tqdm==4.70.1
trafilatura==2.2.0
tree-sitter-c-sharp==0.23.5
tree-sitter-cpp==0.23.4
tree-sitter-go==0.25.0
tree-sitter-java==0.23.5
tree-sitter-javascript==0.25.0
tree-sitter-language-pack==1.20.0
tree-sitter-lua==0.5.0
tree-sitter-php==0.24.1
tree-sitter-python==0.25.0
tree-sitter-rust==0.24.2
tree-sitter-typescript==0.23.2
tree-sitter==0.26.0
typer==0.27.2
typing-inspection==0.4.4
typing_extensions==4.16.0
tzdata==2026.4
tzlocal==5.4.4
uri-template==1.3.0
urllib3==2.8.0
uvicorn==0.53.0
uvloop==0.22.1
volcengine-python-sdk==5.0.50
volcengine==1.0.228
w3lib==2.4.1
watchfiles==1.2.0
webcolors==25.10.0
websockets==15.0.1
wrapt==2.4.1
xxhash==4.0.1
yarl==1.25.1
zipp==4.1.0
zope.interface==8.6
```

```json
{
  "recorded_at": "2026-09-18T18:31:10.057127+00:00",
  "note": "Uncommitted worktree source snapshot; base HEAD alone does not include implementation",
  "repositories": {
    "teamEvolver": {
      "base_head": "fe7f8246fe229fecccbb37fd713aded0c654d682",
      "sha256": {
        "Dockerfile": "3a854a8600ede418a29753e6214a09530a1367b0a50cdeaec78399873b706f35",
        "cicd_scripts/build.sh": "8723264afeb0407e69997eecad250c09d7f631c051c505e16b2698cea86246a5",
        "docker/Dockerfile.customer": "7d9d6ef1f7ed42b75ef9a237372ef6e6e77c2a61daa4865ab5f5ab26dbcf0ca6",
        "docs/scripts/check-docs-refs.mjs": "c5be8c929d264b05f3b61f834c546d9cb2c26be4d65d45fd38e1525e4454024d",
        "pyproject.toml": "dc7327a739f0656de400a9f0a82b09bf5cc495cdf6e3fe7197c97fa3af7c0703",
        "scripts/ontology_lab.py": "fcb2aaf0ed59d5c8312871700a1b60dc5253aff00f32b831a19713df16d21585",
        "scripts/ontology_load.py": "cbb130857ce24d1d82fae9bf3d0f020e203dedac776c33b2cc3f699bfc5087e7",
        "scripts/ontology_scale.py": "95d38749282c7009f28b56b9d3aef528e88eb2abd37db4ec338fd3d1f30211b7",
        "scripts/ontology_smoke.py": "b7921d71d926cf98c35d380e8a6788141f5bc71b233b6ed67279c5b780011c9a",
        "teamEvolver/proxy/routes.py": "6013389940385b1934e63344a97cb10eb8b5adbc4c85023a93efe3df8a6dae73",
        "team_ontology/README.md": "5efd676909eb2f8498cf078f8f5a5108cfd6c32afaf0cf1077b4285d0acd37b0",
        "team_ontology/__init__.py": "6976ddb6d2d91f2ee512022ca02738c1c1ab2c5cfed0603056d0db8b62b30110",
        "team_ontology/api.py": "8acef782f1b6afc8614f17d8d6f9a404646ff7d9568a2208cd11421fb7ae9cec",
        "team_ontology/standalone.py": "3031cf046f2b20b7859d0c0f310fe10ff6ef5b387f3a307cdf665b1fa56b8f92",
        "tests/test_ontology_integration.py": "a8ded112ae878bcbf8ed480b1a6028a98e5fbe26b9275ac8b9b4a91cff538efb",
        "web-ui/package-lock.json": "3a6f9c43d6d0d7e35eef0b6e756e2916d5eec63e03b17c6b459d2f40996613ab",
        "web-ui/src/App.tsx": "bb46c74805175705a4fa4509567fde3ede74f1156fab848f12be6d89db6257dd",
        "web-ui/src/views/OntologyView.tsx": "bad2543b6bef9b28e389be7f8c4185974ea36d3cc7a0f36648eafd236d1e4a4e",
        "scripts/ontology_agent_example.py": "c2210d2fd3738fa31d71a9b6f4aa2b4f01b7f4ecde2358df222d7297849b1604"
      }
    },
    "OpenViking": {
      "base_head": "029faeff0b204c1522b6d3bf95df61c984ebb2c6",
      "sha256": {
        "openviking/ontology/README.md": "6d8df25fe92aa73ce2920619383b2b65c73305f750a4dd4541893d1da539c170",
        "openviking/ontology/__init__.py": "e0c7246f4299ab830bbffc0063370a1fea9da32ae3a4f02471215fa70b901581",
        "openviking/ontology/api.py": "d926fd37360e6c99696c028d4fb61996780b0b99aa360016f1f2d14d75fb8d20",
        "openviking/ontology/contracts/candidate_bundle.schema.json": "2255a45be668973d92f8774e326bc2a70056d356eb67f9b5c4b281271c2ba86e",
        "openviking/ontology/contracts/context_packet.schema.json": "33b1f1813286d3a0ee6ae04a322fdc13db6c8a8777c101b952b9a30296372807",
        "openviking/ontology/contracts/context_request.schema.json": "746cc106596661dcd56c3c59dc01f211d1ded2c98533ab43801ec73ac73a3273",
        "openviking/ontology/contracts/error_response.schema.json": "5712891679d5e03b5c1b4b7b02774d8de95b1e172dad2aaef8b200b4e1469d7d",
        "openviking/ontology/contracts/manifest.json": "ff247802d2de4f8e3666160cf92c375b64df6c267515d09a336c0cf7efb5bc20",
        "openviking/ontology/contracts/manifest.schema.json": "2de70111c954d1429048ef8f946c05aba10722077a3f223c15cd7debe5d364fd",
        "openviking/ontology/contracts/observation.schema.json": "94fcde063207f1238aaa132545b5e4bf3041ca4662942e8c60b686804bd2111f",
        "openviking/ontology/contracts/openapi.json": "f58d1b7db07f53c9d1cc4b38210731b54f57444fedc1d6e26e8094e2b8fffded",
        "openviking/ontology/contracts/publication_grant.schema.json": "e7cc69e8aea570449146e5c1bc9870cadf0b7f7c49eea5389f18be9770a32019",
        "openviking/ontology/contracts/schema.schema.json": "726f82886b1e5ba4e4d81b0040072ac83653041ad03626622f56765c88a941ea",
        "openviking/ontology/contracts/typed_query.schema.json": "649cebdb5bf571d070e7d483c23b8551ead4ee4ad67528159b5a870f9db6dd70",
        "openviking/ontology/contracts.py": "1d18c719995b895937d88aa1af43919a1941ffd1b68110955be080fd35f1fcd8",
        "openviking/ontology/errors.py": "a3703d6cabc7fa12723dbb867910ccd64c65c029dad3f0d122c8622eb14c39f5",
        "openviking/ontology/export_contracts.py": "c41b9c6ecc4b36ec66256fdb4ca7a537f7f88c34d157002c6adf475b30bf1080",
        "openviking/ontology/limits.py": "13e258c28b846b6feb1f614df551721a010913b8b78efc91413d6b47b663a6c7",
        "openviking/ontology/rules.py": "bb1faa6fc5570ca1cc47552f6d4067099885633867469d63f6e1578b1cfb6de0",
        "openviking/ontology/schema.sql": "17430e82c37bf888bb81b5b5127919a4c775979170d672f1bb7a8a6c53335e86",
        "openviking/ontology/service.py": "6cdd020ca536012f75d019239fe0b7c4a30cb9310f272ec453bc48c699312c99",
        "openviking/ontology/standalone.py": "1de0c66aee4f0def3adcf8b49931d0b49490b98972efaaa76173e8cfabc1674f",
        "openviking/ontology/store.py": "defdf08da42f880b0d7941f4867a8ed0679392684b687f081ade48041acbfd64",
        "openviking/ontology/validation.py": "6651ab647522c6a2ea4cfa9d01aa95de68a13db11be2a4e5a344406682bda252",
        "openviking/ontology/worker.py": "4f26ad3a1839525fbd633e2f58454ccbca9b5038a1d55a6819970d5c15d5714e",
        "openviking/server/app.py": "0ce473c6857c5d32d9b2dd72d5db2a57b869c084179dd842c15ef235495d976b",
        "openviking/storage/metadata/tasks.py": "27475af5d9977d1ad40cd403f855a46c026412e1bdb19caeee8830f4abfdc7c4",
        "pyproject.toml": "598c9f0dd28cdc50b2a8dfe24bb6ad52db4b40fe8fffa082fca0962ec01c6e48"
      }
    },
    "ontology-enhancer": {
      "base_head": "b8de0d1a033ba8584af87a51b32fe3988a456c1c",
      "sha256": {
        "pyproject.toml": "d2fdfe8108ab19e5ad6d71afdb6a5e7108fb07e7a5fa9f337436960c5c901547",
        "src/ontology_enhancer/ov_runtime/README.md": "5d2d269b44be7397bd3c7f14c76d40da65af23154e6f43430c9ee79cdc8704e1",
        "src/ontology_enhancer/ov_runtime/__init__.py": "322d54a14194203873210d697d920e2d19bbd77b9f70c85196c832baa71e596f",
        "src/ontology_enhancer/ov_runtime/app.py": "f83b564a3927987514f8983a829e8a0b9309cbb528540b3c4ac748c61d3a83dd",
        "src/ontology_enhancer/ov_runtime/contracts/candidate_bundle.schema.json": "2255a45be668973d92f8774e326bc2a70056d356eb67f9b5c4b281271c2ba86e",
        "src/ontology_enhancer/ov_runtime/extractor.py": "9853dea1ee7f0bc84289dc006b008640e56b8dd0263072e6b11217a2ab46a40d",
        "tests/test_ov_runtime.py": "66090a528abafc57ebea9b5e4a11e41d12f094716a0a7e305ccfe8d2d9f347ad"
      }
    }
  }
}
```

只读 Agent 示例：`python scripts/ontology_agent_example.py --lab`。已使用隔离 agent 主体直读 OV，返回来源撤回后的 UNKNOWN 和证据缺口，输出见 results/readonly-agent-example.txt。
