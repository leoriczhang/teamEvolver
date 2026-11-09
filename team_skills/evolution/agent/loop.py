"""Bounded Plan → Act → Observation → Final control shared by asset editors.

Adapters own the asset's tools, evidence gates, presentation and final contract.
This loop does not execute business tasks, inspect Replay cases or decide release
eligibility. The caller supplies the existing Worker-owned LLM client.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .contract import parse_turn


@dataclass
class LoopPlanResult:
    """One adapter-owned plan decision and its already rendered observation."""

    done: bool = False
    result: dict | None = None
    message: str | None = None
    log_kind: str = "plan_accepted"
    log_fields: dict = field(default_factory=dict)


class EditorLoopAdapter(Protocol):
    def plan(self, plan: dict, *, round_no: int, max_rounds: int) -> LoopPlanResult: ...

    async def tools(self, calls: list[dict]) -> list[dict]: ...

    def finalize(self, decision: dict) -> tuple[dict | None, list[str]]: ...

    def observe(
        self, observations: list[dict], *, round_no: int, max_rounds: int, must_submit: bool = False,
    ) -> str: ...

    def fallback(self) -> dict | None: ...


def _round_trace(trace_kwargs: dict | None, round_no: int) -> dict:
    if not trace_kwargs:
        return {}
    kwargs = dict(trace_kwargs)
    metadata = dict(kwargs.get("trace_metadata") or {})
    metadata["round"] = round_no
    kwargs["trace_metadata"] = metadata
    return kwargs


async def run_editor_loop(
    llm: Any,
    *,
    adapter: EditorLoopAdapter,
    system: str,
    first_user: str,
    max_rounds: int = 12,
    call_options: dict | None = None,
    trace_kwargs: dict | None = None,
    debug_dump: Callable[[int, str, str, str | None], None] | None = None,
    round_log: list[dict] | None = None,
    append_assistant: bool = False,
    parse: Callable[..., dict] | None = None,
) -> dict | None:
    """Run one bounded editing conversation, returning only adapter decisions.

    ``append_assistant=False`` preserves the established Skill observation
    protocol byte for byte. Adapters that need explicit assistant protocol turns
    may opt in. A parser override can enforce an asset-specific strict protocol;
    only the last round is offered ``allow_repair=True``, as in the Skill loop.
    """
    options = dict(call_options or {})
    parser = parse or parse_turn
    messages = [{"role": "system", "content": system}, {"role": "user", "content": first_user}]
    if debug_dump is not None:
        debug_dump(0, system, first_user, None)

    def log_round(round_no: int, kind: str, **extra: Any) -> None:
        if round_log is not None:
            round_log.append({"round": round_no, "type": kind, **extra})

    for round_no in range(1, max_rounds + 1):
        raw = await llm.chat(messages, **options, **_round_trace(trace_kwargs, round_no))
        if debug_dump is not None:
            debug_dump(round_no, system, str(messages[-1].get("content") or ""), raw)
        if append_assistant:
            messages.append({"role": "assistant", "content": raw})

        turn = parser(raw)
        if not turn.get("ok") and round_no == max_rounds:
            turn = parser(raw, allow_repair=True)
        if not turn.get("ok"):
            log_round(round_no, "parse_error", error=str(turn.get("error")))
            messages.append({
                "role": "user", "content": adapter.observe(
                    [{"tool": "(protocol)", "ok": False, "error": (
                        "上一轮无法解析为协议 JSON："
                        f"{turn.get('error')}。请重新输出一个合法的 "
                        "JSON 对象（plan / tool_calls / final），不要围栏。"
                    )}], round_no=round_no, max_rounds=max_rounds,
                    must_submit=round_no + 1 >= max_rounds,
                ),
            })
            continue

        if turn["type"] == "plan":
            outcome = adapter.plan(turn["plan"], round_no=round_no, max_rounds=max_rounds)
            log_round(round_no, outcome.log_kind, **outcome.log_fields)
            if outcome.done:
                return outcome.result
            if outcome.message is not None:
                messages.append({"role": "user", "content": outcome.message})
            continue

        if turn["type"] == "tool_calls":
            observations = await adapter.tools(turn["calls"])
            log_round(round_no, "tool_calls", tools=[
                str(call.get("tool") or "") for call in turn["calls"] if isinstance(call, dict)
            ])
            messages.append({
                "role": "user", "content": adapter.observe(
                    observations, round_no=round_no, max_rounds=max_rounds,
                    must_submit=round_no + 1 >= max_rounds,
                ),
            })
            continue

        finalized, errors = adapter.finalize(turn["decision"])
        if finalized is not None and not errors:
            default_action = getattr(adapter, "final_action_default", "")
            log_round(round_no, "final", action=str(finalized.get("action") or default_action))
            return finalized
        log_round(round_no, "final_rejected", errors=errors)
        if round_no < max_rounds:
            messages.append({
                "role": "user", "content": adapter.observe(
                    [{"tool": "(validation)", "ok": False, "errors": errors}],
                    round_no=round_no, max_rounds=max_rounds,
                    must_submit=round_no + 1 >= max_rounds,
                ),
            })
            continue
        break

    return adapter.fallback()
