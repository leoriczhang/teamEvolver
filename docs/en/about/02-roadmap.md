# Roadmap

## Current Capabilities

- Agent protocol: Session ingest, Agent Context, Replay branches and Skill pull, authenticated by tenant Key + `user_id`
- Data source onboarding: versioned adapters in PostgreSQL, preview, pull and daily scheduled pull
- Skill evolution: Session analysis, grouping by Skill, Agent-loop improvement and creation, conflict merge, dataset synthesis
- True Replay: Baseline vs Candidate in separate workspaces, Checklist gate and efficiency comparison
- Skill versions: append-only version chain, idempotent mutations, rollback and tombstones, delivered to Agents by pull
- Team Memory: cross-user aggregation, DreamCycle maintenance, change records and Memory Replay
- Successful experiences: experience library, pattern compilation and OpenViking projection
- Knowledge base mining: document upload and conversion, OpenViking Compile and Wiki graph
- Console: experiment workbench, Prompt Studio, personal and team assets, platform assets, tenant and member management
- Runtime: separate API and Worker roles, PostgreSQL task queue, leases and fences, multi-replica support

## Pending

- End-to-end acceptance against a real OpenViking environment (Compile, retrieval cleanup, ACL)

## Long-term Direction

- Cross-organization Skill validation and sharing
- Automatically discovering team capability bottlenecks and starting evolution
