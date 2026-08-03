# teamEvolver

<div align="center">

## A Skill Library, Sync Console, and Validation Workbench for Agent Teams

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Service-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Console-React%20%2B%20TypeScript-61DAFB.svg?logo=react&logoColor=111)](https://react.dev/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)
[![中文](https://img.shields.io/badge/README-中文-111827.svg)](./README.md)

**Turn real agent experience into reusable, synced, validated `SKILL.md` assets for your team.**

</div>

---

## Why teamEvolver?

Agents can already complete complex tasks, but team skills often remain a loose set of files on one machine:

- **Hard to share**: the same experience gets copied across members, machines, and agents.
- **Hard to separate**: personal preferences, customer facts, and team SOPs can mix, creating privacy and contamination risk.
- **Hard to version**: skill origin, publisher, version state, and live team content are difficult to keep aligned.
- **Hard to trust**: a skill may look polished, but there is little evidence that it improves task outcomes.

**teamEvolver is not about making agents remember more; it is a safe pipeline from real sessions to team capability.**
It turns scattered sessions into comparable evidence, separates personal and team assets, and publishes team skills through replay validation and version governance.

---

## Design Principles

- **Central evidence**: retain sessions, tool calls, success strategies, and failure reasons so cross-user patterns become visible.
- **Layered assets**: decide whether knowledge is shareable before deciding whether it should become `skill` or `memory`; personal assets stay isolated, team assets are published deliberately.
- **Validated release**: team `SKILL.md` assets pass aggregation, redaction, deduplication, replay validation, versioning, and rollback gates.

Hermes and other agents keep their native runtime model. teamEvolver delivers team skills through synced directories and hooks, so the agent's native skill system remains in control.

---

## Core Capabilities

<table>
  <tr>
    <td width="25%" valign="top">
      <h3>Skill Library</h3>
      <p>Read, create, edit, delete, package, and import standard <code>SKILL.md</code> bundles while preserving frontmatter and attachments.</p>
    </td>
    <td width="25%" valign="top">
      <h3>Team Sync</h3>
      <p>Use local object storage or OpenViking-compatible object storage with separate personal and team spaces.</p>
    </td>
    <td width="25%" valign="top">
      <h3>Web Console</h3>
      <p>A built-in React + TypeScript console for skills, users, candidate review, health checks, and model settings.</p>
    </td>
    <td width="25%" valign="top">
      <h3>True Replay</h3>
      <p>Run baseline and candidate branches in isolated sandboxes and validate skill changes with real tool trajectories.</p>
    </td>
  </tr>
</table>

---

## Architecture

```mermaid
flowchart LR
    subgraph Team["teamEvolver Team Service"]
        Console["Web Console"]
        API["FastAPI Service"]
        Registry["Skill Registry"]
        Validation["Validation Queue"]
    end

    subgraph Storage["Shared Storage"]
        Local["Local Object Store"]
        Viking["OpenViking-compatible Store"]
    end

    subgraph Agent["Agent Machines"]
        Sync["teamEvolver-sync Hook"]
        Dir["Synced SKILL.md Directory"]
        Hermes["Hermes Native Skills"]
    end

    Console --> API
    API --> Registry
    API --> Validation
    API <--> Local
    API <--> Viking
    Sync --> Viking
    Sync --> Dir
    Dir --> Hermes
```

The recommended path is shared storage, local sync, and native agent loading. Commands such as `skills_list`, `skill_view`, and `/skills` continue to come from the agent itself; teamEvolver only makes sure the team skill library reaches the machine reliably.

---

## Manual Installation

If external network access fails, run:

```bash
export http_proxy="http://sys-proxy-rd-relay.byted.org:8118"
export https_proxy="http://sys-proxy-rd-relay.byted.org:8118"
export no_proxy="localhost,.byted.org,byted.org,.bytedance.net,bytedance.net,127.0.0.0/8,169.254.0.0/16,100.64.0.0/10,172.16.0.0/12,192.168.0.0/16,10.0.0.0/8,::1,fe80::/10,fd00::/8,33.0.0.0/8,2605:340:CD00::/40,64:ff9b::/96,64:ff9b:1::/48"
```

### Server: Deploy teamEvolver

```bash
export TEAMEVOLVER_HOST="<server-ip-or-hostname>"

git clone https://github.com/leoriczhang/teamEvolver.git
cd teamEvolver
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[all]"
npm --prefix web-ui install
npm --prefix web-ui run build

teamEvolver config service.host 0.0.0.0
teamEvolver config service.port 52010
teamEvolver config skills.enabled true
teamEvolver config skills.dir ./skills
teamEvolver config sharing.enabled true
teamEvolver config sharing.backend viking
teamEvolver config sharing.viking_team_api_key "<team-key>"
teamEvolver config sharing.viking_personal_api_key "<personal-key>"
teamEvolver config sharing.viking_root_prefix "team-skill-evolver"
# DreamCycle reads personal-key sources and writes to the team-key space above.
# Additional AgentsHub peers merge their personal keys through the internal config API.
teamEvolver config dreamcycle.enabled true
teamEvolver config dreamcycle.auto_start true
teamEvolver config validation.enabled true
teamEvolver config validation.agentshub_url "http://<agentshub-host>:5173"

mkdir -p skills
teamEvolver start --daemon --port 52010
teamEvolver status
curl -fsS "http://127.0.0.1:52010/health"
curl -fsS "http://127.0.0.1:52010/status"
```

```text
http://<server-ip-or-hostname>:52010/console
```

On first launch, initialize the admin account. The default username and password are both `admin`; change them after deployment.

### Client: Deploy Hermes

```bash
export TEAMEVOLVER_REPO="/path/to/teamEvolver"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
export TEAMEVOLVER_URL="http://<server-ip-or-hostname>:52010"
export TEAMEVOLVER_USER="<unique-user-alias-for-this-machine>"
export TEAMEVOLVER_API_KEY=""
TEAMEVOLVER_AUTH_ARGS=()
[ -n "$TEAMEVOLVER_API_KEY" ] && TEAMEVOLVER_AUTH_ARGS=(--api-key "$TEAMEVOLVER_API_KEY")

python "$TEAMEVOLVER_REPO/teamEvolver/integrations/hermes_skill_sync/install.py" \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --backend service \
  --url "$TEAMEVOLVER_URL" \
  --user "$TEAMEVOLVER_USER" \
  "${TEAMEVOLVER_AUTH_ARGS[@]}"

python "$TEAMEVOLVER_REPO/teamEvolver/integrations/hermes_skill/install.py" \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --user "$TEAMEVOLVER_USER" \
  --url "$TEAMEVOLVER_URL" \
  "${TEAMEVOLVER_AUTH_ARGS[@]}"

python "$HERMES_HOME/skills/teamEvolver-sync/sync_skills.py"
hermes hooks list
curl -fsS "$TEAMEVOLVER_URL/status"
```

If Hermes is already running, execute this in the Hermes session:

```text
/reload-skills
```

Full coding agent integration instructions live in [docs/coding-agent.en.md](./docs/coding-agent.en.md).

---

## Console Map

<div align="center">
  <img src="docs/assets/teamEvolver-console-dashboard.png" width="900" alt="teamEvolver console evolution dashboard screenshot">
  <br>
  <sub>teamEvolver Console: evolution dashboard, team skill status, storage connectivity, and management entry points.</sub>
</div>

```mermaid
flowchart TB
    Home["Evolution Dashboard"]
    Candidates["Candidate Review"]
    Audit["Evolution Audit"]
    Health["System Health"]
    Skills["Skill Management"]
    Users["User Management"]
    Model["Model Settings"]

    Home --> Candidates
    Home --> Audit
    Home --> Health
    Skills --> Users
    Candidates --> Model
```

The console includes:

- **Evolution Dashboard**: storage connectivity, skill count, candidate queue, and service status.
- **Candidate Review**: inspect candidate skills before publication, with optional True Replay validation.
- **Evolution Audit**: review skill-evolution records.
- **System Health**: check service, storage, and key API availability.
- **Skill Management**: manage personal and team skills, including zip upload.
- **User Management**: manage users, roles, and personal/team storage credentials.
- **Model Settings**: configure an optional validation model and test connectivity.

---

## OpenViking / Object Storage

Remote sync uses teamEvolver's object-store abstraction. The default endpoint uses VolcEngine-hosted OpenViking:

```bash
teamEvolver config sharing.enabled true
teamEvolver config sharing.backend viking
teamEvolver config sharing.viking_team_api_key "<team-key>"
teamEvolver config sharing.viking_personal_api_key "<personal-key>"
teamEvolver config sharing.viking_root_prefix "team-skill-evolver"
```

For self-hosted OpenViking Server deployments, see [volcengine/OpenViking](https://github.com/volcengine/OpenViking) and override the default service URL with `teamEvolver config sharing.viking_endpoint "<your-server-url>"`.

Do not commit real API keys. Use local configuration, environment variables, or your deployment platform's secret manager.

---

## True Replay: Validate Skills with Real Trajectories

Plain-text A/B checks can only compare answers. True Replay starts real agents in isolated environments and runs baseline and candidate branches. If a task is incomplete, judge feedback becomes the next user message in the same session. The primary comparison dimensions are:

1. User/agent interaction turns needed to complete the task; fewer is better.
2. Tool-call count; fewer calls usually indicate a more direct execution path.
3. Total tokens, with input/output/cache/reasoning details retained.

```mermaid
flowchart LR
    Job["Candidate Job"] --> Base["Baseline Sandbox"]
    Job --> Cand["Candidate Sandbox"]
    Base --> TraceA["Tool Trace A"]
    Cand --> TraceB["Tool Trace B"]
    TraceA --> Score["Replay Scoring"]
    TraceB --> Score
    Score --> Decision["Keep / Revise / Publish"]
```

Install dependencies:

```bash
python -m pip install -e ".[truereplay]"
```

Replay a shared validation job:

```bash
python -m teamEvolver.true_replay --job-id <validation-job-id> --json
```

Replay a local JSON job file:

```bash
python -m teamEvolver.true_replay --job-file ./candidate_job.json --dry-run
python -m teamEvolver.true_replay --job-file ./candidate_job.json --json
```

True Replay creates temporary `HOME` and `HERMES_HOME` directories for both branches and does not modify your real agent configuration. To use a local agent checkout, set `HERMES_ORIGIN`.

---

## Project Layout

```text
teamEvolver/
├── teamEvolver/
│   ├── cli/              # teamEvolver command line
│   ├── config_store/     # local config store
│   ├── proxy/            # service routes, console, and admin APIs
│   ├── skills/           # SKILL.md management, bundling, sync
│   ├── storage/          # local / OpenViking storage backends
│   ├── integrations/     # Hermes integration
│   ├── validation/       # optional candidate-skill validation
│   ├── true_replay.py    # true A/B replay
│   └── web/              # built console assets
├── web-ui/               # React + TypeScript console source
├── tests/
├── scripts/
└── pyproject.toml
```

---

## Development

```bash
python -m pip install -e ".[dev,all]"
python -m pytest
```

Build the console and Python package:

```bash
npm --prefix web-ui install
npm --prefix web-ui run build
python -m pip install build
python -m build
```

---

## References

Related projects and references:
- [SkillClaw](https://github.com/AMAP-ML/SkillClaw): a multi-agent skill evolution project.
- [OpenSpace](https://github.com/HKUDS/OpenSpace): a quality-first skill hub for AI agents.
- [Hermes Agent](https://github.com/nousresearch/hermes-agent): optional runtime dependency for True Replay.
- [FastAPI](https://fastapi.tiangolo.com/): the teamEvolver service framework.
- [React](https://react.dev/) and [TypeScript](https://www.typescriptlang.org/): the teamEvolver console stack.

---

## License

MIT
