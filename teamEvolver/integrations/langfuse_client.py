"""Deprecated import alias; implementation lives in session_ingestion.adapters."""
import sys

from session_ingestion.adapters._shared import langfuse_client as _implementation

sys.modules[__name__] = _implementation
