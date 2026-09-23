"""Deprecated offline export entry point; upstream code lives in session_ingestion/adapters/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_ingestion.adapters._shared.pull_sessions_local import main

if __name__ == "__main__":
    raise SystemExit(main())
