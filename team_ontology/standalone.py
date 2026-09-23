"""Isolated two-service harness; production uses install_native in TE."""

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from .api import install
from .config import OntologyConfig


def create_app():
    if os.environ.get("ONTOLOGY_DEMO_MODE") != "1":
        raise RuntimeError("Harness requires explicit demo mode")
    identities = json.loads(Path(os.environ["ONTOLOGY_DEMO_IDENTITIES"]).read_text())
    app = FastAPI(title="TE Knowledge Operations — isolated")

    async def principal(request: Request):
        value = identities.get(request.headers.get("authorization", "").removeprefix("Bearer "))
        if not value:
            raise HTTPException(401, "UNAUTHENTICATED")
        return value

    async def resolver(p):
        key = next((key for key, value in identities.items() if value == p), None)
        if key is None:
            raise HTTPException(403, "UNKNOWN_IDENTITY")
        return {"url": os.environ["ONTOLOGY_LAB_OV_URL"], "account": p["tenant"], "api_key": os.environ["ONTOLOGY_LAB_ROOT_KEY"], "model": {}}

    config = OntologyConfig(
        enabled=True,
        state_dir=os.environ["TE_ONTOLOGY_STATE"],
        allow_fixture=True,
    )
    install(app, principal, config=config, dsn=os.environ["ONTOLOGY_LAB_TE_DSN"], resolver=resolver)
    return app
