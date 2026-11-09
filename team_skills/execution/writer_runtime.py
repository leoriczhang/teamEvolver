"""Lossless, bounded requests for the shared Prompt editor."""
from __future__ import annotations

import copy
import json

from team_skills.library.repository import SkillLibraryError
from teamEvolver.evidence_transport import TRANSPORT_INSTRUCTIONS, encode_evidence

MAX_EDITOR_INPUT_BYTES = 786432


class EditorInputTooLarge(SkillLibraryError):
    """Complete recorded Evidence and editing history cannot fit this request."""


def input_bytes(messages):
    return len(json.dumps(messages, ensure_ascii=False).encode())


def editor_messages(system, payload):
    """Reserve instructions and framing before encoding; never clip Evidence."""
    if payload.get("variable_groups"):
        payload = copy.deepcopy(payload)
        for key in ("execution_plan", "baseline_execution_plan"):
            for part in (payload.get(key) or {}).get("components") or []:
                if (part.get("binding") or {}).get("inline_variable"):
                    part.pop("content", None)
                    part["content_deferred"] = "read_variable_group or read_component"
    reserve = input_bytes([{"role": "system", "content": system + TRANSPORT_INSTRUCTIONS},
                           {"role": "user", "content": ""}])
    body, packed = encode_evidence(payload, max_bytes=max(1, MAX_EDITOR_INPUT_BYTES - reserve))
    messages = [{"role": "system", "content": system + (TRANSPORT_INSTRUCTIONS if packed else "")},
                {"role": "user", "content": body}]
    if input_bytes(messages) > MAX_EDITOR_INPUT_BYTES:
        raise EditorInputTooLarge("Complete Prompt editing input exceeds the byte budget; nothing was truncated")
    return messages


class BoundedEditorClient:
    """Check every full round, including observations, before the provider call."""

    def __init__(self, delegate):
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def chat(self, messages, **options):
        if input_bytes(messages) > MAX_EDITOR_INPUT_BYTES:
            raise EditorInputTooLarge("Complete Prompt editing history exceeds the byte budget; nothing was truncated")
        return await self.delegate.chat(messages, **options)
