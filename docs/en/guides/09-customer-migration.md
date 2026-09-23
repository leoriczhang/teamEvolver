# Customer Migration From Skill-Opt

The operational runbook is maintained in [Chinese](../../zh/guides/09-customer-migration.md). PostgreSQL is authoritative for Sessions, Skills, candidates, reviews, versions, mutation commits, and outbox events. Pod-local Skill directories are rebuildable caches. Keep one instance during migration; restore two Pods only after the PostgreSQL migration and single-Pod acceptance checks pass. This is not a drop-in implementation of the legacy Flask API.

## Original Console

The console retains the core teamEvolver navigation and views, including mining, the Experiment Workbench, personal/team assets, platform assets, and governance. Tenant search stays in the sidebar; converter configuration and preflight are incremental additions.
The SkillMiner console is enabled by default, with persistent input and job files under `DATA_ROOT/.teamEvolver/skillminer`. Running mining jobs still requires a configured model and Hermes, which is not installed in the minimal customer image. Service-wide settings and SkillMiner remain restricted to the default account without hiding their navigation entries.
The Experiment Workbench owns Skill bundle editing and True Replay debugging. Personal Memory at `viking://user` and Team Resources at `viking://resources` are available from a standalone sidebar page; standalone Skill Lab, Memory Lab, and Skill import/export actions are removed. Asset browsing requires OpenViking, and the terminal requires its CLI. Platform Assets visualizes service-owned PostgreSQL and NAS objects directly and does not depend on OpenViking.

## Procedure

1. Back up the legacy DATA_ROOT, converters, and database. Keep the legacy service available for rollback.
2. Install Python 3.11 dependencies from `docker/requirements.customer.lock`, then install the project with `--no-deps`.
3. Set `TEAMEVOLVER_PG_DSN` or the individual `OV_PG_*` variables. The runtime database role must not be SUPERUSER or BYPASSRLS. Enable `storage_pg` and use `sharing.skill_backend=postgres` plus `sharing.session_backend=postgres`.
4. Run `scripts/prepare_customer.py --legacy-root OLD_DATA --converters-dir OLD_SOURCE/converters` without `--apply`. Review `runtime/customer/migration-report.json`.
5. Resolve every blocked project. Doris projects require verified HTTP data parity and explicit `--allow-doris-api-fallback`, or a custom SourceAdapter.
6. Quiesce writes and repeat with `--apply`. The script preserves the service Root Key, imports tenant configuration and converter source, and publishes baseline Skill bundles idempotently.
7. If existing Pods contain local control-plane state, stop publication and rollout. Run `teamEvolver storage migrate-local-control-plane --source-root POD_A_ROOT --source-root POD_B_ROOT --tenant-id ACCOUNT_ID --dry-run`; resolve every conflict, then repeat with `--apply`. The command strips the local tenant prefix, uses batched CAS writes, verifies hashes, and writes an `admin_migrations/local-to-pg/<migration_id>.json` marker.
8. Source `runtime/customer/customer.env`; start `compose.customer.yaml` or `scripts/start_customer.sh`. Bootstrap an administrator using the Root Key and a strong password.
9. Require both effective backends and `multi_replica_safe=true` from `/storage/status`. Verify real Trace samples, project isolation, restart recovery, archived Skill bundles, DEAP workspace cleanup, and publication before restoring two Pods and switching the reverse proxy.

## Compatibility

The 73 supplied converters pass static checks and synthetic conversion tests. The legacy parser's pure functions are preserved. Custom imports, SDK calls, legacy disk-state assumptions and custom SQL need explicit adaptation; conversion failures do not silently fall back to another parser.

Legacy CAS, reports, notifications, schedulers, analysis prompts, and direct Flask clients require separate integration. DEAP response mapping is implemented here, but the upstream upclaw service must return per-turn `messages`, `tool_call_count`, `total_tokens`, and `traceId`. The converter preview worker has execution limits but is not an untrusted-code sandbox.

Rollback means stopping new publication, restoring the old entry point and scheduler, and retaining the new database and migration report. Externally published Skills must be rolled back explicitly; switching the UI alone does not reverse publication.
