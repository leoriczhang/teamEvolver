"""User-editable OKF Skill for the aggregation compile pass.

The aggregation Skill is an ordinary OpenViking compile Skill; users own its
content so they can tune the OKF contract (frontmatter, type mapping, cross
references, dedup/conflict rules). This module only provides:

- a sensible default body that encodes the §7.1/§7.2/§7.3 contract discussed
  with the customer, with two corrections baked in:
    * an ``index`` page type is included, and
    * cross references use standard Markdown links, not ``[[wikilink]]``
      (OpenViking's renderer only builds relations from Markdown links).
- a stable fingerprint used by the incremental state to force a full
  recompile when the Skill changes.

Reading/writing the Skill content in OpenViking is done by the service via the
CLI/HTTP layer; here we keep the default text and fingerprint helper only.
"""

from __future__ import annotations

import hashlib

DEFAULT_OKF_SKILL_NAME = "team-memory-okf"

# NOTE: kept intentionally close to the customer's §7 contract, corrected for
# OpenViking compatibility. Users may replace this wholesale.
DEFAULT_OKF_SKILL_BODY = """---
name: team-memory-okf
description: >-
  Aggregate multiple users' OpenViking memories into an account-shared OKF
  knowledge tree under viking://resources. Merge same-subject items across users,
  de-duplicate, resolve conflicts by keeping the most complete/recent version,
  preserve concrete data, and record provenance. Produce OKF v0.2 Markdown pages
  with YAML frontmatter, a maintained index, and Markdown cross references.
---

# Team Memory OKF Aggregation

Turn the supplied per-user memory sources into durable, connected team knowledge.
Keep sources read-only. Do not invent facts; use only the supplied sources and
the existing target tree.

## Input format

Phase 1 is a deterministic copy and does not execute this Skill. A direct
per-user staging source contains `snapshot-*.jsonl`; each line records
`source_uri`, `relative_path`, `kind`, `modified_at`, `content_sha256`, and the
verbatim visible Memory text in `content`. Read the `content` field as source
material and retain `source_uri` as provenance. Inputs from later tree-reduce
levels are prior structured merge outputs.

## Output format (OKF v0.2)

Every page is a complete UTF-8 Markdown file beginning with YAML frontmatter:

```yaml
---
type: entity            # entity | concept | synthesis | index
title: "Canonical title"
description: "One factual sentence describing the page's retrieval purpose."
tags: [tag1, tag2]
status: stable
sources:
  - resource: "viking://user/<uid>/memories/<path>"
    author: "user:<uid>"
---
```

Keep `description` on one line. Follow the frontmatter with one H1 matching the title.

## Type mapping

| Output directory | type |
| --- | --- |
| `index.md` | `index` |
| `entities/**` | `entity` |
| `cases/**` | `entity` |
| `patterns/**` | `entity` |
| `experiences/**` | `entity` |
| `tools/**` | `concept` |
| `skills/**` | `concept` |
| `trajectories/**` | `concept` |
| `preferences/**` | `synthesis` |
| `events/**` | `synthesis` |
| `insights/**` | `synthesis` |
| `entities/team-overview.md` | `synthesis` |

## Directory layout

Write pages into kind subdirectories **relative to the compile target root**:
`entities/<page>.md`, `events/<page>.md`, `tools/<page>.md`, and so on. The
compile target is already the knowledge-base root — do NOT recreate the target's
own name as a subdirectory (e.g. never write `entities/entities/<page>.md`). The
root `index.md` sits directly at the target root.

## Merge rules

- Union of information: keep every user's distinct facts.
- De-duplicate: merge repeats, keep the most complete version.
- Conflicts: keep the most detailed/recent; annotate multiple sources.
- Preserve concrete numbers (parameters, success rates, dates).
- De-identify team knowledge: strip names and person-locating detail; present
  team-level, reusable statements.
- Record provenance in `sources` (source user + original memory URI).
- Baseline: one input may be a snapshot of the current team memory (its
  `source_uri` values live under `viking://resources`). Treat it as the
  authoritative existing knowledge base. Preserve human edits that appear only
  in the baseline, keep pages that no new source touches, and merge/de-duplicate
  new material onto it — never discard baseline content just because it is
  absent from this run's user sources.

## Cross references

Use standard Markdown links between pages, e.g. `[顺丰标快](entities/products/顺丰标快.md)`.
Do NOT use `[[wikilink]]` double-bracket syntax: OpenViking builds its relation
graph and backlinks only from Markdown links, so double brackets become inert
text. Link the first mention of another page in a paragraph; never self-link;
never add links inside headings, table rows, or code blocks.

## Index

Always create or update the root `index.md` (type `index`) as the navigation
catalog: one line per active page with its Markdown link and a one-line summary.
Preserve valid entries for pages not changed by this run.
"""


def skill_fingerprint(body: str) -> str:
    """Stable fingerprint of the Skill body for incremental invalidation."""
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
