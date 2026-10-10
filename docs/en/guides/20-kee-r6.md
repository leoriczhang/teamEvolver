# KEE R6 operations and recovery

See [deployment and detailed recovery](../../zh/guides/20-kee-r6.md), [handoff](../../kee-r6/HANDOFF.md) and [offline architecture](../../kee-r6/architecture.html).

Only local implementation and isolated validation are authorized in this delivery. No deployment or push. Package/CLI names, IDs, original database schema and existing asset writers remain compatible. No UpClaw changes, execution interceptors or additional Session/Judge pipeline.

Set service-level `compile.executor: internal` and enable the existing ontology module. The method model inherits TE tenant configuration; fixtures and optional structural reads are disabled in deployment templates. Source limits are 1,000 files, depth20, 10,000 entries, 20MiB total and 2MiB/file. The isolated Worker has bounded calls/time and no OV key or tools. Per-process model concurrency is8; cluster-wide limits require operational enforcement.

Wiki publication binds approved structure, sources, readers and baseline. Unknown writes reconcile hashes; index waiting does not repeat model calls or unchanged content. A30-minute publication deadline transfers unresolved work to manual reconciliation. Withdrawal denies access first, then recursively removes the whole version. OV unavailable or failed ACL operations cannot be reported as success.

Memory retains its existing two-stage writer and state, with a bounded internal Compile adapter. It does not execute Skill scripts/tools or add deletion/atomic-publication guarantees. Existing generic knowledge-mining Compile compatibility remains with its original executor.

Migration scripts default to dry-run, preserve history and never delete OV tables. Credentials are supplied through named environment variables. Verify counts, hashes and mappings before explicit apply; rollback deletes only exact imported payloads. Old fact-only assets require enrichment and fresh review, not invented ProcedurePacks.

The offline Wiki validator needs no service or credentials. External producers may upload audited Wiki directly to generic OV; that does not create a KEE management receipt.
