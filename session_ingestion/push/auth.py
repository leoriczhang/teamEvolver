"""Authentication helpers for Agent-initiated Session ingestion."""

from __future__ import annotations

import os
import secrets

from fastapi import HTTPException, Request


def bearer_token(request: Request) -> str:
    header = str(request.headers.get("authorization") or "").strip()
    return header[7:].strip() if header.lower().startswith("bearer ") else header


def check_legacy_ingest_key(request: Request) -> None:
    expected = str(os.environ.get("EVOLVE_INGEST_API_KEY") or "").strip()
    if not expected:
        return
    if not secrets.compare_digest(bearer_token(request), expected):
        raise HTTPException(status_code=401, detail="invalid ingest api key")
