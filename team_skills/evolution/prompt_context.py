"""Pinned model-stage settings passed by HTTP/Worker entrypoints."""
from contextlib import contextmanager
from contextvars import ContextVar

_settings = ContextVar("skill_prompt_settings", default=None)


@contextmanager
def prompt_context(config):
    token = _settings.set(config)
    try:
        yield
    finally:
        _settings.reset(token)


def settings():
    return _settings.get()
