"""Compatibility imports for the relocated Hermes Session feed."""

from __future__ import annotations

import sys

from session_ingestion.push.hermes import install, push_session

sys.modules[f"{__name__}.install"] = install
sys.modules[f"{__name__}.push_session"] = push_session

__all__ = ["install", "push_session"]
