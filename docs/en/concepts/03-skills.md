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

## Personal and Team Skills

| Dimension | Personal Skill | Team Skill |
|-----------|----------------|------------|
| Default path | `viking://resources/team-skill-evolver/peers/<account>/skills/` | `viking://resources/team-skill-evolver/skills/` |
| Edit permission | User or administrator | Administrator |
| Agent use | Agents mapped to that user | Authorized team Agents |
| Publication | Regular users submit a publish request; administrators can copy directly | Candidate or administrator mutation enters the version chain |

Personal Skills can be edited directly in Agent Workspace. A regular user submits a personal-to-team publish request for administrator approval; team-to-personal copying can install a team Skill into a personal space.

## Skill Version Management

Skills in teamEvolver have three states:

| State | Description | Affects Agents |
|-------|-------------|----------------|
| **Published** | Currently active version, passed all gates | Agents pull this version by default |
| **Candidate** | Under validation/review, does not overwrite published version | Does not affect production Agents |
| **Archived** | Superseded by new version, but complete content and audit chain preserved | Not directly usable, can be rolled back to |

The shared team-Skill registry uses monotonically increasing integer versions (`v1`, `v2`, ...). Rollback republishes a historical Bundle as a newer version and never moves or deletes old versions. `SkillMutationService` owns commits, tombstones, and the sync outbox.

## Skill Lifecycle

```text
Evidence → static checks → Candidate → True Replay
                                  ├─ gates pass → new version → Skill Sync
                                  ├─ gray zone → human review → publish or reject
                                  └─ gates fail → reject or revise
Administrator force-publish ─────────────────────┘
```

`publish_mode: validated` uses the Candidate validation queue. A Candidate may publish automatically after result-count, approval-count, and runtime-compatibility gates pass; gray-zone results can enter human review. `publish_mode: direct` bypasses the validation queue.

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
| Validation method | True Replay comparison | Memory Lab/Memory Replay; aggregation output constrained by its Skill |
| Update gate | Candidate gates, administrator release, or publish request | Agents write only personal Memory; only administrators/evolution paths write team Memory |
| Typical examples | "How to do Code Review", "How to write unit tests" | "Team uses pnpm", "Service port is 52010" |

## Code Entry Points

| Module | Path |
|--------|------|
| Skill Bundle model | [skills/bundle.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/bundle.py) |
| Mutation service | [skills/mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py) |
| Render engine | [skills/render.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/render.py) |
| Registry | [skills/registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/registry.py) |
| Frontmatter parser | [skills/frontmatter.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/frontmatter.py) |
