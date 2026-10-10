"""Fixed paths for the non-PostgreSQL control plane."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def control_state_dir(config: Any = None) -> Path:
    """Return the directory for local control-plane state.

    The path follows the active config file when one is known. Otherwise it
    uses the process-wide TeamEvolver config directory. There is intentionally
    no per-feature path setting.
    """
    config_file = str(getattr(config, "_config_file", "") or "").strip()
    if config_file:
        return Path(config_file).expanduser().parent
    configured = os.environ.get("TEAMEVOLVER_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".teamEvolver"


def identity_database_file(config: Any = None) -> Path:
    """Return the standalone identity database path."""
    return control_state_dir(config) / "console_identity.sqlite3"
