"""Bounded synchronous HTTP transport for a configured external Replay scorer."""
from __future__ import annotations

import json

import httpx

MAX_RESPONSE_BYTES = 1024 * 1024


def evaluate_external_http(request, config, secret=""):
    headers = {"Content-Type": "application/json", "Idempotency-Key": request["request_id"]}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    try:
        with httpx.Client(timeout=config["timeout_seconds"], follow_redirects=False) as client:
            with client.stream("POST", config["url"], json=request, headers=headers) as response:
                if not 200 <= response.status_code < 300:
                    raise ValueError("external evaluator returned a non-success HTTP status")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ValueError("external evaluator response exceeds the size limit")
                    body.extend(chunk)
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("external evaluator response must be a JSON object")
        return value
    except (httpx.HTTPError, ValueError):
        # HTTP/provider messages may contain URLs, credentials or response text.
        raise RuntimeError("external evaluator request failed") from None
