"""Copy this file to adapt request/response shapes for an external scoring Agent.

Only Root-authored Python is executable. Keep credentials in the selected Secret
Ref; ``secret`` is resolved inside the Worker, never in persisted source/config.
"""
import json

import httpx

EVALUATOR_ADAPTER = {"label": "HTTP external scoring Agent", "enabled": True}


def evaluate(request, config, *, secret=""):
    """Return both scores from the saved baseline/candidate execution outputs.

    Customize payload construction, auth headers, and response conversion below.
    A ground-truth grader should resolve its own frozen reference using case_id
    or explicit task inputs. Never infer the expected answer from either reply.
    """
    headers = {"Content-Type": "application/json", "Idempotency-Key": request["request_id"]}
    if secret:
        headers["Authorization"] = "Bearer " + secret
    payload = request  # Replace with your scoring Agent's input contract.
    body = bytearray()
    with httpx.Client(timeout=config["timeout_seconds"], follow_redirects=False) as client:
        with client.stream("POST", config["url"], json=payload, headers=headers) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > 1024 * 1024:
                    raise ValueError("evaluator response exceeds 1 MiB")
                body.extend(chunk)
    value = json.loads(body)
    return {
        "request_id": request["request_id"],
        "baseline": {"score": value["baseline"]["score"], "reason": value["baseline"]["reason"]},
        "candidate": {"score": value["candidate"]["score"], "reason": value["candidate"]["reason"]},
        **({"usage": value["usage"]} if "usage" in value else {}),
    }
