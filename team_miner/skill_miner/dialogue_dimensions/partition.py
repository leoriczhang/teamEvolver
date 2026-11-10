"""Joint limits over complete model files, never over dialogue substrings."""

from .input import canonical

DEFAULTS = {"quote_mode": "none", "max_cases_per_compile": 8, "extract_input_chars": 24000,
            "extract_input_bytes": 96000, "max_case_chars": 20000, "merge_window_items": 12,
            "merge_input_chars": 16000, "merge_input_bytes": 64000, "max_inflight_compiles": 1,
            "repair_attempts_per_unit": 2}


def options(raw=None):
    raw = raw or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError("unknown dialogue_options")
    result = {**DEFAULTS, **raw}
    if result["quote_mode"] not in {"none", "sentences"}:
        raise ValueError("quote_mode must be none or sentences")
    for key, value in result.items():
        if key == "quote_mode":
            continue
        ceiling = {"max_inflight_compiles": 2, "repair_attempts_per_unit": 10,
                   "max_cases_per_compile": 64, "merge_window_items": 64}.get(key, 1000000)
        minimum = 0 if key == "repair_attempts_per_unit" else 1
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= ceiling:
            raise ValueError("invalid dialogue_options." + key)
    return result


def batches(items, limits):
    """Yield (complete items, oversized items); callers preserve oversized source records."""
    batch, chars, size = [], 0, 0
    for item in items:
        text = item["content"]
        length, encoded = len(text), len(text.encode("utf-8"))
        too_big = (length > limits["extract_input_chars"] or encoded > limits["extract_input_bytes"]
                   or item.get("dialogue_chars", 0) > limits["max_case_chars"])
        if too_big:
            yield [], [item]
            continue
        if batch and (len(batch) >= limits["max_cases_per_compile"]
                      or chars + length > limits["extract_input_chars"]
                      or size + encoded > limits["extract_input_bytes"]):
            yield batch, []
            batch, chars, size = [], 0, 0
        batch.append(item)
        chars += length
        size += encoded
    if batch:
        yield batch, []


def window_text(window):
    return "# Dimension window\n\n```json\n" + canonical(window) + "\n```\n"


def fits_window(window, limits):
    text = window_text(window)
    return (len(window["members"]) <= limits["merge_window_items"]
            and len(text) <= limits["merge_input_chars"]
            and len(text.encode("utf-8")) <= limits["merge_input_bytes"])
