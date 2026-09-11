# Customer Migration From Skill-Opt

The operational runbook is maintained in [Chinese](../../zh/guides/09-customer-migration.md). The customer profile is a single-instance, single-worker deployment with PostgreSQL persistence and account-scoped access. It is not a drop-in implementation of the legacy Flask API.

## Original Console

The original teamEvolver navigation and full views are retained, including mining, personal/team workspaces, platform assets, both Labs and governance. Tenant search stays in the sidebar; converter configuration and preflight are incremental additions.
The SkillMiner console is enabled by default, with persistent input and job files under `DATA_ROOT/.teamEvolver/skillminer`. Running mining jobs still requires a configured model and Hermes, which is not installed in the minimal customer image. Service-wide settings and SkillMiner remain restricted to the default account without hiding their navigation entries.
Both Labs remain reachable without OpenViking, but Memory and resource access require an OpenViking connection and the terminal requires its CLI. PostgreSQL internal objects and the OpenViking file tree are separate stores and must be verified separately.

## Procedure

1. Back up the legacy DATA_ROOT, converters, and database. Keep the legacy service available for rollback.
2. Install Python 3.11 dependencies from `docker/requirements.customer.lock`, then install the project with `--no-deps`.
3. Set `TEAMEVOLVER_PG_DSN` or the individual `OV_PG_*` variables. The runtime database role must not be SUPERUSER or BYPASSRLS.
4. Run `scripts/prepare_customer.py --legacy-root OLD_DATA --converters-dir OLD_SOURCE/converters` without `--apply`. Review `runtime/customer/migration-report.json`.
5. Resolve every blocked project. Doris projects require verified HTTP data parity and explicit `--allow-doris-api-fallback`, or a custom SourceAdapter.
6. Quiesce writes and repeat with `--apply`. The script preserves the service Root Key, imports tenant configuration and converter source, and publishes baseline Skill bundles idempotently.
7. Source `runtime/customer/customer.env`; start `compose.customer.yaml` or `scripts/start_customer.sh`. Bootstrap an administrator using the Root Key and a strong password.
8. Verify real Trace samples, project isolation, bounded concurrency, restart recovery, DEAP workspace cleanup and the publication process before switching the reverse proxy.

## Compatibility

The 73 supplied converters pass static checks and synthetic conversion tests. The legacy parser's pure functions are preserved. Custom imports, SDK calls, legacy disk-state assumptions and custom SQL need explicit adaptation; conversion failures do not silently fall back to another parser.

Legacy CAS, reports, notifications, schedulers, analysis prompts, direct Flask clients and production Skill delivery require separate integration. DEAP replay lacks complete usage metrics and therefore requires manual review. The converter preview worker has execution limits but is not an untrusted-code sandbox.

Rollback means stopping new publication, restoring the old entry point and scheduler, and retaining the new database and migration report. Externally published Skills must be rolled back explicitly; switching the UI alone does not reverse publication.
