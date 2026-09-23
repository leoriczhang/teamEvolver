"""Deprecated import alias; implementation lives in session_ingestion.adapters."""
import sys

from session_ingestion.adapters._shared import legacy_converter as _implementation

if __name__ == "__main__":
    _implementation._preview_worker()
else:
    sys.modules[__name__] = _implementation
