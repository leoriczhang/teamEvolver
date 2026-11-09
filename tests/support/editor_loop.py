"""Scripted provider for editing-loop contracts; never calls a model or service."""
from __future__ import annotations

import copy
import json


class ScriptedEditorLLM:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def chat(self, messages, **options):
        self.calls.append({"messages": copy.deepcopy(messages), "options": copy.deepcopy(options)})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
