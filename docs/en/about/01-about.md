# About teamEvolver

teamEvolver is a control plane for evolving the capabilities of an Agent team. It receives real Agent Sessions, extracts Evidence, generates Skill Candidates and validates them with True Replay, and publishes them as team Skills through Candidate Review; it also aggregates and maintains members' personal Memory into Team Memory.

## Design Principles

1. **Evidence driven**: every Skill or Memory change can be traced back to source Sessions
2. **Real validation**: Candidates are replayed against the Baseline in separate workspaces; the Checklist must pass before efficiency is compared
3. **White-box control**: prompts, model parameters and process parameters can be viewed, changed and tested in the console
4. **Governance first**: publishing goes through gates and human review, and the version chain and audit records are kept in full
5. **Tenant isolation**: all business data is isolated per tenant, enforced by PostgreSQL row-level security

## Tech Stack

| Layer | Components |
|------|------|
| Backend | Python 3.10+, FastAPI, asyncpg |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS |
| Authoritative store | PostgreSQL (required) |
| Knowledge store | OpenViking (Memory, Resources, Compile) |
| Observability | Langfuse (optional) |

The runtime has two roles, `api` and `worker`, each scaled horizontally; see [Architecture](../concepts/01-architecture.md).

## License

MIT License; see `LICENSE` in the repository root.

## Contributing

Before submitting code:

1. Run tests: `python -m pytest tests/`
2. Run lint: `ruff check .`
3. For frontend changes, run `npm --prefix web-ui test` and `npm --prefix web-ui run build`
4. Update the Chinese and English documents together and pass `node docs/scripts/check-docs-refs.mjs --strict`; see [Docs Maintenance](../api/99-docs-maintenance.md)
