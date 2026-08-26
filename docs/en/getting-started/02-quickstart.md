# Quick Start

This guide starts teamEvolver locally, connects it to OpenViking, and gets you into an executable Skill and Memory evolution workflow.

## Prerequisites

- Python 3.10+
- Node.js 18+ (only required when modifying the frontend; standard installation uses pre-built artifacts)
- An accessible OpenViking instance (local or cloud)
- An LLM API Key (Doubao/OpenAI compatible interface)

## 1. Installation

```bash
pip install -e ".[all]"
```

After installation, the `teamEvolver` CLI will be available.

## 2. Minimal Configuration

```bash
# Set service port
teamEvolver config service.port 52010
teamEvolver config service.host 0.0.0.0

# Configure OpenViking backend
teamEvolver config sharing.enabled true
teamEvolver config sharing.backend viking
teamEvolver config sharing.viking_deployment local
teamEvolver config sharing.viking_endpoint "http://localhost:1933"
teamEvolver config sharing.viking_account "default"
teamEvolver config sharing.viking_user "team"
teamEvolver config sharing.viking_team_api_key "your-service-or-admin-key"

# Configure LLM for evolution
teamEvolver config llm.api_base "https://ark.cn-beijing.volces.com/api/v3"
teamEvolver config llm.api_key "your-llm-key"
teamEvolver config llm.model_id "doubao-seed-evolving"
```

Configuration is saved to `~/.teamEvolver/config.yaml`.

## 3. Start the Service

```bash
teamEvolver start --daemon
```

Verify the service is running:

```bash
curl http://localhost:52010/health
# {"status":"ok"}

curl http://localhost:52010/status
# {"running":true,...}
```

For a self-hosted OpenViking server on another machine, set `sharing.viking_endpoint` to a reachable URL such as `http://10.0.0.8:1933`. You can also save and hot-reload these settings after login under **Governance → Runtime Status → OpenViking Deployment**.

## 4. Bootstrap and Open the Console

Visit [http://localhost:52010/](http://localhost:52010/). The first visit opens the administrator bootstrap screen, with `admin` prefilled; use a strong password in production. After setup, the console provides:

- Knowledge sources and mining jobs under **Skill Mining**
- Operations, candidate review, Langfuse, and Skill/team-Memory evolution under **Evolution Loop**
- Agent Workspace, Skill Lab, Memory Lab, and Platform Assets under **Asset Center**
- Model, users and permissions, OpenViking deployment, and health under **Governance**
- Built-in English and Chinese documentation with search

![Console Dashboard](/assets/teamEvolver-console-dashboard.png)

## 5. Connect Hermes Agent (Optional)

If you have a Hermes Coding Agent, you can install synchronization and feedback hooks:

```bash
export TEAMEVOLVER_URL="http://localhost:52010"
export TEAMEVOLVER_USER="your-name"
export HERMES_HOME="$HOME/.hermes"

# Install team skill sync hook
python teamEvolver/integrations/hermes_skill_sync/install.py \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --backend service \
  --url "$TEAMEVOLVER_URL" \
  --user "$TEAMEVOLVER_USER"

# Install session ingest hook
python teamEvolver/integrations/hermes_skill/install.py \
  --hermes-home "$HERMES_HOME" \
  --python python3 \
  --user "$TEAMEVOLVER_USER" \
  --url "$TEAMEVOLVER_URL"
```

After installation, restart Hermes; new sessions will automatically be reported to teamEvolver upon completion. See [Hermes Integration Guide](../agent-integrations/03-hermes) for detailed instructions.

## 6. Manually Trigger Evolution

```bash
curl -X POST http://localhost:52010/trigger
```

This immediately runs one evolution cycle: dequeue Sessions → extract Evidence → generate Candidates. In `validated` mode, Candidates then enter True Replay and release gates.

## 7. Stop the Service

```bash
teamEvolver stop
```

## Docker Compose

Use the repository image when you do not want to prepare a local Python/Hermes environment. The image builds the console, installs all dependencies, and bundles the OpenViking CLI:

```bash
docker compose up -d --build
docker compose ps
```

The service remains available at `http://localhost:52010/`; configuration and SkillMiner artifacts persist under `runtime/`.

## Next Steps

- [Core Concepts](../concepts/01-architecture): Deep dive into evolution loops, Skills, Memory, True Replay
- [Configuration Reference](../guides/01-configuration): Complete documentation of all configuration options
- [Agent Integration](../agent-integrations/01-overview): Connect your own Agent to teamEvolver
