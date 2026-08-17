# Quick Start

This guide will help you run a local teamEvolver instance, connect a Hermes Agent, and complete your first evolution closed loop in 5 minutes.

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
teamEvolver config sharing.viking_endpoint "http://localhost:1933"
teamEvolver config sharing.viking_api_key "your-openviking-key"

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

## 4. Open the Console

Visit [http://localhost:52010/console](http://localhost:52010/console) in your browser to see:

- Service running status, queued sessions count, registered skills count
- Session history queue
- Skill Candidates awaiting review
- Evolution pipeline configuration panel

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

This immediately triggers one evolution cycle: pulling Sessions from queue → extracting Evidence → generating Candidates → True Replay validation → awaiting review.

## 7. Stop the Service

```bash
teamEvolver stop
```

## Next Steps

- [Core Concepts](../concepts/01-architecture): Deep dive into evolution loops, Skills, Memory, True Replay
- [Configuration Reference](../guides/01-configuration): Complete documentation of all configuration options
- [Agent Integration](../agent-integrations/01-overview): Connect your own Agent to teamEvolver
