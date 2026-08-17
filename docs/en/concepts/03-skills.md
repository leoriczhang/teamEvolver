# Skill System

Skills are **executable, reusable task methods** that clearly specify applicable scenarios, operational steps, constraints, and supporting resources. A Skill is not a Prompt, not a script file, not an SOP document—it is an executable capability unit that has been team-validated and governed.

## Skill Structure

A Skill Bundle is a directory containing:

```
my-skill/
├── SKILL.md          # Main entry (required), declares metadata using YAML frontmatter
├── references/       # Reference documents (optional)
│   ├── policy.md
│   └── examples.md
├── scripts/          # Helper scripts (optional)
│   └── helper.py
└── assets/           # Supporting resources (optional)
    └── template.md
```

### SKILL.md Frontmatter

```yaml
---
name: my-skill                # Skill unique identifier
version: "1.2.0"              # Semantic version
description: Brief description of what this Skill does
applicable_when: Trigger condition description  # When to load this Skill
required_tools: [edit, bash]  # Required tools list
author: team-name             # Author/team
created_at: "2025-01-01"      # Creation date
updated_at: "2025-03-15"      # Update date
tags: [code-review, backend]  # Tags
---
```

## Skill Version Management

Skills in teamEvolver have three states:

| State | Description | Affects Agents |
|-------|-------------|----------------|
| **Published** | Currently active version, passed all gates | Agents pull this version by default |
| **Candidate** | Under validation/review, does not overwrite published version | Does not affect production Agents |
| **Archived** | Superseded by new version, but complete content and audit chain preserved | Not directly usable, can be rolled back to |

Version numbers follow Semantic Versioning (SemVer):
- **MAJOR**: Incompatible Skill structure changes
- **MINOR**: Backward-compatible feature additions
- **PATCH**: Backward-compatible bug fixes

`SkillMutationService` maintains commit records and tombstone markers for all versions.

## Skill Lifecycle

```
  Create/Modify
      │
      ▼
  ┌──────────┐    Fail      ┌──────────┐
  │ Candidate │───────────►│ Archived  │
  └────┬─────┘             └──────────┘
       │ Pass static checks
       ▼
  ┌──────────┐    Fail      ┌──────────┐
  │TrueReplay│───────────►│ Reject/Modify │
  └────┬─────┘             └──────────┘
       │ Checklist + Efficiency met
       ▼
  ┌──────────┐    Reject    ┌──────────┐
  │Human Review│──────────►│ Reject/Modify │
  └────┬─────┘             └──────────┘
       │ Approve
       ▼
  ┌──────────┐
  │ Published │◄── Rollback ── Historical versions
  └──────────┘
```

## Skill Synchronization

### Pull Mode

Agent calls at startup or before each session:

```
GET /internal/agents/context/skills
Authorization: Bearer <agent-access-token>
```

Returns currently published team Skill Bundle manifest and content.

### Push Mode

After Skill publication, `SkillMutationService` sends update notifications to push-capable Agents via outbox mechanism. Push adapters are registered in [skill_sync_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/skill_sync_adapters.py).

### Hermes Integration

Hermes implements automatic pull via `pre_llm_call` hook:

```
User initiates task → Hermes pre_llm_call hook → teamEvolver-sync → 
Pull latest Skill Bundle → Update external_dirs → Hermes native skill discovery
```

Installation script at [hermes_skill_sync/install.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/hermes_skill_sync/install.py).

## Boundary Between Skill and Memory

| Dimension | Skill | Memory |
|-----------|-------|--------|
| Content nature | Executable task methods (steps, workflows) | Retrievable facts and context |
| Prescribes execution flow | Yes, explicit steps | No, does not prescribe complete workflow |
| Validation method | True Replay comparative validation | DreamCycle semantic dedup/merge |
| Update gate | Strict (automatic + human) | Lenient (automatic or human per risk) |
| Typical examples | "How to do Code Review", "How to write unit tests" | "Team uses pnpm", "Service port is 52010" |

## Code Entry Points

| Module | Path |
|--------|------|
| Skill Bundle model | [skills/bundle.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/bundle.py) |
| Mutation service | [skills/mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py) |
| Render engine | [skills/render.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/render.py) |
| Registry | [skills/registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/registry.py) |
| Frontmatter parser | [skills/frontmatter.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/frontmatter.py) |
