"""Settings that directly control OpenViking Skill mining."""

from __future__ import annotations

import copy
from typing import Any

from .ov_compile_mining import PLAN_SCHEMA_VERSION, SKILL_BODY

PIPELINE_DEFAULTS = {"max_rounds": 3, "runtime_timeout_seconds": 3600}
PIPELINE_RANGES = {"max_rounds": (1, 5), "runtime_timeout_seconds": (60, 7200)}
COMPILER_PROMPT_ID = "ov_compile"
MAX_COMPILER_BYTES = 256 * 1024
LEGACY_COMPILER_WARNING = (
    "当前编译 Skill 未声明能力拆分计划契约，新任务仍按单候选编译。"
    "恢复默认编译 Skill 后，新任务可生成多个 Skill Candidate。"
)


def pipeline_settings(store_data: dict[str, Any]) -> dict[str, int]:
    mining = store_data.get("mining") or {}
    raw = (mining.get("pipeline") or {}) if isinstance(mining, dict) else {}
    result = dict(PIPELINE_DEFAULTS)
    for key, default in PIPELINE_DEFAULTS.items():
        value = raw.get(key, default) if isinstance(raw, dict) else default
        try:
            value = int(value) if not isinstance(value, bool) else default
        except (TypeError, ValueError):
            value = default
        minimum, maximum = PIPELINE_RANGES[key]
        result[key] = max(minimum, min(maximum, value))
    return result


def compiler_prompt(store_data: dict[str, Any]) -> str:
    mining = store_data.get("mining") or {}
    prompts = (mining.get("prompts") or {}) if isinstance(mining, dict) else {}
    body = prompts.get(COMPILER_PROMPT_ID) if isinstance(prompts, dict) else None
    return body if isinstance(body, str) and body.strip() else SKILL_BODY


def compiler_contract_version(body: str) -> int:
    """Preserve saved single-candidate compilers without rewriting their content."""
    return 2 if PLAN_SCHEMA_VERSION in body else 1


def settings_payload(store_data: dict[str, Any]) -> dict[str, Any]:
    body = compiler_prompt(store_data)
    contract_version = compiler_contract_version(body)
    warning = LEGACY_COMPILER_WARNING if contract_version == 1 else ""
    return {
        "engine": "ov_compile",
        "candidate_contract_version": contract_version,
        "candidate_mode": "collection" if contract_version == 2 else "single",
        "candidate_mode_warning": warning,
        "options_warnings": [warning] if warning else [],
        "model": {"configuration_source": "openviking_server", "editable_here": False},
        "pipeline": pipeline_settings(store_data),
        "prompts": [{
            "id": COMPILER_PROMPT_ID,
            "label": "Skill 挖掘编译",
            "description": (
                "OpenViking 每轮使用的编译 Skill；每轮受理前冻结，已有轮次不受后续修改影响。"
                + (warning or "由模型规划 0–8 个独立 Skill Candidate，各自生成 Test Dataset。")
            ),
            "default_prompt": SKILL_BODY,
            "effective_prompt": body,
            "overridden": body != SKILL_BODY,
            "char_count": len(body),
        }],
    }


def update_settings(store_data: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise ValueError("挖掘设置必须为对象")
    mining = {
        "pipeline": pipeline_settings(store_data),
        "prompts": {},
    }
    current = compiler_prompt(store_data)
    if current != SKILL_BODY:
        mining["prompts"][COMPILER_PROMPT_ID] = current
    incoming = body.get("pipeline", {})
    if not isinstance(incoming, dict):
        raise ValueError("pipeline 必须为对象")
    unknown = set(incoming) - set(PIPELINE_DEFAULTS)
    if unknown:
        raise ValueError("不支持的挖掘参数：" + ", ".join(sorted(unknown)))
    for key, value in incoming.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} 必须为整数")
        minimum, maximum = PIPELINE_RANGES[key]
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
        mining["pipeline"][key] = value
    prompts = body.get("prompts", [])
    if not isinstance(prompts, list):
        raise ValueError("prompts 必须为数组")
    for entry in prompts:
        if not isinstance(entry, dict) or entry.get("id") != COMPILER_PROMPT_ID:
            raise ValueError("仅支持当前 OpenViking 编译 Skill")
        prompt = entry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("编译 Skill 不能为空")
        if len(prompt.encode("utf-8")) > MAX_COMPILER_BYTES:
            raise ValueError("编译 Skill 超过 256 KB")
        # Use exactly the validator used when provisioning the immutable OV package.
        from .ov_round_orchestration import _compiler_package
        _compiler_package(prompt)
        if prompt == SKILL_BODY:
            mining["prompts"].pop(COMPILER_PROMPT_ID, None)
        else:
            mining["prompts"][COMPILER_PROMPT_ID] = prompt
    updated = copy.deepcopy(store_data)
    updated["mining"] = mining
    payload = settings_payload(updated)
    store_data["mining"] = mining
    return payload


def reset_prompt(store_data: dict[str, Any], stage_id: str) -> dict[str, Any]:
    if stage_id != COMPILER_PROMPT_ID:
        raise KeyError("仅支持当前 OpenViking 编译 Skill")
    mining = {
        "pipeline": pipeline_settings(store_data),
        "prompts": {},
    }
    store_data["mining"] = mining
    return settings_payload(store_data)
