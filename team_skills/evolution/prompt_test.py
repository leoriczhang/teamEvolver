"""Ad hoc Prompt stage execution helpers."""
from __future__ import annotations

from typing import Any, Callable, Optional

from .prompt_studio import (
    _STAGE_CATALOG,
    _expand_shared_blocks,
    build_stage_messages,
    effective_prompt,
    stage_call_options,
)


async def run_stage_test(
    stage_id: str,
    session: dict[str, Any],
    *,
    system_prompt: Optional[str],
    llm_factory: Callable[[], Any],
    config: Any = None,
) -> dict[str, Any]:
    """Run one stage against a session and return system + user + model output.

    ``system_prompt`` overrides the effective prompt for this test only (so the
    operator can try edits before saving). ``llm_factory`` returns an
    ``AsyncLLMClient`` — injected so this module stays free of config coupling.

    The three skill-writing stages run the REAL agent loop  and additionally return a per-round
    transcript under ``rounds``.
    """
    if stage_id not in _STAGE_CATALOG:
        raise KeyError(f"unknown prompt stage: {stage_id}")
    # Resolve the system prompt to use: explicit test text, else effective.
    if system_prompt and system_prompt.strip():
        resolved_system = _expand_shared_blocks(stage_id, system_prompt)
    else:
        resolved_system = effective_prompt(stage_id, config=config)

    if stage_id in {"evolve_skill", "create_skill", "merge"}:
        return await _run_agent_stage_test(
            stage_id,
            session,
            resolved_system=resolved_system,
            llm_factory=llm_factory,
            config=config,
        )

    messages = build_stage_messages(stage_id, session, system_prompt=resolved_system)
    llm = llm_factory()
    output = await llm.chat(
        messages,
        **stage_call_options(stage_id, config),
        trace_name=f"team_skills.evolution.prompt_test.{stage_id}",
        trace_tags=["evolve", "prompt-studio", stage_id],
        trace_metadata={
            "component": "team_skills.evolution",
            "operation": "prompt_test",
            "stage_id": stage_id,
            "source_session_id": str(session.get("session_id") or ""),
        },
    )
    return {
        "stage_id": stage_id,
        "system_prompt": messages[0]["content"],
        "user_message": messages[1]["content"],
        "output": output,
    }


async def _run_agent_stage_test(
    stage_id: str,
    session: dict[str, Any],
    *,
    resolved_system: str,
    llm_factory: Callable[[], Any],
    config: Any = None,
) -> dict[str, Any]:
    """Run the real agent loop for a skill-writing stage in the test panel."""
    import json as _json

    from team_skills.evolution.agent.cards import build_merge_card, build_round1_user_msg
    from team_skills.evolution.agent.runner import run_skill_agent
    from team_skills.evolution.agent.tools import build_protocol_appendix
    from team_skills.evolution.stages.summarize import _extract_session_metadata, build_session_trajectory

    _extract_session_metadata(session)
    if not session.get("_trajectory"):
        session["_trajectory"] = build_session_trajectory(session)
    session.setdefault("_summary", session.get("_summary") or "")

    llm = llm_factory()
    rounds: list[dict[str, Any]] = []
    call_options = stage_call_options(stage_id, config)
    trace_metadata = {
        "component": "team_skills.evolution",
        "operation": "prompt_test",
        "stage_id": stage_id,
        "source_session_id": str(session.get("session_id") or ""),
    }

    if stage_id == "merge":
        existing = {
            "name": "example-skill",
            "description": "existing",
            "content": "Existing body.",
            "_version": 1,
        }
        incoming = {"name": "example-skill", "description": "incoming", "content": "Incoming body."}
        result = await run_skill_agent(
            llm,
            stage="merge",
            system_prompt=resolved_system,
            merge_versions=(existing, incoming),
            max_rounds=6,
            call_options=call_options,
            trace_kwargs={
                "trace_name": f"team_skills.evolution.prompt_test.{stage_id}",
                "trace_tags": ["evolve", "prompt-studio", stage_id],
                "trace_metadata": dict(trace_metadata),
            },
            round_log=rounds,
        )
        user_message = build_merge_card(existing, incoming)
        appendix = build_protocol_appendix("merge")
        system_message = f"{resolved_system}\n\n{appendix}" if resolved_system else appendix
    else:
        skill_name = (
            str(session.get("_probe_skill_name") or "example-skill")
            if stage_id == "evolve_skill"
            else ""
        )
        existing_names = [skill_name] if stage_id == "evolve_skill" else []
        result = await run_skill_agent(
            llm,
            stage=stage_id,
            system_prompt=resolved_system,
            skill_name=skill_name,
            sessions=[session],
            current_skill=None,
            existing_skill_names=existing_names,
            max_rounds=6,
            call_options=call_options,
            trace_kwargs={
                "trace_name": f"team_skills.evolution.prompt_test.{stage_id}",
                "trace_tags": ["evolve", "prompt-studio", stage_id],
                "trace_metadata": dict(trace_metadata),
            },
            round_log=rounds,
        )
        user_message = build_round1_user_msg(
            stage=stage_id,
            skill_name=skill_name,
            sessions=[session],
            current_skill=None,
            existing_skill_names=existing_names,
            evolution_context=None,
        )
        appendix = build_protocol_appendix(stage_id)
        system_message = (
            resolved_system.replace("{skill_name}", skill_name) + f"\n\n{appendix}"
        )

    output = (
        _json.dumps(result, ensure_ascii=False, indent=2)
        if isinstance(result, dict)
        else str(result)
    )
    return {
        "stage_id": stage_id,
        "system_prompt": system_message,
        "user_message": user_message,
        "output": output,
        "rounds": rounds,
    }
