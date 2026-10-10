# KEE R6 governance API

KEE reuses TE authentication, tenant membership, jobs, audit and PostgreSQL objects. See the [complete API contract](../../zh/api/20-kee-r6.md), [schemas](../../../team_ontology/kee/contracts) and [source baseline](../../kee-r6/BASELINE.md).

Use JSON Content-Type and authenticated tenant context. `/kee/v1/sources`, `/documents`, `/methods/build` freeze inputs and enqueue isolated internal Compile. Job operations include candidate editing, Schema confirmation, Prepare, approval, idempotent Commit, recovery, cancellation, rollback candidates and whole-release withdrawal. Editing invalidates approval. Approval expires after five minutes.

Readers can list/read authorized Wiki releases and submit feedback. They cannot mutate governance through direct API calls. All release readers must have original source access. Frozen text, exact Unicode evidence, Schema and output hashes remain bound to the candidate.

Optional `/kee/v1/ontology/query` and `/check` are disabled by default and are never MCP tools. Checks return TRUE/FALSE/UNKNOWN, independent conflict, gaps, fixed generation and evidence. Missing evidence is UNKNOWN. Checks never authorize business execution.

OV generic mkdir/ACL/content/wait/consistency/rm interfaces are used. Content verification, indexing and publication have separate states. OV has no package-atomic commit guarantee. Old ontology task IDs remain queryable/cancellable by their original adapter; native R6 installations retire old new-build requests with 410.
Native R6 hosts retire writes under `/te/enterprise/v1` with HTTP 410, except cancellation. Legacy capabilities returns a successor link. Historical jobs remain queryable/cancellable; historical sources and assets require the migration workflow. Keeping paths does not keep the old OV writer active.
