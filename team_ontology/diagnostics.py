"""Metadata-only ontology diagnostics; independent of publication decisions."""

import functools
import logging
import time
from collections import OrderedDict

from teamEvolver.logging_runtime import event, log_context, request_id, safe_code

logger = logging.getLogger("team_ontology.operations")


def upstream_error(response, path):
    """Normalize enterprise and native OV errors without exposing remote bodies."""
    fallback = "OV_NOT_FOUND" if response.status_code == 404 else (
        "OV_VALIDATION_ERROR" if response.status_code == 422 else "OV_REQUEST_FAILED"
    )
    try:
        value = response.json()
    except ValueError:
        value = {}
    if not isinstance(value, dict):
        value = {}
    native = value.get("error")
    native = native if isinstance(native, dict) else {}
    detail = value.get("detail")
    detail_code = detail.get("code") if isinstance(detail, dict) else detail
    code = safe_code(detail_code, safe_code(native.get("code"), fallback))
    message = native.get("message")
    if (path == "/snapshots/freeze" and response.status_code == 400 and code == "INVALID_ARGUMENT"
            and isinstance(message, str) and message.startswith("Directory URI is not readable as a file:")):
        return "SOURCE_URI_IS_DIRECTORY", code
    return code, code


class FailureSummary:
    def __init__(self, interval=60):
        self.interval = interval
        self.entries = OrderedDict()

    def failure(self, key, code, **fields):
        now = time.monotonic()
        old = self.entries.get(key)
        if old and old[0] == code and now - old[1] < self.interval:
            self.entries[key] = (code, old[1], old[2] + 1)
            return
        event(
            logger,
            "ontology.failure",
            logging.ERROR,
            stage=key[-1],
            code=code,
            suppressed=old[2] if old else 0,
            **fields,
        )
        import sys

        if sys.exc_info()[0] is not None:
            logger.error("ontology.failure_trace", exc_info=True)
        self.entries[key] = (code, now, 0)
        self.entries.move_to_end(key)
        if len(self.entries) > 1024:
            self.entries.popitem(last=False)

    def success(self, key, **fields):
        old = self.entries.pop(key, None)
        if old:
            event(logger, "ontology.recovered", stage=key[-1], suppressed=old[2], **fields)


failures = FailureSummary()


def diagnostic(stage, *, quiet=False):
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(self, *args, **kwargs):
            principal = args[0] if args and isinstance(args[0], dict) else {}
            context = {"tenant": principal.get("tenant", ""), "user": principal.get("subject", "")}
            if principal.get("id"):
                context.update(
                    job_id=principal["id"],
                    attempt=principal.get("attempt", 0),
                    request_id=request_id() or f"{principal['id']}-{principal.get('attempt', 0)}",
                )
            elif len(args) > 1 and isinstance(args[1], str) and args[1].startswith("ont_"):
                context["job_id"] = args[1]
            key = (context["tenant"], context["user"], stage)
            started = time.monotonic()
            with log_context(**{k: v for k, v in context.items() if v != ""}):
                if not quiet:
                    event(logger, "ontology.stage_started", stage=stage)
                try:
                    result = await fn(self, *args, **kwargs)
                except Exception as exc:
                    failures.failure(
                        key,
                        safe_code(getattr(exc, "detail", None), type(exc).__name__),
                        duration_ms=round((time.monotonic() - started) * 1000, 1),
                    )
                    raise
                failures.success(key)
                details = {}
                if isinstance(result, dict):
                    details = {k: result[k] for k in ("id", "state", "attempt", "generation") if k in result}
                    nested = result.get("result") or {}
                    artifact = nested.get("artifact") or {}
                    receipt = nested.get("receipt") or {}
                    details.update(
                        candidate_digest=artifact.get("candidate_digest", ""), generation=receipt.get("generation", "")
                    )
                event(
                    logger,
                    "ontology.stage_completed",
                    logging.DEBUG if quiet else logging.INFO,
                    stage=stage,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    **details,
                )
                return result

        return wrapper

    return decorate
