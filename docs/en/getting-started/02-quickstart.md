# Quickstart

This guide starts an all-in-one development environment locally: one process runs both API and Worker and connects to a local PostgreSQL and OpenViking. Production uses separate `api` and `worker` roles; see [Production Deployment Guide](../guides/02-deployment.md).

## Prerequisites

- Python 3.11 (`pyproject.toml` declares `>=3.10`; the release image uses 3.11)
- PostgreSQL 14+ and an application role that is neither SUPERUSER nor BYPASSRLS
- A reachable OpenViking instance and Root Key
- An OpenAI-compatible LLM endpoint and key
- Node.js 20+ (only needed when changing `web-ui/`)

## 1. Install

```bash
bash scripts/install_teamEvolver.sh
source .venv/bin/activate
```

The script installs dependencies from `requirements.lock` with hashes, installs this repository with `--no-deps -e .`, and finally runs `pip check`.

## 2. Prepare the Database

The schema must be created by a database administrator and handed to the application role first; `teamEvolver schema apply` only creates tables inside an existing schema:

```sql
CREATE ROLE te_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE teamevolver OWNER te_app;
\c teamevolver
CREATE SCHEMA teamevolver AUTHORIZATION te_app;
```

```bash
export TEAMEVOLVER_PG_DSN="postgresql://te_app:...@127.0.0.1:5432/teamevolver"
teamEvolver schema apply
teamEvolver schema status
```

`schema apply` idempotently runs the version scripts under `teamEvolver/database/schema/` and writes the default configuration rows of the 5 modules for each tenant. First installs use an empty database.

## 3. Minimal Bootstrap Configuration

Bootstrap configuration is read-only and comes from environment variables and an optional YAML file (`TEAMEVOLVER_CONFIG_FILE`). Online configuration lives in PostgreSQL and is changed from the console.

```bash
export TEAMEVOLVER_ENV=local
export TEAMEVOLVER_ROOT_API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export TEAMEVOLVER_SECRET_MASTER_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export TEAMEVOLVER_OV_ENDPOINT="http://127.0.0.1:1933"
export TEAMEVOLVER_OV_API_KEY="<OpenViking Root Key>"
export TEAMEVOLVER_LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export TEAMEVOLVER_LLM_API_KEY="<LLM Key>"
export TEAMEVOLVER_LLM_MODEL="<model id>"
```

All bootstrap fields are listed in [Configuration Reference](../guides/01-configuration.md).

## 4. Start

```bash
teamEvolver start --role all
```

`all` is only allowed with `TEAMEVOLVER_ENV=local`. Verify:

```bash
curl http://localhost:52010/healthz
# {"ok":true,"role":"all"}
curl -i http://localhost:52010/readyz
# HTTP/1.1 200 OK  {"ready":true,"role":"all"}
```

## 5. Initialize and Open the Console

Open `http://localhost:52010/` in a browser. The first visit shows the administrator initialization page; in PostgreSQL mode the password needs at least 12 characters. After initialization the left navigation contains:

- Workbench: run overview, experiment workbench, run status
- Agents and capabilities: personal and team assets, platform assets, members and integration
- Knowledge and methods: experience library
- Evolution and experiments: datasets, evolution pipeline
- Review and publishing: review and publish entry, permission audit, tracing
- Settings and integration: platform users, tenant management, model configuration, data source integration

![Console Dashboard](/assets/teamEvolver-console-dashboard.png)

## 6. Connect an Agent and Trigger an Evolution

1. Create a tenant in "Tenant management" and copy the tenant key (`tevt_...`), which is shown only once.
2. Report one Session following [Agent Protocol v2](../agent-integrations/06-protocol-v2.md):

```bash
curl -X POST http://localhost:52010/ingest_session \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"schema_version":"teamevolver.agent-session.v2","protocol_version":"2.0",
       "session_id":"demo-001","runtime":{"type":"my-agent"},
       "runtime_context":{"user_id":"alice"},
       "turns":[{"turn_num":1,"prompt_text":"Analyze cost","response_text":"Cost report done."}]}'
# 202 {"accepted":true,"job_id":"...:session.normalize","status_url":"/api/jobs/..."}
```

3. After the Worker finishes the normalize, split, analyze, trace_analyze and archive stages, the Session appears in the console. A multi-topic Session is first expanded into traceable child Sessions, which complete the remaining stages independently.
4. Trigger Skill Evolution from "Review and publish entry" (`POST /api/skill-evolution/trigger`); the generated Skill Candidate enters candidate review.

## 7. Stop

Press `Ctrl+C` when running in the foreground. Use `teamEvolver stop` when started with `--daemon`.

## Next Steps

- [Core Concepts](../concepts/01-architecture.md): API/Worker, authoritative state and the evolution loop
- [Configuration Reference](../guides/01-configuration.md): bootstrap and online configuration
- [Agent Integration](../agent-integrations/01-overview.md): connect your own Agent
