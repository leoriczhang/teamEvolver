"""Deprecated import alias; implementation lives in session_ingestion.adapters."""
import sys

from session_ingestion.adapters._shared import legacy_parser as _implementation

sys.modules[__name__] = _implementation
