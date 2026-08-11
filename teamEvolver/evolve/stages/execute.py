"""
Session-level execution helpers for the current teamEvolver.evolve pipeline.

The active flow is intentionally small:
- merge same-name conflicts when two evolved versions collide
- evolve an existing skill from aggregated session evidence
- create a brand-new skill from no-skill session groups

Older turn-level attribution / decision / execution prompts were removed so
`teamEvolver.evolve` matches the session-level pipeline used by `server.py`.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from ..kernel.enums import DecisionAction
from ..kernel.helpers import parse_single_skill
from ..kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

# Shared prompt blocks. These are injected into the create/evolve system
# prompts via sentinel replacement at module load (see the bottom of the file).
# They are the two levers that keep evolved skills reusable:
#   1. force task-instance values to be parameterized (not hardcoded), and
#   2. force the skill body to state that explicit user requests win over the
#      skill's default conventions.

_GENERALIZATION_RULES = """\
## MANDATORY: make the skill GENERAL — this is the single most important rule

First understand what "general" means here, because it decides whether the \
skill is worth anything.

A skill is NOT a record of the task you just watched. It is advice you are \
writing for a DIFFERENT person who, some time from now, will face the SAME \
KIND of task but with completely DIFFERENT specifics — a different input file, \
a different folder, a different date, a different company or person, a \
different set of numbers, and possibly a different number of sub-tasks. If \
anything you write would be wrong, confusing, or useless for that future \
person, it does not belong in the skill.

So while writing, keep asking yourself: "Is this true for EVERY task of this \
kind, or is it just what happened to be true in the one run I observed?" Only \
the first kind belongs in the skill.

### The test to apply to every concrete detail you are about to write

For each specific value, name, path, number, date, or fixed structure, run \
this test in your head:

  "If the next user came with the same kind of request but different inputs, \
   would this exact value still be correct?"

- If YES for every such user (it is a stable property of the environment, the \
tool, or the domain) → keep it concrete. These stable facts are exactly what \
makes the skill valuable, so do not vaguely hand-wave them away.
- If NO — it would change from one request to the next — then it is a detail \
of THIS task only. Do not write it as if it were the answer. Instead:
    1. replace it with a named placeholder (for example a `{...}` slot with a \
       clear name), and
    2. say in one sentence how to figure out the real value from the user's \
       actual request or the files present.

### Kinds of things that almost always FAIL the test (generalize them)

Think in categories, not in one memorized example:
- WHERE things are: the specific input path/filename you read, and the \
specific output path/filename you wrote. The naming *rule* can stay; the \
literal instance cannot.
- WHICH instance it was: any identifier that merely labels this particular \
task, dataset, run, or sub-question. Never let such a label become part of \
the skill name, and never use it to organize the body's structure.
- WHEN it happened: concrete dates, timestamps, reference/base dates.
- WHO/WHAT it was about: specific people, roles, levels, companies, products, \
routes, regions — the subject of this one task.
- HOW MUCH: quantities, budgets, and thresholds that came in AS THIS TASK'S \
INPUT. (A number that is a fixed rule of the domain may stay; a number that \
arrived with this request may not.)

### The subtle trap: do not freeze the SHAPE of the observed run either

Over-fitting is not only about literal values. If the sessions you observed \
happened to contain several sub-tasks or several report types, do NOT write \
the skill as a fixed list of "type 1 does X, type 2 does Y, type 3 does Z" \
mirroring exactly what you saw. That list is itself an artifact of this one \
run — a future request may have a different count, different types, or a \
combination you never observed. Instead, distill the COMMON underlying \
structure and the RULES that decide the shape from the input (e.g. "one \
section per grouping dimension, sorted by the primary metric, with a totals \
row"), so the same skill produces the right output no matter how many or which \
variants the next user brings.

### Name and description

- The NAME must describe a CAPABILITY that many future tasks share — never a \
single instance or a particular dataset/run label.
- The DESCRIPTION must not tie the skill to one file, one date, one subject, \
or one fixed enumeration. Warning sign: if your "NOT for" clause has to \
exclude "other files / other dates / non-standard variants", the skill is \
over-fit — generalize it instead of excluding those cases.

### Final check

- For every placeholder you introduce, you MUST also say how to derive its \
real value from the task input.
- After generalizing, if nothing reusable is left — the skill would only \
restate one task's specific inputs and its answer — then there is no general \
skill here: choose skip / do not create it."""

_USER_OVERRIDE_RULE = """\
## MANDATORY user-precedence section in the skill body

The skill encodes DEFAULT conventions distilled from past runs — not immutable \
law. The skill body you produce MUST include a short, explicit section (2-4 \
lines) stating that:
- these defaults apply only when the user does not specify otherwise, and
- any explicit user requirement — a different output path, filename, section \
structure, format, scope, or content — OVERRIDES the skill's defaults, and
- when the user's request conflicts with a default here, follow the user.

Write this section in the SAME language as the rest of the skill body (a \
Chinese body gets a Chinese section, an English body an English section). Keep \
it brief and state it once; do not scatter the caveat across the document."""

_EVIDENCE_ROUTING_RULES = """\
## MANDATORY evidence routing: team Skill vs user Memory vs runtime

Before choosing an action, classify every proposed lesson into exactly one
bucket:

- `team_skill`: a reusable SOP, stable environment fact, tool/domain procedure,
  or guardrail that is useful across different users and task instances.
- `user_memory`: a preference or habit attributable to one user, such as visual
  taste, preferred format, tone, layout, workflow, or recurring personal choice.
- `task_requirement`: an explicit requirement or correction for the current
  deliverable only.
- `agent_runtime`: interruption, context loss, retry behavior, tool outage,
  orchestration failure, or the agent failing to follow already-correct guidance.
- `insufficient_evidence`: an observation with no demonstrated causal link to
  the skill.

Only `team_skill` evidence may change a shared Skill. A repeated preference from
the same user is stronger user Memory evidence, not team Skill evidence. A user
correction does not automatically reveal a missing SOP: first decide whether it
is a one-task requirement, a stable personal preference, or a universal
procedure.

Sessions with the same non-empty `evaluation_profile` form a controlled
evaluation cohort whose purpose is to learn a shared team method. When two or
more independent users in that cohort converge on the same artifact-driven
correction or acceptance rule, classify that common rule as `team_skill` if it
fits the Skill's purpose. Do not demote it to `task_requirement` merely because
each user stated it while correcting a specific deliverable.

Preserve cohort-common rules at their operational specificity. Exact narrative
sequences, section ratios, metadata vocabularies, design-token contracts,
required notes/audits, and validation thresholds are reusable Skill content
when they recur independently across the cohort. Do not replace them with vague
advice such as "honor the requested structure" or "check relevant tokens"; that
throws away the method the cohort demonstrated. Keep subjects, audiences,
filenames, one-user visual taste, and other non-recurring details in
`user_memory` or `task_requirement`.

An explicit requested output technology or format is not evidence that a skill
covering that technology is irrelevant. Capability boundaries may overlap; for
example, an HTML-based presentation may legitimately use frontend visual-design
guidance. Add a `NOT for` exclusion only when evidence shows the current skill's
guidance causally harmed results and an alternative routing consistently
improved them. Mere co-occurrence, injection, or availability of another skill
is not causal evidence.

Treat broad user labels such as "PPT", "slides", or "presentation" as the
presentation goal, not automatically as a native `.pptx` file contract. An HTML
slide deck is a valid implementation unless the user explicitly requires
PowerPoint/PPTX, native slide objects, or editable presentation-file delivery,
or later rejects HTML. Do not call HTML delivery a routing failure merely
because a PPT-specific skill was also available.

If all useful observations fall into `user_memory`, `task_requirement`,
`agent_runtime`, or `insufficient_evidence`, choose `skip`. Do not encode those
observations into a shared Skill.

Every output must include `evidence_classification` with arrays named
`team_skill`, `user_memory`, `task_requirement`, `agent_runtime`, and
`insufficient_evidence`. Each `team_skill` item must state the reusable claim,
supporting session IDs, and the causal link to the proposed edit.

Keep this classification compact: at most 8 `team_skill` items and at most 4
items in every other bucket. Each claim, causal link, or string item must be at
most 240 characters. Do not repeat session summaries inside the output. Keep
the rationale under 600 characters and the Skill body under 8,000 characters."""

_MERGE_SKILL_SYSTEM = """\
You are a skill engineer for teamEvolver.

Two versions of the SAME skill exist because separate evolution actions produced different content under the same name.

Your task: merge the two versions into a single, superior version that combines the best parts of both.

## Merge principles

- Preserve ALL actionable guidance from both versions - do not drop useful content.
- Eliminate redundancy - deduplicate overlapping sections.
- If the two versions contradict each other, prefer the more specific or concrete guidance.
- When one version hardcodes a task-instance value (a concrete input/output path, filename, dataset id, date, entity, or task-input threshold) and the other expresses it as a parameter/pattern, prefer the parameterized form — never let the merge re-introduce a hardcoded instance value.
- If either version contains a user-precedence section (explicit user requests override the skill's defaults), the merged skill MUST retain one such section, written in the merged body's language.
- Preserve the stronger existing structure unless reorganization is clearly beneficial.
- Do not rewrite either version just to make it look more standardized.
- Keep the same name.
- The merged description should cover trigger conditions from both versions.
- Only keep metadata or extra frontmatter that still helps the merged skill.
- The merged content should stay concise, but do not force a rigid section template.

## Output format

Return EXACTLY one JSON object with:
- "name": same name
- "description": merged trigger description
- "content": merged Markdown body only, not a full SKILL.md with frontmatter

Optional fields:
- "metadata": merged metadata when genuinely useful
- "extra_frontmatter": preserved or merged extra frontmatter when justified

No markdown fences. Output ONLY valid JSON.
"""

_EVOLVE_FROM_SESSIONS_SYSTEM = """\
You are a skill engineer for teamEvolver's skill evolution system.

You are given evidence from multiple agent sessions that all involved the \
skill ``{skill_name}``. Each session contains a programmatic trajectory \
(step-by-step tool calls and outcomes) and an LLM-generated analysis.

Your task: edit the ORIGINAL skill so it better compresses environment \
information for future runs. Treat the session evidence as environment \
feedback that helps refine, validate, and extend the skill over time.

Analyze the session evidence alongside the current skill content, then \
decide the best course of action:

If `active_candidate_feedback` is present, a previous candidate already failed
or was inconclusive in True Replay. Treat its failed checklist items and reason
codes as new evidence: preserve passing behavior, make a materially different
targeted repair, and do not return the same candidate wording or edit summary.

1. **improve_skill** - The skill content needs targeted edits based on the \
session evidence (for example missing guidance, outdated information, or \
unclear instructions). Produce the updated skill.

2. **optimize_description** - The skill body content is fine, but its \
description causes it to be matched to wrong tasks. Rewrite ONLY the \
description for more precise triggering. Do NOT change the body content.

3. **create_skill** - The session evidence reveals a recurring pattern, \
capability gap, or reusable strategy that does NOT belong in the current \
skill ``{skill_name}``. A brand-new, separate skill is needed. The current \
skill remains unchanged. Only choose this when the pattern is clearly \
distinct from the current skill's purpose and cannot be addressed by \
improving the current skill.

4. **skip** - The skill is working well enough, or the evidence is too weak \
or ambiguous to justify changes. No action needed.

## Editing principles (for improve_skill)

- Treat the CURRENT skill as the source of truth, not as a rough draft to be rewritten.
- Read the original skill first, then the session evidence.
- Default to targeted edits, not rewrites.
- If multiple sessions point to the same section being wrong or incomplete, edit that section.
- If failures are only corner cases, add the missing checks or clarify constraints without changing unrelated sections.
- Preserve the original structure, heading order, terminology, and effective guidance, especially parts supported by successful sessions.
- Only rewrite an entire section if the evidence shows that section is materially wrong.
- If the skill contains concrete API details (endpoints, ports, payload schemas, tool names) that are factually correct, KEEP them even if the agent did not use them well. These details are the skill's core value.

## Hard constraints

- Do NOT casually change task API contracts, ports, endpoints, output paths, payload formats, or required filenames. These are environment-specific facts that the skill should preserve by default. EXCEPTION: if the session evidence clearly shows that an API endpoint, port, or contract has changed, update the skill to reflect the corrected value.
- Do NOT remove core capabilities, API references, command patterns, or tool-usage examples unrelated to the observed failures.
- Do NOT turn the skill into a different skill with a different purpose.
- Do NOT rewrite the whole skill from scratch.
- Do NOT impose a new template, new mandatory section structure, or a different writing style unless the evidence requires it.
- Do NOT add generic best-practice guidance (for example rate-limit handling, retry logic, state management, or caching) that the agent should handle on its own. Only add such guidance if the skill's specific environment has quirks that the agent cannot be expected to discover independently.

## Conservative editing mode

- Prefer preserving existing section headings and ordering.
- If a successful session supports a section, leave that section untouched unless failure evidence explicitly contradicts it.
- Prefer tightening or clarifying an existing section over adding a brand-new section.
- Do not introduce a new large section unless failure evidence is strong and the existing structure cannot express the fix.
- If you add a new checklist item, keep it short and tied to the observed failure.

## Distinguishing skill problems from agent problems

Not every failure is a skill deficiency. Before editing, consider whether the failure was caused by:
- **The skill** (wrong, missing, or misleading guidance) -> edit the skill.
- **The agent** (subagent misuse, unnecessary restarts, context overflow, or not reading the skill properly) -> these are agent-level issues; do NOT bloat the skill with agent-runtime advice.
- **The environment** (mock API instability, network flakiness, docker quirks) -> if sessions show repeated API failures or timeouts, add a brief note about the instability so the agent knows to expect it. Keep it short; do NOT turn the skill into a retry tutorial.

Critical anti-pattern to avoid: if the skill ALREADY contains correct environment information (API endpoints, ports, payload formats, tool names) and the agent failed because it did NOT use that information, that is an AGENT problem, not a skill problem. Do NOT delete the correct API information from the skill and replace it with instructions like "go read utils.py" or "inspect the mock service code". The whole point of the skill is to save the agent from having to discover those details.

When in doubt, prefer **skip** over a speculative edit.

__EVIDENCE_ROUTING_RULES__

## Bundle file changes

- The current skill may include an "Editable bundle files" section.
- Use `skill.file_changes` only when session evidence proves a bundled file
  needs to change. Do not rewrite scripts merely for style.
- Supported operations are `upsert` and `delete`.
- An `upsert` must contain the complete replacement UTF-8 `content`.
- A `delete` must target an existing file shown in the editable section.
- Every operation must include a concise evidence-based `reason`.
- Never target `SKILL.md` through file changes; update the `content` field.
- Existing files not listed in the editable section are preserved by the
  service and must not be mentioned in file changes.
- When a script interface changes, update the Skill body so future agents call
  the new interface correctly.

## Skill-writing principles (for create_skill)

- The new skill must serve a DIFFERENT purpose than ``{skill_name}``.
- Prefer a short, action-oriented name (lowercase-hyphenated slug).
- The name MUST differ from all existing skill names listed below.
- A skill should compress environment information (API endpoints, ports, payload formats, tool-specific quirks, or domain procedures), not generic best practices the agent already knows.
- Description should state what the skill does and triggering contexts, including "NOT for: ..." exclusion conditions. 2-4 sentences.
- Content should be domain-specific, practically useful, and non-obvious.
- Keep it concise, reusable, and evidence-driven.
- Write reusable guidance, not a failure summary or postmortem.

The following two blocks apply to ANY action that writes skill body or \
description content (improve_skill and create_skill). When improving an \
existing skill, also DE-HARDCODE any task-instance literals already baked into \
the current skill (input/output paths, filenames, dataset ids, dates, entities, \
task-input thresholds) by turning them into the placeholders below — this \
counts as a targeted fix, not a disallowed rewrite. Adding the required \
user-precedence section is likewise an allowed, expected edit even in \
conservative mode.

__GENERALIZATION_RULES__

__USER_OVERRIDE_RULE__

## Output format

Return EXACTLY one JSON object (no markdown fences, no extra text):
For every non-skip action, emit keys in this order: `action`, `skill`,
`rationale`, `evidence_classification`. The Skill must appear before the
evidence classification so a length-limited response still contains the
candidate body.

If action is improve_skill:
```
{{
  "action": "improve_skill",
  "skill": {{
    "name": "<keep same name>",
    "description": "<keep or improve>",
    "content": "<full updated Markdown body>",
    "category": "<keep or update>",
    "file_changes": [
      {{"path": "scripts/run.py", "operation": "upsert", "content": "<full UTF-8 file>", "reason": "..."}},
      {{"path": "scripts/old.sh", "operation": "delete", "reason": "..."}}
    ],
    "edit_summary": {{"preserved_sections": [...], "changed_sections": [...], "notes": "..."}}
  }},
  "rationale": "<why, synthesizing the evidence>",
  "evidence_classification": {{
    "team_skill": [{{"claim": "...", "supporting_session_ids": ["..."], "causal_link": "..."}}],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }}
}}
```

If action is optimize_description:
```
{{
  "action": "optimize_description",
  "skill": {{
    "name": "<keep same name>",
    "description": "<rewritten description with Use-when and NOT-for conditions>"
  }},
  "rationale": "<why>",
  "evidence_classification": {{
    "team_skill": [{{"claim": "...", "supporting_session_ids": ["..."], "causal_link": "..."}}],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }}
}}
```

If action is create_skill:
```
{{
  "action": "create_skill",
  "skill": {{
    "name": "<new-lowercase-slug, MUST differ from {skill_name} and all existing names>",
    "description": "<2-4 sentences with triggering contexts and NOT-for conditions>",
    "content": "<skill body in Markdown>",
    "file_changes": [
      {{"path": "scripts/run.py", "operation": "upsert", "content": "<full UTF-8 file>", "reason": "..."}}
    ]
  }},
  "rationale": "<why a new skill is needed and why the current skill should not absorb this>",
  "evidence_classification": {{
    "team_skill": [{{"claim": "...", "supporting_session_ids": ["..."], "causal_link": "..."}}],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }}
}}
```

If action is skip:
```
{{
  "action": "skip",
  "rationale": "<why skipping>",
  "evidence_classification": {{
    "team_skill": [],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }}
}}
```
"""

_CREATE_FROM_SESSIONS_SYSTEM = """\
You are a skill engineer for teamEvolver.

You are given summaries of agent sessions where no existing skill was \
referenced. These sessions may reveal patterns that could be captured as a \
reusable skill for future sessions.

Analyze whether these sessions reveal a common pattern, recurring challenge, \
or reusable strategy that would benefit future agent sessions if captured as \
a skill.

1. **create_skill** - A clear, teachable pattern exists that compresses environment-specific knowledge the agent cannot reliably discover on its own. Produce the new skill.
2. **skip** - No actionable or generalizable pattern. The sessions are too diverse, too domain-specific, or the issues are not solvable by skills.

__EVIDENCE_ROUTING_RULES__

## Skill-writing principles (for create_skill)

- A skill should compress environment information (API endpoints, ports, payload formats, tool-specific quirks, or domain procedures), not generic best practices the agent already knows.
- Prefer a short, action-oriented name (lowercase-hyphenated slug).
- Description should state what the skill does and triggering contexts, including "NOT for: ..." exclusion conditions. 2-4 sentences.
- Content should be domain-specific, practically useful, and non-obvious.
- Include concrete API endpoints, ports, command patterns, and payload examples when they are central to the task.
- Keep it concise, reusable, and evidence-driven.
- Write reusable guidance, not a failure summary or postmortem.
- Use imperative instructions. Organize naturally for the task.
- Do NOT add generic agent-runtime advice (rate-limit handling, retry logic, caching strategies, or state management) unless the environment has specific quirks that require it.

__GENERALIZATION_RULES__

__USER_OVERRIDE_RULE__

## When to skip

Prefer skip when:
- The failures are caused by agent-level issues (retries, context overflow, or subagent misuse) rather than missing knowledge.
- The sessions are too diverse to extract a single coherent skill.
- The pattern is something the agent should handle via general intelligence.

## Optional bundle files

Create a bundled text file only when the reusable capability genuinely needs
executable support. Put operations in `skill.file_changes`, use `upsert`, return
the complete UTF-8 content, and include an evidence-based reason. Never create
or edit `SKILL.md` through file changes.

## Output format

Return EXACTLY one JSON object (no markdown fences, no extra text):

If action is create_skill:
```
{{
  "action": "create_skill",
  "rationale": "<why creating this skill>",
  "evidence_classification": {{
    "team_skill": [{{"claim": "...", "supporting_session_ids": ["..."], "causal_link": "..."}}],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }},
  "skill": {{
    "name": "<lowercase-hyphenated-slug>",
    "description": "<2-4 sentences with triggering contexts and NOT-for>",
    "content": "<skill body in Markdown>",
    "file_changes": [
      {{"path": "scripts/run.py", "operation": "upsert", "content": "<full UTF-8 file>", "reason": "..."}}
    ]
  }}
}}
```

If action is skip:
```
{{
  "action": "skip",
  "rationale": "<why skipping>",
  "evidence_classification": {{
    "team_skill": [],
    "user_memory": [],
    "task_requirement": [],
    "agent_runtime": [],
    "insufficient_evidence": []
  }}
}}
```
"""


def _inject_shared_blocks(template: str) -> str:
    return (
        template.replace("__GENERALIZATION_RULES__", _GENERALIZATION_RULES)
        .replace("__USER_OVERRIDE_RULE__", _USER_OVERRIDE_RULE)
        .replace("__EVIDENCE_ROUTING_RULES__", _EVIDENCE_ROUTING_RULES)
    )


# Expand the shared generalization / user-precedence blocks into the prompts
# that write skill content. Done once at import time.
_EVOLVE_FROM_SESSIONS_SYSTEM = _inject_shared_blocks(_EVOLVE_FROM_SESSIONS_SYSTEM)
_CREATE_FROM_SESSIONS_SYSTEM = _inject_shared_blocks(_CREATE_FROM_SESSIONS_SYSTEM)

_EVOLVE_DEBUG_DIR = ""


def set_evolve_debug_dir(path: str) -> None:
    """Set the debug dump directory used by session-level evolution calls."""
    global _EVOLVE_DEBUG_DIR
    _EVOLVE_DEBUG_DIR = str(path or "").strip()


def _get_evolve_debug_dir() -> str:
    return _EVOLVE_DEBUG_DIR


async def execute_merge(
    llm: AsyncLLMClient,
    existing_skill: dict,
    incoming_skill: dict,
) -> Optional[dict]:
    """Merge two versions of the same skill into one superior version."""
    user_msg = (
        f"## Version A (currently in shared storage, v{existing_skill.get('_version', '?')})\n\n"
        f"Name: {existing_skill.get('name', '')}\n"
        f"Description: {existing_skill.get('description', '')}\n"
        f"Category: {existing_skill.get('category', 'general')}\n\n"
        f"Content:\n```\n{existing_skill.get('content', '')}\n```\n\n"
        f"---\n\n"
        f"## Version B (newly evolved)\n\n"
        f"Name: {incoming_skill.get('name', '')}\n"
        f"Description: {incoming_skill.get('description', '')}\n"
        f"Category: {incoming_skill.get('category', 'general')}\n\n"
        f"Content:\n```\n{incoming_skill.get('content', '')}\n```"
    )
    messages = [
        {"role": "system", "content": _MERGE_SKILL_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    raw = await llm.chat(
        messages,
        max_tokens=8192,
        temperature=0.3,
        **_llm_trace_kwargs("merge_skill", skill_name=str(existing_skill.get("name") or "")),
    )
    return parse_single_skill(raw)


def _build_skill_block(skill: dict) -> str:
    block = (
        f"## Current skill\n\n"
        f"Name: {skill.get('name', '')}\n"
        f"Description: {skill.get('description', '')}\n"
        f"Category: {skill.get('category', 'general')}\n\n"
        f"Content:\n```\n{skill.get('content', '')}\n```\n\n"
    )
    editable_files = (
        skill.get("_editable_bundle_files")
        if isinstance(skill.get("_editable_bundle_files"), dict)
        else {}
    )
    if not editable_files:
        return block
    parts = [
        block,
        "## Editable bundle files\n\n",
        "Only the files listed below may be updated or deleted. New files may "
        "be created only with an allowed extension. Return full replacement "
        "content for every upsert.\n\n",
    ]
    for path, content in sorted(editable_files.items()):
        parts.extend(
            [
                f"### `{path}`\n\n",
                f"```text\n{content}\n```\n\n",
            ]
        )
    return "".join(parts)


def _build_session_evidence(sessions: list[dict], max_sessions: int = 30) -> str:
    """Format session evidence (trajectory + summary) for LLM prompts."""
    blocks: list[str] = []
    for session in sessions[:max_sessions]:
        session_id = session.get("session_id", "?")
        avg_prm = session.get("_avg_prm")
        prm_str = f", avg PRM: {avg_prm}" if avg_prm is not None else ""
        has_errors = session.get("_has_tool_errors", False)
        err_str = ", has tool errors" if has_errors else ""
        skills = session.get("_skills_referenced") or set()
        skill_str = f", skills: {sorted(skills)}" if skills else ""
        runtime_context = (
            session.get("runtime_context")
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        evaluation_profile = str(
            runtime_context.get("evaluation_profile")
            or session.get("_evaluation_profile")
            or ""
        ).strip()
        profile_str = (
            f", evaluation_profile: {evaluation_profile}"
            if evaluation_profile
            else ""
        )

        aggregate = session.get("aggregate") or {}
        aggregate_str = ""
        if aggregate:
            parts: list[str] = []
            rollout_count = aggregate.get("rollout_count", 0)
            mean_score = aggregate.get("mean_score")
            stability = aggregate.get("stability", "")
            success_count = aggregate.get("success_count", 0)
            fail_count = aggregate.get("fail_count", 0)
            if rollout_count:
                parts.append(f"{rollout_count} rollouts")
            if mean_score is not None:
                parts.append(f"mean ORM={mean_score:.3f}")
            if success_count or fail_count:
                parts.append(f"success={success_count} fail={fail_count}")
            if stability:
                parts.append(f"stability={stability}")
            if parts:
                aggregate_str = f", {', '.join(parts)}"

        trajectory = session.get("_trajectory", "")
        summary = session.get("_summary", "")

        parts = [
            f"### Session {session_id}{prm_str}{aggregate_str}{err_str}"
            f"{skill_str}{profile_str}"
        ]
        if trajectory:
            parts.append(f"**Trajectory**:\n{trajectory}")
        if summary:
            parts.append(f"**Analysis**:\n{summary}")
        if not trajectory and not summary:
            parts.append("(no data)")
        blocks.append("\n\n".join(parts))

    if len(sessions) > max_sessions:
        blocks.append(f"\n... and {len(sessions) - max_sessions} more sessions")

    return "\n\n---\n\n".join(blocks)


def _build_cross_cycle_context(context: Optional[dict]) -> str:
    if not isinstance(context, dict) or not context:
        return ""
    debt = context.get("change_debt") if isinstance(context.get("change_debt"), dict) else {}
    reconsider = bool(debt.get("reconsideration_ready"))
    guidance = (
        "Repeated independent evidence has crossed the reconsideration threshold. "
        "Do not skip merely because any single session is weak; resolve the repeated "
        "signal with a targeted edit or explain why the accumulated evidence conflicts."
        if reconsider
        else (
            "Use recent evidence for responsiveness and historical evidence as a "
            "regression guard. A skip keeps the unresolved evidence for later cycles."
        )
    )
    payload = {
        "total_evidence_sessions": context.get("total_evidence_sessions"),
        "current_session_count": context.get("current_session_count"),
        "recent_session_ids": context.get("recent_session_ids") or [],
        "historical_session_ids": context.get("historical_session_ids") or [],
        "tool_error_sessions": context.get("tool_error_sessions"),
        "mean_judge_score": context.get("mean_judge_score"),
        "change_debt": debt,
        "active_candidate_job_id": context.get("active_candidate_job_id") or "",
        "active_candidate_feedback": (
            context.get("active_candidate_feedback")
            if isinstance(context.get("active_candidate_feedback"), dict)
            else {}
        ),
    }
    return (
        "## Cross-cycle evidence state\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Evolution guidance: {guidance}\n\n"
    )


def _build_evaluation_cohort_context(sessions: list[dict]) -> str:
    cohorts: dict[str, list[str]] = {}
    for session in sessions:
        runtime_context = (
            session.get("runtime_context")
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        profile = str(
            runtime_context.get("evaluation_profile")
            or session.get("_evaluation_profile")
            or ""
        ).strip()
        session_id = str(session.get("session_id") or "").strip()
        if profile and session_id and session_id not in cohorts.setdefault(profile, []):
            cohorts[profile].append(session_id)
    controlled = {
        profile: session_ids
        for profile, session_ids in cohorts.items()
        if len(session_ids) >= 2
    }
    if not controlled:
        return ""
    return (
        "## Controlled evaluation cohorts\n\n"
        f"{json.dumps(controlled, ensure_ascii=False, indent=2)}\n\n"
        "Rules independently repeated inside one listed cohort are the intended "
        "team method. Preserve those rules concretely in the candidate Skill; "
        "only per-user and per-task details should be generalized away.\n\n"
    )


def _write_debug_dump(stem: str, system: str, user_msg: str, raw: str | None = None) -> None:
    debug_dir = _get_evolve_debug_dir()
    if not debug_dir:
        return

    dump_dir = Path(debug_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / f"{stem}_system.txt").write_text(system, encoding="utf-8")
    (dump_dir / f"{stem}_user.txt").write_text(user_msg, encoding="utf-8")
    if raw is not None:
        (dump_dir / f"{stem}_raw_output.txt").write_text(raw, encoding="utf-8")
    logger.info("[DebugDump] wrote %s prompt artifacts to %s", stem, dump_dir)


def _llm_trace_kwargs(operation: str, *, skill_name: str = "", sessions: list[dict] | None = None) -> dict:
    run_id = os.environ.get("EVOBENCH_RUN_ID", "").strip()
    session_ids = [
        str(session.get("session_id") or "").strip()
        for session in (sessions or [])
        if str(session.get("session_id") or "").strip()
    ]
    tags = ["team-skill-evolver", "teamEvolver.evolve", operation]
    if run_id:
        tags.append(run_id)
    if skill_name:
        tags.append(f"skill:{skill_name}")
    tags.extend(session_ids[:20])
    metadata = {
        "source": "team-skill-evolver",
        "component": "teamEvolver.evolve",
        "operation": operation,
        "skill_name": skill_name or None,
        "evobench_run_id": run_id or None,
        "session_count": len(sessions or []),
        "session_ids": session_ids[:50],
    }
    return {
        "trace_name": f"team-skill-evolver:{operation}" + (f":{skill_name}" if skill_name else ""),
        "trace_tags": tags,
        "trace_metadata": metadata,
        "trace_session_id": f"team-skill-evolver:{run_id}:{operation}:{skill_name or 'no-skill'}" if run_id else "",
        "trace_user_id": run_id,
    }


async def evolve_skill_from_sessions(
    llm: AsyncLLMClient,
    skill_name: str,
    sessions: list[dict],
    current_skill: Optional[dict],
    existing_skill_names: list[str],
    *,
    evolution_context: Optional[dict] = None,
) -> Optional[dict]:
    """Combined decision + execution for one existing-skill session group."""
    system = _EVOLVE_FROM_SESSIONS_SYSTEM.replace("{skill_name}", skill_name)
    skill_section = _build_skill_block(current_skill) if current_skill else ""
    evidence = _build_session_evidence(sessions)
    user_msg = (
        f"{skill_section}"
        f"{_build_cross_cycle_context(evolution_context)}"
        f"{_build_evaluation_cohort_context(sessions)}"
        f"## Session evidence ({len(sessions)} sessions)\n\n"
        f"{evidence}\n\n"
        f"## Existing skill names in the library\n\n"
        f"{', '.join(existing_skill_names) or '(none)'}\n"
    )

    stem = skill_name.replace("/", "_")
    _write_debug_dump(stem, system, user_msg)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    raw = await llm.chat(
        messages,
        max_tokens=16384,
        temperature=0.4,
        **_llm_trace_kwargs("evolve_skill", skill_name=skill_name, sessions=sessions),
    )
    _write_debug_dump(stem, system, user_msg, raw)
    return _parse_evolve_result(raw, skill_name)


async def create_skill_from_sessions(
    llm: AsyncLLMClient,
    sessions: list[dict],
    existing_skill_names: list[str],
    *,
    evolution_context: Optional[dict] = None,
) -> Optional[dict]:
    """Combined decision + execution for the no-skill session bucket."""
    evidence = _build_session_evidence(sessions)
    user_msg = (
        f"{_build_cross_cycle_context(evolution_context)}"
        f"{_build_evaluation_cohort_context(sessions)}"
        f"## Session evidence ({len(sessions)} sessions)\n\n"
        f"{evidence}\n\n"
        f"## Existing skill names in the library\n\n"
        f"{', '.join(existing_skill_names) or '(none)'}\n"
    )

    stem = "no_skill"
    _write_debug_dump(stem, _CREATE_FROM_SESSIONS_SYSTEM, user_msg)

    messages = [
        {"role": "system", "content": _CREATE_FROM_SESSIONS_SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    raw = await llm.chat(
        messages,
        max_tokens=16384,
        temperature=0.4,
        **_llm_trace_kwargs("create_skill", sessions=sessions),
    )
    _write_debug_dump(stem, _CREATE_FROM_SESSIONS_SYSTEM, user_msg, raw)
    return _parse_evolve_result(raw, "")


def _parse_evolve_result(raw: str, skill_name: str) -> Optional[dict]:
    """Parse the combined decision+execution JSON from the LLM."""
    import re

    raw = re.sub(r"<think>.*?</think>", "", raw.strip(), flags=re.DOTALL)
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`")

    try:
        result = json.loads(clean)
    except (json.JSONDecodeError, ValueError):
        start = clean.find("{")
        end = clean.rfind("}")
        if start == -1:
            logger.warning("[SessionExec] no JSON object found for '%s'", skill_name)
            return None
        candidate = clean[start : end + 1] if end > start else clean[start:]
        try:
            result = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            try:
                from json_repair import repair_json

                result = repair_json(candidate, return_objects=True)
            except Exception:  # noqa: BLE001 - malformed model output is non-fatal.
                logger.warning(
                    "[SessionExec] failed to parse evolve result for '%s'",
                    skill_name,
                )
                return None

    if not isinstance(result, dict):
        return None

    action = result.get("action", DecisionAction.SKIP)
    raw_classification = result.get("evidence_classification")
    classification: dict[str, list] = {}
    for bucket in (
        "team_skill",
        "user_memory",
        "task_requirement",
        "agent_runtime",
        "insufficient_evidence",
    ):
        values = (
            raw_classification.get(bucket)
            if isinstance(raw_classification, dict)
            else []
        )
        classification[bucket] = values if isinstance(values, list) else []

    if action == DecisionAction.SKIP:
        return {
            "action": DecisionAction.SKIP,
            "rationale": result.get("rationale", ""),
            "evidence_classification": classification,
        }

    if not classification["team_skill"]:
        logger.warning(
            "[SessionExec] action '%s' for '%s' has no reusable team-skill evidence; treating as skip",
            action,
            skill_name,
        )
        return {
            "action": DecisionAction.SKIP,
            "rationale": (
                "Candidate suppressed because its evidence classification contains "
                "no reusable team-skill evidence. "
                + str(result.get("rationale", "") or "")
            ).strip(),
            "evidence_classification": classification,
        }

    skill_data = result.get("skill")
    if not isinstance(skill_data, dict):
        logger.warning("[SessionExec] action '%s' but no skill data for '%s'", action, skill_name)
        return None

    if action == DecisionAction.CREATE:
        if not skill_data.get("name"):
            logger.warning("[SessionExec] create_skill action but no name provided for '%s'", skill_name)
            return None
        if skill_data["name"] == skill_name:
            logger.warning(
                "[SessionExec] create_skill returned same name '%s' - treating as improve",
                skill_name,
            )
            action = DecisionAction.IMPROVE
    elif skill_name and not skill_data.get("name"):
        skill_data["name"] = skill_name

    return {
        "action": action,
        "rationale": result.get("rationale", ""),
        "skill": skill_data,
        "evidence_classification": classification,
    }
