"""White-box settings for the vendored SkillMiner product."""

from __future__ import annotations

import ast
import importlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SKILLMINER_ROOT = Path(__file__).resolve().parent / "skillminer"

_PROMPTS: dict[str, dict[str, Any]] = {
    "sample_package": {
        "label": "样本包构建",
        "description": "把原始知识文档切分为可追溯、可校验的样本包。",
        "file": "sample_package_constructor_agent_prompt.py",
        "symbol": "SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT",
    },
    "semantic_discovery": {
        "label": "语义发现",
        "description": "从单个样本包挖掘决策单元、边界、冲突和知识缺口。",
        "file": "semantic_discovery_agent_prompt.py",
        "symbol": "SEMANTIC_DISCOVERY_AGENT_PROMPT",
    },
    "evaluation_compiler": {
        "label": "Skill 与评测编译",
        "description": "把语义报告编译为配套的 SKILL.md 与 EVALUATION.md。",
        "file": "evaluation_compiler_agent_prompt.py",
        "symbol": "EVALUATION_COMPILER_AGENT_PROMPT",
    },
    "benchmark_generation": {
        "label": "Benchmark 出题",
        "description": "根据 SKILL.md 与 EVALUATION.md 生成结构化内部题库。",
        "dynamic": "benchmark_generation",
    },
    "benchmark_usage": {
        "label": "Benchmark 单轮作答",
        "description": "单轮模式下交给被测 Skill 的业务情境 Prompt。",
        "dynamic": "benchmark_usage",
    },
    "benchmark_participant": {
        "label": "Benchmark 模拟参与者",
        "description": "多轮模式下控制模拟参与者逐步披露事实并施压。",
        "dynamic": "benchmark_participant",
    },
    "benchmark_skill_reply": {
        "label": "Benchmark 被测 Skill 回复",
        "description": "多轮模式下交给被测 Skill 的当前对话 Prompt。",
        "dynamic": "benchmark_skill_reply",
    },
    "benchmark_judge_single": {
        "label": "Benchmark 单轮裁判",
        "description": "依据 EVALUATION 与 gold 对单轮答案给出机器可读裁决。",
        "dynamic": "benchmark_judge_single",
    },
    "benchmark_judge_dialogue": {
        "label": "Benchmark 多轮裁判",
        "description": "对完整多轮对话、主动追问与情绪应对进行裁决。",
        "dynamic": "benchmark_judge_dialogue",
    },
    "trajectory_benchmark_generation": {
        "label": "轨迹 Benchmark 挖掘",
        "description": "从脱敏成功/失败轨迹挖掘 held-out Benchmark。",
        "dynamic": "trajectory_benchmark_generation",
    },
}

_PIPELINE_DEFAULTS: dict[str, Any] = {
    "max_rounds": 3,
    "max_retries": 2,
    "retry_backoff_seconds": 0.8,
    "oneshot_timeout_seconds": 1800,
    "step1_validation_retries": 1,
    "strict_step1": True,
    "benchmark_target_total": 16,
    "benchmark_difficulty_dist": "easy:4,medium:7,hard:5",
    "benchmark_max_turns": 5,
}


_DYNAMIC_PROMPT_LOCK = threading.Lock()


def _skillminer_module(name: str):
    root = str(_SKILLMINER_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


def _dynamic_default(kind: str) -> str:
    with _DYNAMIC_PROMPT_LOCK:
        previous = os.environ.get("SKILLMINER_DISABLE_PROMPT_OVERRIDES")
        os.environ["SKILLMINER_DISABLE_PROMPT_OVERRIDES"] = "1"
        try:
            rb = _skillminer_module("run_benchmark")
            question = {
                "input": "{{question_input}}",
                "target_dimensions": ["{{dimensions}}"],
                "gold": {
                    "expected_label": {"label": "{{expected_labels}}"},
                    "must_hit": ["{{must_hit}}"],
                    "must_avoid": ["{{must_avoid}}"],
                },
                "customer_sim": {
                    "persona": "{{persona}}",
                    "goal": "{{goal}}",
                    "hidden_facts": ["{{hidden_facts}}"],
                    "reveal_rules": "{{reveal_rules}}",
                    "pressure_tactics": ["{{pressure_tactics}}"],
                    "opening_line": "",
                    "stop_when": "{{stop_when}}",
                },
            }
            if kind == "benchmark_generation":
                prompt = rb.build_benchmark_prompt(
                    "{{skill_text}}",
                    "{{evaluation_text}}",
                    "{{output_path}}",
                    difficulty_counts=None,
                )
                return prompt.replace(
                    "【构建要求】\n",
                    "【构建要求】\n{{difficulty_instruction}}",
                    1,
                )
            if kind == "benchmark_usage":
                return rb.usage_prompt_for(question)
            if kind == "benchmark_participant":
                prompt = rb.customer_turn_prompt(question, [])
                return prompt.replace(
                    "（对话尚未开始）",
                    "{{transcript}}",
                    1,
                ).replace(
                    "    - {{hidden_facts}}",
                    "{{hidden_facts}}",
                    1,
                ).replace(
                    "    - {{pressure_tactics}}",
                    "{{pressure_tactics}}",
                    1,
                )
            if kind == "benchmark_skill_reply":
                prompt = rb.skill_reply_prompt(question, [])
                return prompt.replace(
                    "（对话尚未开始）",
                    "{{transcript}}",
                    1,
                ).replace(
                    "\n  - 这是你的开场，请先做好接待与初步响应。",
                    "{{first_turn_instruction}}",
                    1,
                )
            if kind == "benchmark_judge_single":
                return rb.judge_prompt_for(
                    question,
                    "{{answer}}",
                    "{{evaluation_text}}",
                ).replace(
                    "label={{expected_labels}}",
                    "{{expected_labels}}",
                ).replace(
                    "['{{must_hit}}']",
                    "{{must_hit}}",
                ).replace(
                    "['{{must_avoid}}']",
                    "{{must_avoid}}",
                )
            if kind == "benchmark_judge_dialogue":
                prompt = rb.judge_prompt_dialogue(
                    question,
                    [],
                    {"ended_by_customer": False},
                    "{{evaluation_text}}",
                )
                return prompt.replace(
                    "（对话尚未开始）",
                    "{{transcript}}",
                    1,
                ).replace(
                    "对话跑满上限仍未达成收尾条件（可能是被测 skill 迟迟未解决目标）",
                    "{{ending}}",
                    1,
                ).replace(
                    "label={{expected_labels}}",
                    "{{expected_labels}}",
                ).replace(
                    "['{{must_hit}}']",
                    "{{must_hit}}",
                ).replace(
                    "['{{must_avoid}}']",
                    "{{must_avoid}}",
                )
            if kind == "trajectory_benchmark_generation":
                tb = _skillminer_module("trajectory_benchmark")
                fake_evidence = [{"marker": "{{evidence_json}}"}]
                prompt = tb.build_prompt(
                    {
                        "dataset_name": "{{dataset_name}}",
                        "target_total": "{{target_total}}",
                        "difficulty_counts": None,
                        "evidence": fake_evidence,
                    },
                    Path("{{output_path}}"),
                )
                return prompt.replace(
                    tb._difficulty_instruction({"difficulty_counts": None}),
                    "{{difficulty_instruction}}",
                    1,
                ).replace(
                    json.dumps(fake_evidence, ensure_ascii=False, indent=2),
                    "{{evidence_json}}",
                    1,
                )
            raise KeyError(f"unknown dynamic mining prompt: {kind}")
        finally:
            if previous is None:
                os.environ.pop("SKILLMINER_DISABLE_PROMPT_OVERRIDES", None)
            else:
                os.environ["SKILLMINER_DISABLE_PROMPT_OVERRIDES"] = previous


def _default_prompt(stage_id: str) -> str:
    entry = _PROMPTS.get(stage_id)
    if not entry:
        raise KeyError(f"unknown mining prompt: {stage_id}")
    if entry.get("dynamic"):
        return _dynamic_default(str(entry["dynamic"]))
    path = _SKILLMINER_ROOT / entry["file"]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == entry["symbol"]
            for target in node.targets
        ):
            continue
        value = ast.literal_eval(node.value)
        return str(value or "")
    raise ValueError(f"prompt symbol {entry['symbol']} not found in {path.name}")


def settings_payload(store_data: dict[str, Any]) -> dict[str, Any]:
    mining = (
        store_data.get("mining")
        if isinstance(store_data.get("mining"), dict)
        else {}
    )
    llm = (
        store_data.get("llm")
        if isinstance(store_data.get("llm"), dict)
        else {}
    )
    model = (
        mining.get("model")
        if isinstance(mining.get("model"), dict)
        else {}
    )
    pipeline = (
        mining.get("pipeline")
        if isinstance(mining.get("pipeline"), dict)
        else {}
    )
    overrides = (
        mining.get("prompts")
        if isinstance(mining.get("prompts"), dict)
        else {}
    )
    raw_temperature = model.get("temperature")
    if raw_temperature is None:
        raw_temperature = llm.get("temperature", 0.2)
    try:
        temperature = float(raw_temperature)
    except (TypeError, ValueError):
        temperature = 0.2
    prompts = []
    for stage_id, entry in _PROMPTS.items():
        default = _default_prompt(stage_id)
        raw_override = overrides.get(stage_id)
        overridden = isinstance(raw_override, str) and bool(raw_override.strip())
        effective = str(raw_override) if overridden else default
        prompts.append(
            {
                "id": stage_id,
                "label": entry["label"],
                "description": entry["description"],
                "symbol": entry.get("symbol") or entry.get("dynamic") or "",
                "default_prompt": default,
                "effective_prompt": effective,
                "overridden": overridden,
                "char_count": len(effective),
            }
        )
    return {
        "model": {
            "provider": str(
                model.get("provider")
                or llm.get("provider")
                or "custom"
            ),
            "model": str(model.get("model_id") or llm.get("model_id") or ""),
            "base_url": str(
                model.get("base_url")
                or llm.get("api_base")
                or ""
            ),
            "max_tokens": int(
                model.get("max_tokens")
                or llm.get("max_tokens")
                or 100000
            ),
            "context_length": int(
                model.get("context_length") or 240000
            ),
            "temperature": max(0.0, min(2.0, temperature)),
            "api_key_present": bool(
                model.get("api_key") or llm.get("api_key")
            ),
            "inherits_global": not bool(model),
        },
        "pipeline": {
            key: pipeline.get(key, default)
            for key, default in _PIPELINE_DEFAULTS.items()
        },
        "prompts": prompts,
    }


def update_settings(
    store_data: dict[str, Any],
    body: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("mining settings body must be an object")
    mining = store_data.setdefault("mining", {})
    model = mining.setdefault("model", {})
    pipeline = mining.setdefault("pipeline", {})
    prompts = mining.setdefault("prompts", {})
    model_in = body.get("model") if isinstance(body.get("model"), dict) else {}
    pipeline_in = (
        body.get("pipeline")
        if isinstance(body.get("pipeline"), dict)
        else {}
    )

    if bool(model_in.get("inherits_global", False)):
        mining["model"] = {}
    else:
        for source, target in (
            ("provider", "provider"),
            ("model", "model_id"),
            ("base_url", "base_url"),
        ):
            if source in model_in:
                model[target] = str(model_in.get(source) or "").strip()
        for key, default, minimum, maximum in (
            ("max_tokens", 100000, 1, 1_000_000),
            ("context_length", 240000, 1024, 4_000_000),
        ):
            if key in model_in:
                try:
                    value = int(model_in.get(key, default))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be an integer") from exc
                model[key] = max(minimum, min(maximum, value))
        existing_key = str(model.get("api_key") or "")
        if bool(model_in.get("clear_api_key", False)):
            existing_key = ""
        if str(model_in.get("api_key") or "").strip():
            existing_key = str(model_in["api_key"]).strip()
        model["api_key"] = existing_key

    integer_fields = {
        "max_rounds": (1, 20),
        "max_retries": (0, 20),
        "oneshot_timeout_seconds": (30, 86400),
        "step1_validation_retries": (0, 10),
        "benchmark_target_total": (1, 500),
        "benchmark_max_turns": (1, 50),
    }
    for key, (minimum, maximum) in integer_fields.items():
        if key not in pipeline_in:
            continue
        try:
            value = int(pipeline_in[key])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be an integer") from exc
        pipeline[key] = max(minimum, min(maximum, value))
    if "retry_backoff_seconds" in pipeline_in:
        try:
            backoff = float(pipeline_in["retry_backoff_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("retry_backoff_seconds must be numeric") from exc
        pipeline["retry_backoff_seconds"] = max(0.0, min(300.0, backoff))
    if "strict_step1" in pipeline_in:
        pipeline["strict_step1"] = bool(pipeline_in["strict_step1"])
    if "benchmark_difficulty_dist" in pipeline_in:
        pipeline["benchmark_difficulty_dist"] = str(
            pipeline_in["benchmark_difficulty_dist"] or ""
        ).strip()

    prompts_in = body.get("prompts")
    if isinstance(prompts_in, list):
        prompt_items = {
            str(item.get("id") or ""): item
            for item in prompts_in
            if isinstance(item, dict)
        }
    elif isinstance(prompts_in, dict):
        prompt_items = {
            str(stage_id): {"prompt": prompt}
            for stage_id, prompt in prompts_in.items()
        }
    else:
        prompt_items = {}
    for stage_id, item in prompt_items.items():
        if stage_id not in _PROMPTS:
            raise ValueError(f"unknown mining prompt: {stage_id}")
        prompt = str(item.get("prompt") or item.get("effective_prompt") or "")
        if not prompt.strip():
            raise ValueError(f"prompt {stage_id} must not be empty")
        default = _default_prompt(stage_id)
        if prompt == default:
            prompts.pop(stage_id, None)
        else:
            prompts[stage_id] = prompt

    return settings_payload(store_data)


def mining_model_form_payload(store_data: dict[str, Any]) -> dict[str, Any]:
    """Return the model form used by the SkillMiner console without a secret.

    The unified console owns the effective model configuration in
    ``mining.model``.  Keeping this adapter here prevents the legacy
    SkillMiner form from maintaining a second, divergent Hermes-only value.
    """
    model = settings_payload(store_data)["model"]
    model_id = str(model.get("model") or "").strip()
    base_url = str(model.get("base_url") or "").strip()
    return {
        "provider": str(model.get("provider") or "custom"),
        "id": model_id,
        "model": model_id,
        "base_url": base_url,
        "max_tokens": int(model.get("max_tokens") or 100000),
        "temperature": float(model.get("temperature") or 0.2),
        "api_key_present": bool(model.get("api_key_present")),
        "configured": bool(model_id and base_url),
    }


def update_mining_model_form(
    store_data: dict[str, Any], body: dict[str, Any]
) -> dict[str, Any]:
    """Validate and persist the legacy model form into ``mining.model``."""
    if not isinstance(body, dict):
        raise ValueError("模型配置必须是对象")

    model_id = str(body.get("model") or body.get("id") or "").strip()
    if not model_id:
        raise ValueError("请填写模型名称")
    if len(model_id) > 200 or any(ch in model_id for ch in "\r\n"):
        raise ValueError("模型名称格式不正确")

    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("Base URL 必须是有效的 HTTP(S) 地址")

    try:
        max_tokens = int(body.get("max_tokens") or 32768)
    except (TypeError, ValueError) as exc:
        raise ValueError("最大输出 Token 必须是整数") from exc
    if not 1 <= max_tokens <= 131072:
        raise ValueError("最大输出 Token 必须在 1 到 131072 之间")

    try:
        temperature = float(
            body.get("temperature") if body.get("temperature") is not None else 0.2
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Temperature 必须是数字") from exc
    if not 0 <= temperature <= 2:
        raise ValueError("Temperature 必须在 0 到 2 之间")

    incoming_key = str(body.get("api_key") or "").strip()
    if any(ch in incoming_key for ch in "\r\n") or len(incoming_key) > 4096:
        raise ValueError("API Key 格式不正确")

    mining = store_data.setdefault("mining", {})
    if not isinstance(mining, dict):
        mining = {}
        store_data["mining"] = mining
    current = mining.get("model")
    if not isinstance(current, dict):
        current = {}
    model = dict(current)
    model.update(
        {
            "provider": str(body.get("provider") or model.get("provider") or "custom").strip() or "custom",
            "model_id": model_id,
            "base_url": base_url,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    )
    if bool(body.get("clear_api_key", False)):
        model.pop("api_key", None)
    elif incoming_key:
        model["api_key"] = incoming_key
    mining["model"] = model
    return mining_model_form_payload(store_data)


def reset_prompt(store_data: dict[str, Any], stage_id: str) -> dict[str, Any]:
    if stage_id not in _PROMPTS:
        raise KeyError(f"unknown mining prompt: {stage_id}")
    mining = store_data.setdefault("mining", {})
    prompts = mining.setdefault("prompts", {})
    prompts.pop(stage_id, None)
    return settings_payload(store_data)
