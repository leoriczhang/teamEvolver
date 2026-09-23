# Agent 去注册化升级与恢复

此仓库为兼容发布，默认 `dual/push`，不自动改动生产数据。
新客户端使用 [v2](../agent-integrations/06-protocol-v2.md)。

## 发布时序

1. 部署阶段 0–4；保留存量注册、用户映射和 v1 Schema。
2. 升级全部 Agent，验证所有租户 Skill pull 隔离，为需要 True Replay 的租户绑定 adapter。
3. 持续采集 `/api/agent-protocol/metrics` 的 legacy 计数，一个完整发布周期增量为 0；进程重启重置计数时要累计监控数据。
4. 切换 `agent_protocol.identity_mode: tenant_user`、`skills.delivery_mode: pull`，观察至少一个完整业务周期。
5. 停止 API/后台写入，使用迁移脚本 dry-run、apply。
6. 验证并恢复业务，稳定后单独发布阶段 6 清理代码包。

开关和 `replay.adapters_dir` 是部署级配置；只有 `replay_adapter` 是租户可选绑定。
声明用户无需全局 users 注册或租户成员查询；Key 持有方对其 Account 中的用户声明负责。

## 迁移

脚本 [migrate_deregister.py](../../../scripts/migrate_deregister.py) 支持 `--tenant <id>`、
`--all-tenants`、`--dry-run`、`--apply`、`--restore`。
观察证据必须来自本部署；下列只是格式，填写真实时间和全部租户（含 default）：

```json
{
  "observation_start": "<UTC ISO8601>",
  "observation_end": "<UTC ISO8601>",
  "legacy_requests": 0,
  "full_release_cycle_observed": true,
  "strict_business_cycle_observed": true,
  "clients_use_user_id": true,
  "writers_stopped": true,
  "skill_pull_isolation_verified": ["default", "tenant-a"],
  "replay_binding_verified_or_unused": ["default", "tenant-a"]
}
```

```bash
python scripts/migrate_deregister.py --config config.yaml --all-tenants --dry-run > dry-run.json
python scripts/migrate_deregister.py --config config.yaml --all-tenants --apply   --evidence cutover-evidence.json > backup-manifest.json
python scripts/migrate_deregister.py --config config.yaml   --restore backup-manifest.json --writers-stopped
```


PG 逐租户显式读取 object-store scope；文件模式只支持 default。
脚本先校验所有目标数据，再在原 scope 备份 agents、Context 和全局 users，生成逐对象计数与
canonical JSON SHA-256 manifest。原空白格式不参与 checksum。
Context 补实际 tenant_id，沿用 user_id 和旧 Session ID，保留 agent_id_legacy。
单租户迁移不删除全局用户映射；全部租户迁移完成后最后才清理用户映射。
重复 apply 复用不可覆盖备份；失败续跑仍校验原 checksum。未知主体或租户冲突直接停止。

同 scope 的 `deregister-*.manifest.json` 是重试/恢复依据；CLI 输出不包含备份正文和密钥。
若重定向输出因进程中断不完整，可从 default scope 读取该 manifest。
apply/restore 必须停写。源数据自备份后出现额外变动会被拒绝覆盖，需离线核对合并，
不能把历史备份直接覆盖到仍在写入的生产环境。

## 回滚

阶段 0–4 可切回 `dual/push`，新接入与 Replay 使用明确的兼容 adapter。
阶段 5 停写后恢复 manifest 内对象、校验 checksum，再以兼容配置启动。
阶段 6 同时需要回退应用版本及数据恢复；代码回退不代替数据恢复。
旧 outbox 在切 pull 时已取消，回到 push 后需要管理员核对待分发版本，不能宣称旧 delivery 已同步。


## 阶段 6 独立清理包

[构建脚本](../../../scripts/build_deregister_phase6.py) 从兼容版生成独立源码、cleanup.patch 和逐文件 checksum manifest；输出目录必须不存在，且放在仓库外，避免被模块布局检查扫描。构建不连接生产环境。

```bash
python scripts/build_deregister_phase6.py --output ../teamevolver-phase6
```

清理版固定 v2 + pull，移除注册表模块、subject 映射、旧身份解析、push worker 和 retry/discard 接口。旧配置开关不再生效。迁移与历史 Schema 留作恢复/查阅，不参与请求解析。

阶段 5 稳定一个完整业务周期后，停写并使用 [finalize_deregister.py](../../../scripts/finalize_deregister.py)。在已有真实观察证据中增加 `"phase5_stable_cycle_observed": true`。脚本验证已落盘的全租户阶段 5 manifest 和原始备份，拒绝阶段 5 之后的新注册、残留用户映射和缺少租户归属的 Context。

```bash
python scripts/finalize_deregister.py --config config.yaml --phase5-manifest backup-manifest.json --dry-run > phase6-preview.json
python scripts/finalize_deregister.py --config config.yaml --phase5-manifest backup-manifest.json --apply --evidence cutover-evidence.json > phase6-manifest.json
```

每个租户生成 `.pre-phase6.json` 备份，先移除 Context 的 `agent_id_legacy`，全部成功后才删除 live agents.json。PG 按显式 tenant scope 删除对象，文件模式物理删除文件。原 `.pre-deregister.json` 备份保持不变。失败可以重试，仍会检查 checksum；不会把新增业务数据覆盖为旧快照。

阶段 6 回滚先停写、切回兼容应用，再恢复阶段 6 前状态：

```bash
python scripts/finalize_deregister.py --config config.yaml --restore phase6-manifest.json --writers-stopped
```

若还需退回阶段 5 前，继续执行 migrate_deregister.py 的 restore。观察期内新增的 Context/用户数据可能使旧 manifest 的恢复校验失败，必须离线合并后再恢复。生产观察证据不由源码测试代替。
