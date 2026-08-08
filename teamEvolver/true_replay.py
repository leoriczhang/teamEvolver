"""True A/B replay prototype for skill-candidate validation.

The existing candidate validation (``_run_candidate_replay`` in
``orchestrator.py``) is a *text* replay: it feeds the original user instruction
to ``llm.chat`` once per branch and judges the returned text. Tools never run,
so tool-oriented skills (e.g. "install a skill from a path", which copies files
and edits config) get systematically under-scored — the model can only *say*
what it would do, not actually do it.

This module implements a *true* replay: for a single instruction it spins up
**two real Hermes agents** in isolated, disposable sandboxes that differ only in
whether the candidate skill's guidance is injected, lets each run the full tool
loop for real (``TERMINAL_ENV=local``, ``HERMES_YOLO_MODE=1``), then judges both
trajectories — including tool-call correctness, not just the final text — with
an LLM judge inspired by ``agent_evolve_evaluation``'s trajectory dimension.

Safety model
------------
The candidate skill writes into ``~/.hermes`` and shells out ``cp``/config edits
whose ``~`` expands to ``$HOME``. So each branch runs in its own subprocess with
**both** ``HOME`` and ``HERMES_HOME`` redirected into a throwaway temp dir. The
real ``~/.hermes`` is never touched. Referenced *source* paths (read-only) must
exist on this machine or the case is skipped — a true replay of "install from
/path/X" is meaningless if /path/X isn't there.

This is the evolve server's primary candidate-skill validator: the cycle auto-
runs it for every queued candidate, and ``EvolveServer._run_candidate_replay``
shells out to the ``--json`` mode below. It also runs standalone:

    python3 -m teamEvolver.true_replay --job-id <validation-job-id>

Add ``--dry-run`` to only resolve cases + check paths without running agents.

Hermes runtime
--------------
True replay imports ``run_agent.AIAgent`` from the open-source Hermes agent
(https://github.com/nousresearch/hermes-agent). Install it with
``pip install 'teamEvolver[truereplay]'``, or point the ``HERMES_ORIGIN``
env var at a local checkout. When Hermes is absent the replay degrades to a
per-branch error rather than crashing the server. See ``resolve_hermes_origin``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from .checklist import (
    compile_common_checklist,
    evaluate_branch_checklist,
    objective_replay_decision,
    scope_checklist_for_case,
)

# Canonical checkout root (holds the teamEvolver/ package).
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIN_EFFICIENCY_ONLY_GAIN = 0.05
_EFFICIENCY_WEIGHTS = {
    "interaction_turns": 0.60,
    "tool_call_count": 0.25,
    "total_tokens": 0.15,
}


def replay_candidate_accepted(
    *,
    baseline_score: float,
    candidate_score: float,
    min_score: float,
    tolerance: float,
    efficiency_score: float,
) -> bool:
    """Legacy compatibility helper.

    The live release path uses ``objective_replay_decision`` with checklist
    gates and weighted execution cost. Keep this only for callers of the old
    public helper while they migrate.
    """
    no_regression = candidate_score >= baseline_score - tolerance
    quality_ok = candidate_score >= min_score and no_regression
    quality_improved = candidate_score > baseline_score
    efficiency_improved = (
        no_regression
        and efficiency_score >= _MIN_EFFICIENCY_ONLY_GAIN
    )
    return bool(
        quality_ok
        and (quality_improved or efficiency_improved)
        and efficiency_score >= -0.10
    )


def resolve_hermes_origin() -> Optional[str]:
    """Locate the open-source Hermes agent runtime (nousresearch/hermes-agent).

    True replay imports ``run_agent.AIAgent`` from Hermes. Resolution order:

    1. ``HERMES_ORIGIN`` env var — an explicit local checkout path (for
       developers hacking on Hermes itself). Wins if it holds ``run_agent.py``.
    2. A sibling ``../hermes_origin`` checkout next to this repo, if present.
    3. ``None`` — meaning "rely on an installed ``hermes-agent`` package"
       (``pip install 'teamEvolver[truereplay]'``); the worker imports
       ``run_agent`` straight off ``sys.path`` with no path injection.

    Returning a path means "inject this dir onto sys.path before importing";
    returning ``None`` means "import the installed package as-is"."""
    env = os.environ.get("HERMES_ORIGIN", "").strip()
    if env and (Path(env) / "run_agent.py").exists():
        return env
    sibling = _REPO_ROOT.parent / "hermes_origin"
    if (sibling / "run_agent.py").exists():
        return str(sibling)
    return None

# ---------------------------------------------------------------------------
# Candidate job loading (reuses the running server's config + storage bucket).
# ---------------------------------------------------------------------------


def load_candidate_job(job_id: str) -> Optional[dict[str, Any]]:
    """Load a validation job from the same backend the live server uses.

    Returns ``None`` when the job does not exist so callers (``evaluate_job``)
    can frame a clean ``not_found`` verdict instead of crashing the subprocess."""
    sys.path.insert(0, str(_REPO_ROOT))
    from teamEvolver.config_store import ConfigStore
    from teamEvolver.validation.store import ValidationStore

    config = ConfigStore().to_config()
    store = ValidationStore.from_config(config)
    return store.load_job(job_id) or None


def load_candidate_job_file(path: str) -> dict[str, Any]:
    """Load a validation job JSON file for standalone true replay."""
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("job file must contain a JSON object")
    data.setdefault("job_id", Path(path).stem)
    return data


def load_source_session(session_id: str) -> Optional[dict[str, Any]]:
    """Load an archived source session so replay can select its native runtime."""
    if not session_id:
        return None
    try:
        from teamEvolver.config_store import ConfigStore
        from teamEvolver.session_store import SessionStore

        config = ConfigStore().to_config()
        return SessionStore.from_config(config).load_session(session_id)
    except Exception:
        return None


def _agentshub_endpoint(source_session: dict[str, Any]) -> str:
    configured = os.environ.get("AGENTSHUB_REPLAY_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    runtime = (
        source_session.get("runtime")
        if isinstance(source_session.get("runtime"), dict)
        else {}
    )
    endpoint = str(runtime.get("replay_endpoint") or "").strip()
    if endpoint.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return endpoint.rstrip("/")
    try:
        from teamEvolver.config_store import ConfigStore

        endpoint = str(
            ConfigStore().to_config().validation_agentshub_url or ""
        ).strip()
    except Exception:
        endpoint = ""
    if endpoint.startswith(
        ("http://127.0.0.1", "http://localhost", "http://[::1]")
    ):
        return endpoint.rstrip("/")
    return ""


def spawn_agentshub_branch(
    branch: str,
    instruction: str,
    branch_skill: Optional[dict[str, Any]],
    job: dict[str, Any],
    case: dict[str, Any],
    source_session: dict[str, Any],
    timeout: int,
    max_interactions: int,
) -> dict[str, Any]:
    endpoint = _agentshub_endpoint(source_session)
    if not endpoint:
        return {
            "branch": branch,
            "runtime": "agentshub",
            "ok": False,
            "error": "AGENTSHUB_REPLAY_URL is not configured",
        }
    if not endpoint.endswith("/api/internal/team-evolver/replay"):
        endpoint = f"{endpoint}/api/internal/team-evolver/replay"
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("AGENTSHUB_REPLAY_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        import httpx

        response = httpx.post(
            endpoint,
            json={
                "branch": branch,
                "instruction": instruction,
                "target_skill_name": str(
                    (job.get("candidate_skill") or {}).get("name")
                    or job.get("candidate_skill_name")
                    or ""
                ),
                "skill": branch_skill,
                "current_skill": job.get("current_skill"),
                "source_session": source_session,
                "case": case,
                "timeout_seconds": max(30, int(timeout)),
                "max_interactions": max(1, int(max_interactions or 1)),
            },
            headers=headers,
            timeout=max(60, int(timeout) + 30),
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {
            "branch": branch,
            "runtime": "agentshub",
            "ok": False,
            "error": "AgentsHub replay returned a non-object response",
        }
    except Exception as exc:
        return {
            "branch": branch,
            "runtime": "agentshub",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def read_hermes_harness() -> dict[str, str]:
    """Mirror the user's real Hermes model harness (the replayed agent must be
    consistent with what the client runs). Reads ~/.hermes/config.yaml."""
    cfg_path = Path(os.path.expanduser("~/.hermes/config.yaml"))
    model: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            import yaml

            model = (yaml.safe_load(cfg_path.read_text("utf-8")) or {}).get("model", {}) or {}
        except Exception:
            model = {}
    return {
        "base_url": str(model.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")),
        "api_key": str(model.get("api_key") or os.getenv("OPENAI_API_KEY", "")),
        "model": str(model.get("default") or os.getenv("TEAMEVOLVER_REPLAY_MODEL", "doubao-seed-evolving")),
        "api_mode": str(model.get("api_mode") or ""),
        "max_tokens": int(model.get("max_tokens") or 8192),
    }


def read_team_evolver_harness() -> dict[str, Any]:
    """Use teamEvolver's configured judge for AgentsHub-native replay."""
    from teamEvolver.config_store import ConfigStore

    config = ConfigStore().to_config()
    return {
        "base_url": str(config.llm_api_base or ""),
        "api_key": str(config.llm_api_key or ""),
        "model": str(config.llm_model_id or config.model_name or ""),
        "api_mode": str(config.llm_api_mode or ""),
        "max_tokens": int(config.llm_max_tokens or 8192),
    }


# ---------------------------------------------------------------------------
# Path grounding: does this instruction reference real files on this machine?
# ---------------------------------------------------------------------------


def extract_referenced_paths(text: str) -> list[str]:
    """Pull filesystem-path-looking tokens out of a free-text instruction.

    Catches absolute paths (/home/...) and repo-relative hints (teamEvolver/...,
    integrations/...). Intentionally loose — grounding is advisory."""
    import re

    tokens = re.split(r"[\s,;，；、]+", text.strip())
    hits: list[str] = []
    for tok in tokens:
        tok = tok.strip().strip("`'\"")
        if not tok:
            continue
        looks_pathy = tok.startswith("/") or ("/" in tok and not tok.startswith("http"))
        if looks_pathy:
            hits.append(tok)
    return hits


def check_paths(paths: list[str], search_roots: list[Path]) -> list[dict[str, Any]]:
    """Resolve each referenced path (absolute, or relative to any search root)
    and report whether it exists on this machine."""
    out: list[dict[str, Any]] = []
    for p in paths:
        resolved: Optional[str] = None
        exists = False
        cand = Path(p)
        if cand.is_absolute():
            exists = cand.exists()
            resolved = str(cand) if exists else None
        else:
            for root in search_roots:
                probe = (root / p)
                if probe.exists():
                    exists, resolved = True, str(probe)
                    break
        out.append({"path": p, "exists": exists, "resolved": resolved})
    return out


def annotate_cases(job: dict[str, Any], search_roots: list[Path]) -> list[dict[str, Any]]:
    """Attach path-grounding to every replay case and flag which are runnable."""
    cases = []
    for idx, case in enumerate(job.get("replay_cases") or []):
        instr = str(case.get("instruction") or "").strip()
        refs = check_paths(extract_referenced_paths(instr), search_roots)
        referenced = [r for r in refs if r["exists"] or r["path"].startswith("/")]
        missing = [r for r in referenced if not r["exists"]]
        # Runnable when the instruction either references no path, or every
        # referenced path resolves on this machine.
        runnable = len(missing) == 0
        cases.append(
            {
                "index": idx,
                "session_id": case.get("session_id"),
                "turn_num": case.get("turn_num"),
                "instruction": instr,
                "evidence_window": str(
                    case.get("evidence_window") or "recent"
                ),
                "evaluation_profile": str(
                    case.get("evaluation_profile") or ""
                ),
                "had_tool_calls": bool(case.get("had_tool_calls")),
                "referenced_paths": referenced,
                "missing_paths": missing,
                "grounded": bool(referenced) and runnable,
                "runnable": runnable,
            }
        )
    return cases


# ---------------------------------------------------------------------------
# Sandbox construction (disposable HOME + HERMES_HOME per branch).
# ---------------------------------------------------------------------------


def build_sandbox(base: Path, branch: str, harness: dict[str, str],
                  skill: Optional[dict[str, Any]]) -> dict[str, str]:
    """Create an isolated HOME for one branch. ``branch`` is 'baseline' or
    'candidate'. The candidate branch also gets the skill installed under its
    private skills/ dir; both get a config.yaml mirroring the real harness."""
    home = base / branch
    hermes_home = home / ".hermes"
    workspace = home / "workspace"
    for d in (hermes_home / "skills", hermes_home / "sessions", hermes_home / "logs", workspace):
        d.mkdir(parents=True, exist_ok=True)

    # Minimal config.yaml so the sandboxed Hermes matches the client harness.
    config = {
        "model": {
            "provider": "custom",
            "base_url": harness["base_url"],
            "default": harness["model"],
            "api_key": harness["api_key"],
            "max_tokens": harness["max_tokens"],
            "api_mode": harness["api_mode"],
        }
    }
    try:
        import yaml

        (hermes_home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), "utf-8")
    except Exception:
        (hermes_home / "config.yaml").write_text(json.dumps(config), "utf-8")

    if skill:
        name = str(skill.get("name") or "candidate-skill")
        sk_dir = hermes_home / "skills" / name
        sk_dir.mkdir(parents=True, exist_ok=True)
        (sk_dir / "SKILL.md").write_text(str(skill.get("content") or ""), "utf-8")

    return {"home": str(home), "hermes_home": str(hermes_home), "workspace": str(workspace)}


# ---------------------------------------------------------------------------
# Worker: run ONE branch in an isolated subprocess and dump its trajectory.
# ---------------------------------------------------------------------------


def count_tool_calls(messages: list[dict[str, Any]]) -> int:
    return sum(
        len(message.get("tool_calls") or [])
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )


def _run_worker(spec_path: str) -> None:
    """Executed in a child process. Reads a spec JSON, sets the frozen env vars
    BEFORE importing hermes, runs one conversation, writes the trajectory out."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))

    # These must be set before importing any hermes module (import-time frozen).
    os.environ["HOME"] = spec["home"]
    os.environ["HERMES_HOME"] = spec["hermes_home"]
    os.environ["TERMINAL_ENV"] = "local"       # real tools on the host, no VM
    os.environ["HERMES_YOLO_MODE"] = "1"        # auto-approve, no TTY needed
    os.environ.pop("HERMES_INTERACTIVE", None)
    os.environ.pop("HERMES_GATEWAY_SESSION", None)
    os.chdir(spec["workspace"])                 # confine stray relative writes

    out: dict[str, Any] = {"branch": spec["branch"], "ok": False}
    t0 = time.time()
    try:
        # ``hermes_origin`` is a local checkout path to inject on sys.path, or
        # empty/absent to import an installed ``hermes-agent`` package as-is.
        origin = spec.get("hermes_origin")
        if origin:
            sys.path.insert(0, origin)
        try:
            from run_agent import AIAgent
        except ImportError as exc:
            raise ImportError(
                "Hermes agent runtime not found. Install it with "
                "`pip install 'teamEvolver[truereplay]'` or point HERMES_ORIGIN "
                "at a nousresearch/hermes-agent checkout. Original error: " + str(exc)
            ) from exc

        kwargs: dict[str, Any] = dict(
            base_url=spec["harness"]["base_url"],
            api_key=spec["harness"]["api_key"],
            model=spec["harness"]["model"],
            max_iterations=spec.get("max_iterations", 25),
            enabled_toolsets=["terminal", "file"],
            skip_context_files=True,
            skip_memory=True,
            save_trajectories=False,
            quiet_mode=True,
        )
        if spec.get("skill_content"):
            kwargs["ephemeral_system_prompt"] = (
                "You have access to the following installed skill. Follow its "
                "procedure when relevant:\n\n" + spec["skill_content"]
            )
        agent = AIAgent(**kwargs)
        original_instruction = str(spec["instruction"])
        current_prompt = original_instruction
        interactions: list[dict[str, Any]] = []
        result: dict[str, Any] = {}
        previous_total_tokens = 0
        previous_tool_calls = 0
        progress: dict[str, Any] = {}
        max_interactions = max(1, int(spec.get("max_interactions", 4) or 4))
        for interaction_num in range(1, max_interactions + 1):
            result = agent.run_conversation(
                current_prompt,
                task_id=f"replay_{spec['branch']}",
            )
            messages = result.get("messages") or []
            total_tokens = int(result.get("total_tokens") or 0)
            tool_call_count = count_tool_calls(messages)
            branch_snapshot = {
                "ok": True,
                "messages": messages,
                "final_response": result.get("final_response", ""),
            }
            progress = judge_branch(
                spec["harness"],
                original_instruction,
                branch_snapshot,
            )
            interactions.append(
                {
                    "interaction_num": interaction_num,
                    "prompt": current_prompt,
                    "response": str(result.get("final_response") or "")[:4000],
                    "tool_call_count": max(0, tool_call_count - previous_tool_calls),
                    "total_tokens": max(0, total_tokens - previous_total_tokens),
                    "completed": bool(progress.get("success")),
                    "judge": progress,
                }
            )
            previous_total_tokens = total_tokens
            previous_tool_calls = tool_call_count
            if progress.get("success"):
                break
            feedback = str(progress.get("feedback") or progress.get("rationale") or "").strip()
            current_prompt = feedback or (
                "上一轮尚未完整达成任务目标，请检查现有结果并继续完成，不要重复已经成功的步骤。"
            )
        out.update(
            ok=True,
            final_response=result.get("final_response", ""),
            messages=result.get("messages", []),
            api_calls=result.get("api_calls"),
            completed=result.get("completed"),
            interaction_turns=len(interactions),
            tool_call_count=count_tool_calls(result.get("messages") or []),
            input_tokens=int(result.get("input_tokens") or result.get("prompt_tokens") or 0),
            output_tokens=int(result.get("output_tokens") or result.get("completion_tokens") or 0),
            cache_read_tokens=int(result.get("cache_read_tokens") or 0),
            cache_write_tokens=int(result.get("cache_write_tokens") or 0),
            reasoning_tokens=int(result.get("reasoning_tokens") or 0),
            total_tokens=int(result.get("total_tokens") or 0),
            interactions=interactions,
            progress_judge=progress,
        )
    except Exception as e:  # noqa: BLE001 — surface any failure to the parent
        import traceback

        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()
    finally:
        out["elapsed_seconds"] = round(time.time() - t0, 1)
        Path(spec["out_path"]).write_text(json.dumps(out, ensure_ascii=False), "utf-8")


def spawn_branch(branch: str, sandbox: dict[str, str], instruction: str,
                 harness: dict[str, str], skill: Optional[dict[str, Any]],
                 tmp: Path, timeout: int, max_interactions: int = 4) -> dict[str, Any]:
    """Spawn a worker subprocess for one branch and collect its trajectory."""
    worker_python = os.environ.get("TEAMEVOLVER_REPLAY_PYTHON", "").strip() or sys.executable
    spec = {
        "branch": branch,
        "home": sandbox["home"],
        "hermes_home": sandbox["hermes_home"],
        "workspace": sandbox["workspace"],
        # A local checkout to inject on sys.path, or "" to import the installed
        # hermes-agent package. Resolved once here so both branches agree.
        "hermes_origin": resolve_hermes_origin() or "",
        "instruction": instruction,
        "harness": harness,
        "skill_content": (skill or {}).get("content"),
        "max_iterations": 25,
        "max_interactions": max(1, int(max_interactions or 4)),
        "out_path": str(tmp / f"{branch}_out.json"),
    }
    spec_path = tmp / f"{branch}_spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), "utf-8")
    os.chmod(spec_path, 0o600)  # spec carries the api_key

    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT)}
    print(f"  ▶ running {branch} branch (real tool loop, timeout {timeout}s)…", flush=True)
    try:
        subprocess.run(
            [worker_python, "-m", "teamEvolver.true_replay",
             "--worker", "--spec", str(spec_path)],
            cwd=str(_REPO_ROOT), env=env, timeout=timeout,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        return {"branch": branch, "ok": False, "error": f"timeout after {timeout}s"}
    out_file = tmp / f"{branch}_out.json"
    if not out_file.exists():
        return {"branch": branch, "ok": False, "error": "worker produced no output"}
    return json.loads(out_file.read_text("utf-8"))


# ---------------------------------------------------------------------------
# Trajectory rendering + LLM judge (trajectory-aware, not text-only).
# ---------------------------------------------------------------------------


def render_trajectory(messages: list[dict[str, Any]]) -> str:
    """Render an OpenAI-format message list into a numbered tool-call trace,
    the same evidence style agent_evolve_evaluation feeds its trajectory judge."""
    lines: list[str] = []
    step = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in (m.get("tool_calls") or []):
                step += 1
                fn = (tc.get("function") or {})
                args = fn.get("arguments")
                if isinstance(args, str) and len(args) > 600:
                    args = args[:600] + "…"
                lines.append(f"[step {step}] call {fn.get('name')}({args})")
        elif role == "tool":
            content = m.get("content")
            if isinstance(content, str) and len(content) > 500:
                content = content[:500] + "…"
            lines.append(f"        ↳ result: {content}")
    return "\n".join(lines) if lines else "(no tool calls were made)"


def judge_branch(
    harness: dict[str, str],
    instruction: str,
    branch: dict[str, Any],
    checklist: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """LLM-as-judge over the *trajectory*: did the task actually get done, were
    the tools used correctly? Returns {overall, task_completion, tool_correctness,
    rationale}. Scores in [0,1]."""
    if not branch.get("ok"):
        return {
            "success": False,
            "overall": 0.0,
            "task_completion": 0.0,
            "tool_correctness": 0.0,
            "feedback": "",
            "rationale": f"branch failed: {branch.get('error')}",
        }
    gap_report = (
        branch.get("artifact_gap_report")
        if isinstance(branch.get("artifact_gap_report"), dict)
        else {}
    )
    if gap_report and not gap_report.get("passed"):
        failed = [
            str(item)
            for item in gap_report.get("failed_checks") or []
            if item
        ]
        return {
            "success": False,
            "overall": 0.0,
            "task_completion": 0.0,
            "tool_correctness": 0.5 if branch.get("artifacts") else 0.0,
            "feedback": failed[0][:800] if failed else "",
            "rationale": (
                "real artifact did not pass methodology checks: "
                + "; ".join(failed[:6])
            )[:800],
        }

    trace = render_trajectory(branch.get("messages") or [])
    final = str(branch.get("final_response") or "")[:1500]
    soft_items = [
        {
            "id": str(item.get("id") or ""),
            "claim": str(item.get("claim") or ""),
        }
        for item in (checklist or {}).get("items") or []
        if isinstance(item, dict) and item.get("kind") == "soft"
    ]
    sys_prompt = (
        "You are a strict evaluator of an AI agent's execution TRACE. You are "
        "given a user instruction, the agent's tool-call sequence (with results), "
        "and its final answer. Judge whether the task was ACTUALLY accomplished "
        "via the tools (files really created/copied, config really edited, etc.), "
        "not merely described. Score three numbers in [0,1]:\n"
        "- task_completion: was the concrete goal achieved end-to-end?\n"
        "- tool_correctness: were the right tools called with correct arguments, "
        "and did they succeed (vs error/no-op)?\n"
        "- overall: advisory holistic quality; it is NOT the release score.\n"
        "- success: true only when the concrete task is complete enough that no "
        "further user interaction is needed.\n"
        "- checklist_results: pass/fail each supplied SOFT checklist item. Do "
        "not evaluate hard items; code handles them.\n"
        "- feedback: if incomplete, give a concise next-turn instruction that "
        "helps the same agent finish without revealing hidden grading rubrics.\n"
        'Reply ONLY as JSON: {"success":true|false,"task_completion":..,'
        '"tool_correctness":..,"overall":..,"checklist_results":'
        '[{"id":"...","passed":true|false,"reason":"..."}],'
        '"feedback":"..","rationale":".."}'
    )
    user_prompt = (
        f"[Instruction]\n{instruction}\n\n[Tool-call trace]\n{trace}\n\n"
        f"[Final answer]\n{final}\n\n[Soft checklist]\n"
        f"{json.dumps(soft_items, ensure_ascii=False, indent=2)}"
    )
    try:
        from openai import OpenAI

        client = OpenAI(base_url=harness["base_url"], api_key=harness["api_key"])
        resp = client.chat.completions.create(
            model=harness["model"],
            messages=[{"role": "system", "content": sys_prompt},
                      {"role": "user", "content": user_prompt}],
            temperature=0,
        )
        message = resp.choices[0].message
        raw = getattr(message, "content", None) or getattr(message, "reasoning_content", None) or "{}"
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 else {}
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "overall": 0.0,
            "task_completion": 0.0,
            "tool_correctness": 0.0,
            "feedback": "",
            "rationale": f"judge error: {type(e).__name__}: {e}",
        }

    def _num(x: Any) -> float:
        try:
            return max(0.0, min(1.0, float(x)))
        except Exception:
            return 0.0

    checklist_results: dict[str, bool] = {}
    checklist_reasons: dict[str, str] = {}
    raw_checklist = data.get("checklist_results")
    if isinstance(raw_checklist, list):
        for item in raw_checklist:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if item_id:
                checklist_results[item_id] = bool(item.get("passed"))
                checklist_reasons[item_id] = str(item.get("reason") or "")[:500]
    return {
        "success": bool(data.get("success")) and _num(data.get("task_completion")) >= 0.75,
        "overall": _num(data.get("overall")),
        "task_completion": _num(data.get("task_completion")),
        "tool_correctness": _num(data.get("tool_correctness")),
        "feedback": str(data.get("feedback") or "")[:800],
        "rationale": str(data.get("rationale") or "")[:800],
        "checklist_results": checklist_results,
        "checklist_reasons": checklist_reasons,
    }


def branch_efficiency(branch: dict[str, Any]) -> dict[str, int]:
    return {
        "interaction_turns": int(branch.get("interaction_turns") or 0),
        "tool_call_count": int(
            branch.get("tool_call_count")
            or count_tool_calls(branch.get("messages") or [])
        ),
        "total_tokens": int(branch.get("total_tokens") or 0),
        "input_tokens": int(branch.get("input_tokens") or 0),
        "output_tokens": int(branch.get("output_tokens") or 0),
        "cache_read_tokens": int(branch.get("cache_read_tokens") or 0),
        "cache_write_tokens": int(branch.get("cache_write_tokens") or 0),
        "reasoning_tokens": int(branch.get("reasoning_tokens") or 0),
    }


def compare_efficiency(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    base = branch_efficiency(baseline)
    cand = branch_efficiency(candidate)
    dimensions: dict[str, dict[str, Any]] = {}
    weighted_gains: list[float] = []
    for key, weight in _EFFICIENCY_WEIGHTS.items():
        baseline_value = int(base[key])
        candidate_value = int(cand[key])
        delta = baseline_value - candidate_value
        gain = max(-1.0, min(1.0, delta / max(1, baseline_value)))
        weighted_gains.append(gain * weight)
        dimensions[key] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "reduction_ratio": round(gain, 4),
            "weight": weight,
            "weighted_gain": round(gain * weight, 4),
            "winner": "candidate" if delta > 0 else ("baseline" if delta < 0 else "tie"),
        }
    score = sum(weighted_gains)
    return {
        "baseline": base,
        "candidate": cand,
        "dimensions": dimensions,
        "score": round(score, 4),
        "weights": dict(_EFFICIENCY_WEIGHTS),
        "improved_dimensions": [
            key for key, value in dimensions.items() if value["winner"] == "candidate"
        ],
        "regressed_dimensions": [
            key for key, value in dimensions.items() if value["winner"] == "baseline"
        ],
    }


# ---------------------------------------------------------------------------
# Orchestration + CLI.
# ---------------------------------------------------------------------------


def _print_case_table(cases: list[dict[str, Any]]) -> None:
    print("\n候选回放案例（含真实路径落地判定）:")
    for c in cases:
        flag = "✅ 可真回放" if c["runnable"] else "⛔ 缺文件, 跳过"
        paths = ", ".join(
            f"{p['path']}{'✓' if p['exists'] else '✗'}" for p in c["referenced_paths"]
        ) or "(无路径引用)"
        print(f"  [{c['index']}] {flag} | tool_calls={c['had_tool_calls']} | 路径: {paths}")
        print(f"       指令: {c['instruction'][:90]}")


def _evaluate_agentshub_case(
    job_id: str,
    job: dict[str, Any],
    case: dict[str, Any],
    source_session: dict[str, Any],
    harness: dict[str, str],
    *,
    timeout: int,
    min_score: float,
    tolerance: float,
    max_interactions: int,
) -> dict[str, Any]:
    harness = read_team_evolver_harness()
    branch_skills = {
        "baseline": (
            job.get("current_skill")
            if isinstance(job.get("current_skill"), dict)
            else None
        ),
        "candidate": job.get("candidate_skill") or {},
    }
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(
                spawn_agentshub_branch,
                branch,
                case["instruction"],
                branch_skills[branch],
                job,
                case,
                source_session,
                timeout,
                max_interactions,
            ): branch
            for branch in ("baseline", "candidate")
        }
        for future in as_completed(futures):
            branch = futures[future]
            try:
                results[branch] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[branch] = {
                    "branch": branch,
                    "runtime": "agentshub",
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    failures = [
        f"{branch}: {result.get('error') or 'branch failed'}"
        for branch, result in results.items()
        if not result.get("ok")
    ]
    efficiency = compare_efficiency(results["baseline"], results["candidate"])
    checklist = compile_common_checklist(job)
    scoped_checklist = scope_checklist_for_case(checklist, case)
    checklist_results: dict[str, dict[str, Any]] = {}

    def branch_case(branch: str, judged: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        result = results[branch]
        score = (judged or {}).get(branch, {})
        return {
            "session_id": str(case.get("session_id") or ""),
            "turn_num": int(case.get("turn_num") or 0),
            "instruction": case["instruction"],
            "score": checklist_results.get(branch, {}).get(
                "pass_rate",
                score.get("overall"),
            ),
            "advisory_judge_score": score.get("overall"),
            "task_completion": score.get("task_completion"),
            "tool_correctness": score.get("tool_correctness"),
            "rationale": score.get("rationale") or result.get("error"),
            "trajectory": render_trajectory(result.get("messages") or []),
            "final_response": str(result.get("final_response") or "")[:2000],
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "elapsed_seconds": result.get("elapsed_seconds"),
            "api_calls": result.get("api_calls"),
            "interaction_turns": result.get("interaction_turns"),
            "tool_call_count": result.get("tool_call_count"),
            "total_tokens": result.get("total_tokens"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cache_read_tokens": result.get("cache_read_tokens"),
            "cache_write_tokens": result.get("cache_write_tokens"),
            "reasoning_tokens": result.get("reasoning_tokens"),
            "interactions": result.get("interactions") or [],
            "artifacts": result.get("artifacts") or [],
            "artifact_gap_report": result.get("artifact_gap_report") or {},
            "checklist": checklist_results.get(branch),
        }

    common = {
        "mode": "true_replay",
        "runtime": "agentshub",
        "job_id": job_id,
        "threshold": round(float(min_score), 3),
        "tolerance": round(float(tolerance), 3),
        "max_interactions": max(1, int(max_interactions or 1)),
        "case_count": 1,
        "case": {
            "index": case["index"],
            "grounded": True,
            "referenced_paths": case.get("referenced_paths"),
        },
        "harness": {
            "model": harness.get("model"),
            "base_url": harness.get("base_url"),
        },
        "efficiency": efficiency,
        "checklist": checklist,
        "scoped_checklist": scoped_checklist,
    }
    if failures:
        return {
            **common,
            "status": "failed",
            "accepted": False,
            "no_regression": False,
            "score": None,
            "baseline_mean": None,
            "delta": None,
            "quality_ok": False,
            "reason": "AgentsHub true replay branch failed: " + "; ".join(failures),
            "cases": [
                {
                    "baseline": branch_case("baseline"),
                    "candidate": branch_case("candidate"),
                }
            ],
        }

    judged = {
        branch: judge_branch(
            harness,
            case["instruction"],
            results[branch],
            scoped_checklist,
        )
        for branch in ("baseline", "candidate")
    }
    checklist_results = {
        branch: evaluate_branch_checklist(
            scoped_checklist,
            results[branch],
            judged[branch].get("checklist_results"),
        )
        for branch in ("baseline", "candidate")
    }
    policy = objective_replay_decision(
        checklist=scoped_checklist,
        baseline=checklist_results["baseline"],
        candidate=checklist_results["candidate"],
        efficiency=efficiency,
    )
    baseline_score = float(checklist_results["baseline"]["pass_rate"])
    candidate_score = float(checklist_results["candidate"]["pass_rate"])
    delta = round(candidate_score - baseline_score, 3)
    no_regression = bool(policy["no_regression"])
    quality_ok = bool(policy["quality_gate"])
    accepted = bool(policy["accepted"])
    return {
        **common,
        "status": "evaluated",
        "accepted": accepted,
        "no_regression": no_regression,
        "score": round(candidate_score, 3),
        "baseline_mean": round(baseline_score, 3),
        "delta": delta,
        "quality_ok": quality_ok,
        "decision_policy": policy,
        "checklist_results": checklist_results,
        "cases": [
            {
                "baseline": branch_case("baseline", judged),
                "candidate": branch_case("candidate", judged),
            }
        ],
    }


def evaluate_job(
    job_id: str,
    *,
    job: Optional[dict[str, Any]] = None,
    case_index: Optional[int] = None,
    timeout: int = 600,
    min_score: float = 0.75,
    tolerance: float = 0.15,
    keep_sandbox: bool = False,
    max_interactions: int = 4,
) -> dict[str, Any]:
    """Run a true replay for one candidate and return a structured verdict.

    The return shape mirrors the orchestrator's text ``_run_candidate_replay``
    (``score``/``baseline_mean``/``no_regression``/``accepted``/``cases``) so it
    is a drop-in replacement, with extra true-replay fields (per-branch tool
    trajectories, judge rationales, path-grounding). ``score`` is the candidate
    branch's ``overall``; ``baseline_mean`` is the baseline branch's ``overall``.

    A candidate whose referenced source paths are missing on this machine yields
    ``status="skipped"`` (nothing runnable) rather than a misleading 0.0.
    """
    import shutil

    job = job or load_candidate_job(job_id)
    if job is None:
        return {"status": "not_found", "job_id": job_id}
    skill = job.get("candidate_skill") or {}
    harness = read_hermes_harness()
    search_roots = [_REPO_ROOT, Path(os.path.expanduser("~"))]
    cases = annotate_cases(job, search_roots)

    requested_cases = [
        case
        for case in cases
        if case["instruction"]
        and (case_index is None or case["index"] == case_index)
    ]
    for source_case in requested_cases:
        source_session = (
            load_source_session(str(source_case.get("session_id") or "")) or {}
        )
        runtime = (
            source_session.get("runtime")
            if isinstance(source_session.get("runtime"), dict)
            else {}
        )
        if (
            os.environ.get("AGENTSHUB_REPLAY_URL", "").strip()
            or str(
                runtime.get("type") or source_session.get("source") or ""
            )
            == "agentshub"
        ):
            if not runtime:
                source_session["runtime"] = {
                    "type": "agentshub",
                    "replay_endpoint": os.environ.get(
                        "AGENTSHUB_REPLAY_URL",
                        "",
                    ).strip(),
                }
            return _evaluate_agentshub_case(
                job_id,
                job,
                source_case,
                source_session,
                harness,
                timeout=timeout,
                min_score=min_score,
                tolerance=tolerance,
                max_interactions=max_interactions,
            )

    runnable = [c for c in cases if c["runnable"] and c["instruction"]]
    if not runnable:
        return {
            "status": "skipped",
            "job_id": job_id,
            "reason": "no runnable case (referenced source paths missing on this host)",
            "mode": "true_replay",
            "cases": [],
        }

    if case_index is not None:
        chosen = next((c for c in cases if c["index"] == case_index and c["runnable"]), None)
        if chosen is None:
            return {"status": "skipped", "job_id": job_id,
                    "reason": f"case {case_index} not runnable", "mode": "true_replay"}
    else:
        chosen = next((c for c in runnable if c["grounded"]), runnable[0])

    tmp = Path(tempfile.mkdtemp(prefix="true_replay_"))
    try:
        # Build both sandboxes first, then run the two branches concurrently:
        # each branch is an isolated subprocess with its own HOME/HERMES_HOME, so
        # they don't share state and can run in parallel to halve wall-clock time.
        branch_skills = {
            "baseline": (
                job.get("current_skill")
                if isinstance(job.get("current_skill"), dict)
                else None
            ),
            "candidate": skill,
        }
        sandboxes = {
            branch: build_sandbox(tmp, branch, harness, branch_skills[branch])
            for branch in ("baseline", "candidate")
        }
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                pool.submit(
                    spawn_branch,
                    branch,
                    sandboxes[branch],
                    chosen["instruction"],
                    harness,
                    branch_skills[branch],
                    tmp,
                    timeout,
                    max_interactions=max_interactions,
                ): branch
                for branch in ("baseline", "candidate")
            }
            for fut in as_completed(futures):
                branch = futures[fut]
                try:
                    results[branch] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    results[branch] = {
                        "branch": branch,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
        branch_failures = [
            f"{branch}: {result.get('error') or 'branch failed'}"
            for branch, result in results.items()
            if not result.get("ok")
        ]
        if branch_failures:
            efficiency = compare_efficiency(results["baseline"], results["candidate"])

            def _failed_branch_case(branch: str) -> dict[str, Any]:
                r = results[branch]
                return {
                    "session_id": str(chosen.get("session_id", "") or ""),
                    "turn_num": int(chosen.get("turn_num", 0) or 0),
                    "instruction": chosen["instruction"],
                    "score": None,
                    "task_completion": None,
                    "tool_correctness": None,
                    "rationale": f"branch failed: {r.get('error') or 'unknown error'}",
                    "trajectory": render_trajectory(r.get("messages") or []),
                    "final_response": str(r.get("final_response") or "")[:2000],
                    "ok": bool(r.get("ok")),
                    "error": r.get("error") or "branch failed",
                    "elapsed_seconds": r.get("elapsed_seconds"),
                    "api_calls": r.get("api_calls"),
                    "interaction_turns": r.get("interaction_turns"),
                    "tool_call_count": r.get("tool_call_count"),
                    "total_tokens": r.get("total_tokens"),
                    "input_tokens": r.get("input_tokens"),
                    "output_tokens": r.get("output_tokens"),
                    "cache_read_tokens": r.get("cache_read_tokens"),
                    "cache_write_tokens": r.get("cache_write_tokens"),
                    "reasoning_tokens": r.get("reasoning_tokens"),
                    "interactions": r.get("interactions") or [],
                    "artifacts": r.get("artifacts") or [],
                    "artifact_gap_report": r.get("artifact_gap_report") or {},
                }

            return {
                "status": "failed",
                "mode": "true_replay",
                "job_id": job_id,
                "accepted": False,
                "no_regression": False,
                "score": None,
                "baseline_mean": None,
                "delta": None,
                "quality_ok": False,
                "efficiency": efficiency,
                "threshold": round(float(min_score), 3),
                "tolerance": round(float(tolerance), 3),
                "max_interactions": max(1, int(max_interactions or 4)),
                "case_count": 1,
                "reason": "true replay branch failed: " + "; ".join(branch_failures),
                "case": {
                    "index": chosen["index"],
                    "grounded": chosen["grounded"],
                    "referenced_paths": chosen.get("referenced_paths"),
                },
                "harness": {"model": harness.get("model"), "base_url": harness.get("base_url")},
                "cases": [{"baseline": _failed_branch_case("baseline"), "candidate": _failed_branch_case("candidate")}],
            }
        checklist = compile_common_checklist(job)
        scoped_checklist = scope_checklist_for_case(checklist, chosen)
        judged = {
            branch: judge_branch(
                harness,
                chosen["instruction"],
                results[branch],
                scoped_checklist,
            )
            for branch in ("baseline", "candidate")
        }
        efficiency = compare_efficiency(results["baseline"], results["candidate"])
        checklist_results = {
            branch: evaluate_branch_checklist(
                scoped_checklist,
                results[branch],
                judged[branch].get("checklist_results"),
            )
            for branch in ("baseline", "candidate")
        }
        policy = objective_replay_decision(
            checklist=scoped_checklist,
            baseline=checklist_results["baseline"],
            candidate=checklist_results["candidate"],
            efficiency=efficiency,
        )
        baseline_overall = float(checklist_results["baseline"]["pass_rate"])
        candidate_overall = float(checklist_results["candidate"]["pass_rate"])
        delta = round(candidate_overall - baseline_overall, 3)
        no_regression = bool(policy["no_regression"])
        quality_ok = bool(policy["quality_gate"])
        accepted = bool(policy["accepted"])

        def _branch_case(branch: str) -> dict[str, Any]:
            r = results[branch]
            j = judged[branch]
            return {
                "session_id": str(chosen.get("session_id", "") or ""),
                "turn_num": int(chosen.get("turn_num", 0) or 0),
                "instruction": chosen["instruction"],
                "score": checklist_results[branch]["pass_rate"],
                "advisory_judge_score": j["overall"],
                "task_completion": j["task_completion"],
                "tool_correctness": j["tool_correctness"],
                "rationale": j["rationale"],
                "trajectory": render_trajectory(r.get("messages") or []),
                "final_response": str(r.get("final_response") or "")[:2000],
                "ok": bool(r.get("ok")),
                "error": r.get("error"),
                "elapsed_seconds": r.get("elapsed_seconds"),
                "api_calls": r.get("api_calls"),
                "interaction_turns": r.get("interaction_turns"),
                "tool_call_count": r.get("tool_call_count"),
                "total_tokens": r.get("total_tokens"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "cache_read_tokens": r.get("cache_read_tokens"),
                "cache_write_tokens": r.get("cache_write_tokens"),
                "reasoning_tokens": r.get("reasoning_tokens"),
                "interactions": r.get("interactions") or [],
                "artifacts": r.get("artifacts") or [],
                "artifact_gap_report": r.get("artifact_gap_report") or {},
                "checklist": checklist_results[branch],
            }

        return {
            "status": "evaluated",
            "mode": "true_replay",
            "job_id": job_id,
            "accepted": accepted,
            "no_regression": no_regression,
            "score": round(candidate_overall, 3),
            "baseline_mean": round(baseline_overall, 3),
            "delta": delta,
            "quality_ok": quality_ok,
            "checklist": checklist,
            "scoped_checklist": scoped_checklist,
            "checklist_results": checklist_results,
            "decision_policy": policy,
            "efficiency": efficiency,
            "threshold": round(float(min_score), 3),
            "tolerance": round(float(tolerance), 3),
            "max_interactions": max(1, int(max_interactions or 4)),
            "case_count": 1,
            "case": {
                "index": chosen["index"],
                "grounded": chosen["grounded"],
                "referenced_paths": chosen.get("referenced_paths"),
            },
            "harness": {"model": harness.get("model"), "base_url": harness.get("base_url")},
            "cases": [{"baseline": _branch_case("baseline"), "candidate": _branch_case("candidate")}],
        }
    finally:
        if not keep_sandbox:
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


def run(
    job_id: str,
    case_index: Optional[int],
    dry_run: bool,
    timeout: int,
    max_interactions: int,
    *,
    job: Optional[dict[str, Any]] = None,
) -> None:
    job = job or load_candidate_job(job_id)
    if job is None:
        print(f"job not found: {job_id}")
        return
    skill = job.get("candidate_skill") or {}
    harness = read_hermes_harness()
    search_roots = [_REPO_ROOT, Path(os.path.expanduser("~"))]
    cases = annotate_cases(job, search_roots)

    print(f"候选技能: {skill.get('name')}  | 动作: {job.get('proposed_action')} | "
          f"基线技能: {'无(新建)' if job.get('current_skill') is None else '有'}")
    print(f"回放 harness (对齐客户端 Hermes): model={harness['model']} @ {harness['base_url']}")
    _print_case_table(cases)

    runnable = [c for c in cases if c["runnable"] and c["instruction"]]
    if not runnable:
        print("\n没有可真回放的案例（引用的真实文件在本机不存在）。")
        return

    # Prefer a grounded case (references a real path) — that's where true replay
    # beats text replay most. Fall back to the first runnable one.
    if case_index is not None:
        chosen = next((c for c in cases if c["index"] == case_index), None)
        if chosen is None or not chosen["runnable"]:
            print(f"\n案例 {case_index} 不可回放。")
            return
    else:
        chosen = next((c for c in runnable if c["grounded"]), runnable[0])

    print(f"\n选定案例 [{chosen['index']}]: {chosen['instruction']}")
    if dry_run:
        print("(--dry-run：仅做案例与路径落地判定，不实际运行 agent。)")
        return

    tmp = Path(tempfile.mkdtemp(prefix="true_replay_"))
    print(f"沙盒根目录: {tmp}  (HOME 与 HERMES_HOME 均隔离到此，真实 ~/.hermes 不受影响)")
    try:
        results: dict[str, dict[str, Any]] = {}
        for branch in ("baseline", "candidate"):
            sandbox = build_sandbox(tmp, branch, harness, skill)
            results[branch] = spawn_branch(
                branch,
                sandbox,
                chosen["instruction"],
                harness,
                skill,
                tmp,
                timeout,
                max_interactions=max_interactions,
            )

        print("\n===== 双分支执行结果 =====")
        judged: dict[str, dict[str, Any]] = {}
        for branch in ("baseline", "candidate"):
            r = results[branch]
            label = "🅰 基线(无技能)" if branch == "baseline" else "🅱 候选(注入技能)"
            print(f"\n{label}: ok={r.get('ok')} elapsed={r.get('elapsed_seconds')}s "
                  f"interactions={r.get('interaction_turns')} "
                  f"tools={r.get('tool_call_count')} tokens={r.get('total_tokens')} "
                  f"completed={r.get('completed')}")
            if not r.get("ok"):
                print(f"   error: {r.get('error')}")
            else:
                print("   工具轨迹:")
                print("   " + render_trajectory(r.get("messages") or []).replace("\n", "\n   "))
                print(f"   最终回答: {str(r.get('final_response') or '')[:400]}")
            judged[branch] = judge_branch(harness, chosen["instruction"], r)

        print("\n===== 裁判打分（trajectory-aware） =====")
        for branch in ("baseline", "candidate"):
            j = judged[branch]
            print(f"  {branch:9s}: overall={j['overall']:.3f}  "
                  f"task_completion={j['task_completion']:.3f}  "
                  f"tool_correctness={j['tool_correctness']:.3f}")
            print(f"             理由: {j['rationale']}")

        b, c = judged["baseline"]["overall"], judged["candidate"]["overall"]
        delta = c - b
        efficiency = compare_efficiency(results["baseline"], results["candidate"])
        print(f"\n===== 分差 =====\n  候选 - 基线 = {c:.3f} - {b:.3f} = {delta:+.3f}  "
              f"→ {'候选更优' if delta > 0.001 else ('基线更优' if delta < -0.001 else '持平')}")
        print("\n===== 效率对比（正数表示候选减少） =====")
        for key, metric in efficiency["dimensions"].items():
            print(
                f"  {key}: baseline={metric['baseline']} candidate={metric['candidate']} "
                f"delta={metric['delta']:+d} reduction={metric['reduction_ratio']:+.1%}"
            )

        artifact = tmp / "true_replay_result.json"
        artifact.write_text(json.dumps(
            {"job_id": job_id, "case": chosen, "harness": harness,
             "results": results, "judged": judged, "delta": delta, "efficiency": efficiency},
            ensure_ascii=False, indent=2), "utf-8")
        print(f"\n完整结果已存档: {artifact}")
    finally:
        pass  # keep sandbox for inspection; caller cleans /tmp when done


def main() -> None:
    ap = argparse.ArgumentParser(description="True A/B replay prototype")
    ap.add_argument("--worker", action="store_true", help="internal: run one branch")
    ap.add_argument("--spec", help="internal: worker spec json path")
    ap.add_argument("--job-id", help="validation job id to replay")
    ap.add_argument("--job-file", help="standalone validation job JSON file")
    ap.add_argument("--case", type=int, default=None, help="replay case index (default: auto)")
    ap.add_argument("--dry-run", action="store_true", help="only resolve cases + check paths")
    ap.add_argument("--timeout", type=int, default=600, help="per-branch timeout seconds")
    ap.add_argument(
        "--max-interactions",
        type=int,
        default=4,
        help="maximum user/agent interactions per branch (default: 4)",
    )
    ap.add_argument("--json", action="store_true",
                    help="emit a single structured JSON verdict on stdout (for programmatic callers)")
    ap.add_argument("--min-score", type=float, default=0.75, help="acceptance threshold (--json)")
    ap.add_argument("--tolerance", type=float, default=0.15, help="no-regression tolerance (--json)")
    args = ap.parse_args()

    if args.worker:
        if not args.spec:
            raise SystemExit("--worker requires --spec")
        _run_worker(args.spec)
        return
    if not args.job_id and not args.job_file:
        raise SystemExit("--job-id or --job-file is required")
    loaded_job = load_candidate_job_file(args.job_file) if args.job_file else None
    job_id = args.job_id or str((loaded_job or {}).get("job_id") or Path(args.job_file).stem)
    if args.json:
        verdict = evaluate_job(
            job_id,
            job=loaded_job,
            case_index=args.case,
            timeout=args.timeout,
            min_score=args.min_score,
            tolerance=args.tolerance,
            max_interactions=args.max_interactions,
        )
        # Frame the payload so a caller can extract it even if worker subprocesses
        # print incidental lines to stdout.
        print("TRUE_REPLAY_JSON_BEGIN")
        print(json.dumps(verdict, ensure_ascii=False))
        print("TRUE_REPLAY_JSON_END")
        return
    run(
        job_id,
        args.case,
        args.dry_run,
        args.timeout,
        args.max_interactions,
        job=loaded_job,
    )


if __name__ == "__main__":
    main()
