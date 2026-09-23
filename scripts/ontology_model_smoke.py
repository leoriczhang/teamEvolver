#!/usr/bin/env python3
"""Explicit opt-in real extraction with synthetic text; never prints credentials."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

import yaml
from dotenv import dotenv_values

from team_ontology.extractor import extract
from team_ontology.wire import Manifest, Schema, Source, digest
from teamEvolver.llm import _call_in_pool

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--env-file", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
config = yaml.safe_load(args.config.read_text())["llm"]
secret = dotenv_values(args.env_file).get("TEAMEVOLVER_LLM_API_KEY", "")
source = Source(
    source_id="synthetic-receipt",
    revision="r1",
    readers=["frank"],
    text="合成测试：运单 shipment:001 属于 Shipment。2026-09-20T08:00:00Z 运单状态 status 为 signed。此记录只用于测试，无真实业务单号。",
)
schema = Schema(
    revision="synthetic-v1",
    entity_types=["Shipment"],
    predicates={"status": {"subject_type": "Shipment"}},
    rules=[],
    issue_pack_revision="synthetic-v1",
    evidence_slots=["status"],
)
body = source.model_dump(mode="json")
manifest = Manifest(
    schema_revision=schema.revision,
    expected_generation=0,
    sources=[{"source_id": source.source_id, "revision": source.revision, "digest": digest(body)}],
    extractor="llm",
    deadline_seconds=120,
).model_dump(mode="json")
result = {"mode": "real_model", "model": config["model_id"], "synthetic_only": True}
started = time.monotonic()
try:
    if not secret:
        raise ValueError("MODEL_API_KEY_MISSING")
    with tempfile.TemporaryDirectory(prefix="te-real-model-") as folder:
        bundle = asyncio.run(
            _call_in_pool(
                extract,
                inputs={"manifest": manifest, "sources": [body], "schema": schema.model_dump(mode="json")},
                workspace=Path(folder),
                model_config={"model": config["model_id"], "base_url": config["api_base"], "api_key": secret},
            )
        )
        # The smoke test is the cross-repository boundary; TE runtime never imports OV.
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "OpenViking"))
        from openviking.ontology.validation import validate
        from openviking.ontology.contracts import CandidateBundle, Schema as OVSchema

        validate(
            CandidateBundle.model_validate(bundle),
            manifest,
            OVSchema.model_validate(schema.model_dump()),
            {(source.source_id, source.revision): body},
        )
        result.update(
            status="passed",
            extraction=bundle["extraction"],
            assertions=len(bundle["assertions"]),
            scope="extraction, JSON Schema and OV deterministic evidence validation; not publication approval",
        )
except Exception as exc:
    result.update(status="failed", error_type=type(exc).__name__)
    result["reason"] = str(exc).replace(secret, "[redacted]").replace(config["api_base"], "[endpoint]")[:600]
    cause = exc
    chain = []
    while cause and len(chain) < 5:
        chain.append(type(cause).__name__)
        cause = cause.__cause__
    result["exception_chain"] = chain
result["elapsed_seconds"] = round(time.monotonic() - started, 2)
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False))
