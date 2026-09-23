"""LLM 客户端：OpenAI 兼容 chat completions + JSON Schema 结构化输出 + 重试。

确定性不变量：LLM 只负责提议，本模块保证返回的 JSON 要么通过调用方提供的
JSON Schema 校验，要么抛出 LLMError（由流水线决定修复重试或标记人工）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from ..config import LLMConfig


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> Any:
    """从自由文本中提取 JSON（兼容端点不支持 response_format 时的降级路径）。"""
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = httpx.Client(timeout=config.timeout_seconds)

    @property
    def model(self) -> str:
        return self.config.model

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        prompt_version: str,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """结构化输出：JSON Schema 约束 + 形状修复 + 校验 + 指数退避重试。"""
        validator = Draft202012Validator(json_schema)
        last_error: str | None = None
        for attempt in range(max_retries):
            try:
                payload = self._call(system, user, json_schema, extra_note=last_error)
            except (httpx.HTTPError, LLMError) as exc:
                last_error = f"调用失败：{exc}"
                if attempt == max_retries - 1:
                    raise LLMError(f"LLM 调用最终失败：{last_error}") from exc
                time.sleep(min(2**attempt, 8))
                continue
            payload = _shape_repair(payload, json_schema)
            if not isinstance(payload, dict):
                last_error = f"响应不是 JSON 对象：{str(payload)[:200]}"
                if attempt == max_retries - 1:
                    raise LLMError(f"LLM 结构化输出最终校验失败：{last_error}")
                time.sleep(min(2**attempt, 8))
                continue
            validation_errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
            if not validation_errors:
                return payload
            last_error = "响应不符合 JSON Schema：" + "；".join(
                f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in validation_errors[:5]
            )
            if attempt == max_retries - 1:
                raise LLMError(f"LLM 结构化输出最终校验失败：{last_error}")
            time.sleep(min(2**attempt, 8))
        raise LLMError("unreachable")

    def _call(self, system: str, user: str, json_schema: dict[str, Any], extra_note: str | None) -> dict[str, Any]:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": user + "\nExpected JSON Schema:\n" + json.dumps(json_schema, ensure_ascii=False),
            },
        ]
        if extra_note:
            messages.append(
                {"role": "user", "content": f"上一轮输出被拒绝，原因：{extra_note}\n请修正后仅输出符合要求的 JSON。"}
            )
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        if self.config.strict_json:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": json_schema},
            }

        response = self._client.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        if response.status_code in {400, 422} and self.config.strict_json:
            # 端点不支持 json_schema response_format：降级为 prompt 约束 + 手工解析
            self.config.strict_json = False
            response = self._client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={**body, "response_format": None},
            )
        if response.status_code == 429 or response.status_code >= 500:
            raise httpx.HTTPError(f"upstream status {response.status_code}: {response.text[:200]}")
        if response.status_code != 200:
            raise LLMError(f"upstream status {response.status_code}: {response.text[:300]}")
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"unexpected response shape: {str(data)[:300]}") from exc
        payload = _extract_json(content)
        return payload


def _shape_repair(payload: Any, json_schema: dict[str, Any]) -> Any:
    """确定性形状修复：模型偶尔输出裸数组，而 schema 期望单数组键对象（如
    {"relations": [...]}）——自动包裹，其余交给校验重试。"""
    if isinstance(payload, list):
        properties = json_schema.get("properties") or {}
        array_keys = [
            key for key, value in properties.items() if isinstance(value, dict) and value.get("type") == "array"
        ]
        if len(array_keys) == 1:
            return {array_keys[0]: payload}
    return payload


Responder = Callable[[str, str, str, dict[str, Any]], dict[str, Any]]


def _cache_key(prompt_version: str, system: str, user: str) -> str:
    digest = hashlib.sha256(f"{prompt_version}\x00{system}\x00{user}".encode()).hexdigest()[:20]
    return f"{prompt_version}-{digest}"


class ScriptedLLM:
    """测试用：responder(prompt_version, system, user, json_schema) -> dict。"""

    def __init__(self, responder: Responder, model: str = "scripted"):
        self._responder = responder
        self.model_name = model

    @property
    def model(self) -> str:
        return self.model_name

    def complete_json(
        self, *, system: str, user: str, json_schema: dict[str, Any], prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]:
        payload = self._responder(prompt_version, system, user, json_schema)
        if Draft202012Validator(json_schema).is_valid(payload):
            return payload
        raise LLMError("scripted responder returned payload that fails its JSON schema")


class RecordReplayLLM:
    """录制/重放：首次真实调用落盘，之后按 (prompt_version, system, user) 哈希重放。

    保证黄金测试与离线复现可用同一份录制响应。
    """

    def __init__(self, delegate: LLMClient | ScriptedLLM, record_path: Path):
        self._delegate = delegate
        self._record_path = Path(record_path)
        self._cache: dict[str, dict[str, Any]] = {}
        if self._record_path.exists():
            for line in self._record_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                self._cache[entry["key"]] = entry["response"]

    @property
    def model(self) -> str:
        return self._delegate.model

    def complete_json(
        self, *, system: str, user: str, json_schema: dict[str, Any], prompt_version: str, max_retries: int = 3
    ) -> dict[str, Any]:
        key = _cache_key(prompt_version, system, user)
        if key in self._cache:
            return self._cache[key]
        payload = self._delegate.complete_json(
            system=system,
            user=user,
            json_schema=json_schema,
            prompt_version=prompt_version,
            max_retries=max_retries,
        )
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._record_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "response": payload}, ensure_ascii=False) + "\n")
        self._cache[key] = payload
        return payload
