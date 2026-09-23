"""Compatibility import for the relocated Session pull implementation."""

from session_ingestion.pull.service import pull_sessions

__all__ = ["pull_sessions"]
