"""Closed, text-free diagnostic contract for mandatory Session Analyze failures."""
from __future__ import annotations

from itertools import combinations

ANALYSIS_SECTIONS = ("classification", "summary", "judge")
PROVIDER_ERROR_TYPES = frozenset({
    "TimeoutError", "APITimeoutError", "APIConnectionError", "RateLimitError", "AuthenticationError",
    "PermissionDeniedError", "BadRequestError", "NotFoundError", "ConflictError", "UnprocessableEntityError",
    "InternalServerError", "APIStatusError", "LLMOverloadedError", "RuntimeError", "ValueError",
    "ReadTimeout", "ConnectTimeout", "PoolTimeout", "ConnectError", "HTTPStatusError", "RemoteProtocolError",
    "ReadError", "WriteError", "OtherError",
})
ANALYSIS_DIAGNOSTIC_CODES = frozenset({
    "session_analysis.llm_unconfigured",
    *("session_analysis.provider." + name for name in PROVIDER_ERROR_TYPES),
    *("session_analysis.missing." + "+".join(parts)
      for size in range(1, 4) for parts in combinations(ANALYSIS_SECTIONS, size)),
})


def safe_analysis_diagnostic(value):
    return value if isinstance(value, str) and value in ANALYSIS_DIAGNOSTIC_CODES else ""


def missing_analysis_diagnostic(sections):
    names = [name for name in ANALYSIS_SECTIONS if name in sections]
    return safe_analysis_diagnostic("session_analysis.missing." + "+".join(names))


def provider_analysis_diagnostic(error):
    name = type(error).__name__
    return "session_analysis.provider." + (name if name in PROVIDER_ERROR_TYPES else "OtherError")


def ingestion_error_code(error):
    """Only recognized attributes cross the boundary; legacy exceptions keep their type code."""
    try:
        diagnostic = safe_analysis_diagnostic(getattr(error, "diagnostic_code", None))
    except Exception:  # A hostile property must not mask the original failure.
        diagnostic = ""
    return diagnostic or type(error).__name__
