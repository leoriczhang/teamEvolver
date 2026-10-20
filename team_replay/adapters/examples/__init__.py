"""Reference Replay Adapters.

Files here are **upload templates**, not import targets. Tenants install
them via the Root-Key endpoint ``PUT /api/replay-adapter/code``; adapter
source lives in PostgreSQL at runtime, not on disk. Do not import these
modules from production code.
"""
