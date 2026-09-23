# Agent deregistration upgrade and restore

This tree is the compatibility release, defaulting to `dual/push`; it does not migrate production data automatically.
New clients use [v2](../agent-integrations/06-protocol-v2.md).

## Release sequence

1. Deploy phases 0–4 with existing registration, mappings and v1 schemas intact.
2. Upgrade every Agent, verify tenant Skill pull isolation, and bind adapters for tenants using True Replay.
3. Collect legacy counters from `/api/agent-protocol/metrics`; require zero increments over a full release cycle, accounting for process resets.
4. Set `agent_protocol.identity_mode: tenant_user` and `skills.delivery_mode: pull`; observe a full business cycle.
5. Stop API/background writers, then run migration dry-run and apply.
6. Verify and resume business; release phase-6 cleanup separately after stability.

The switches and `replay.adapters_dir` are deployment-wide; `replay_adapter` is tenant-specific.
Declared users need no global users registry or tenant-membership lookup; the Key holder owns that declaration.

## Migration

[migrate_deregister.py](../../../scripts/migrate_deregister.py) supports `--tenant <id>`,
`--all-tenants`, `--dry-run`, `--apply` and `--restore`.
Evidence must come from this deployment. Fill real UTC timestamps and all tenants, including default:

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


PG reads each explicit tenant object-store scope. File mode supports default only.
The script validates all selected data, then backs up agents, Context and global users in their original
scope with counts and canonical JSON SHA-256 checksums. Whitespace is not part of the checksum.
It adds actual tenant IDs, retains user IDs and existing Session IDs, and preserves agent_id_legacy.
Single-tenant runs do not remove global mappings; an all-tenant run removes them last.
Repeated apply reuses immutable backups. Resume verifies checksums; unknown principals or scope conflicts stop the run.

The same-scope `deregister-*.manifest.json` supports resume/restore; CLI reports contain no backup bodies or keys.
If redirected CLI output is interrupted, retrieve this manifest from the default scope.
Apply and restore require stopped writers. Newer source changes are never overwritten automatically:
reconcile them offline before restoring an older snapshot.

## Rollback

Phases 0–4 can return to `dual/push`, with explicitly selected compatibility Replay adapters.
For phase 5, stop writers, restore/verify manifest objects, then restart with compatibility configuration.
Phase 6 requires both application-version rollback and data restore.
Switching to pull cancels old deliveries; after returning to push, reconcile versions manually.
Cancelled deliveries are not proof of synchronization.


## Separate phase-6 cleanup release

The [builder](../../../scripts/build_deregister_phase6.py) produces a separate source tree, cleanup.patch and per-file checksum manifest. Use a new output directory outside the repository so package-layout checks do not discover duplicate modules. Building never connects to production.

```bash
python scripts/build_deregister_phase6.py --output ../teamevolver-phase6
```

The cleanup release is always v2 + pull. Registration modules, subject mappings, legacy identity parsing, push workers and retry/discard routes are removed. Retired switches have no effect. Migration tools and historical schemas remain for recovery/reference only.

After a complete stable business cycle following phase 5, stop writers and run [finalize_deregister.py](../../../scripts/finalize_deregister.py). Add `"phase5_stable_cycle_observed": true` to actual cutover evidence. It verifies the persisted all-tenant phase-5 manifest and original backups; new registrations, remaining user mappings or Context records without tenant ownership stop cleanup.

```bash
python scripts/finalize_deregister.py --config config.yaml --phase5-manifest backup-manifest.json --dry-run > phase6-preview.json
python scripts/finalize_deregister.py --config config.yaml --phase5-manifest backup-manifest.json --apply --evidence cutover-evidence.json > phase6-manifest.json
```

Each tenant receives immutable `.pre-phase6.json` backups. All Context `agent_id_legacy` fields are removed before live registries are deleted. PG deletion uses explicit tenant scopes; file mode physically removes agents.json. Original `.pre-deregister.json` backups remain intact. Resume verifies checksums and never overwrites newer business data.

To roll back phase 6, stop writers, revert to the compatibility application, then restore pre-phase-6 state:

```bash
python scripts/finalize_deregister.py --config config.yaml --restore phase6-manifest.json --writers-stopped
```

To go back further, also run migrate_deregister.py restore. Context/user writes during observation can invalidate the older manifest: reconcile offline before restoring. Source tests do not substitute for production observation evidence.
