#!/usr/bin/env python3
"""Export an old enhancer project into a TE import envelope; never publish or copy keys."""

import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("project", type=Path)
parser.add_argument("--pack-id", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if not args.pack_id.replace("-", "").replace("_", "").isalnum():
    parser.error("invalid pack id")
root = args.project.resolve()
annotated = root / "packs" / args.pack_id / "annotated.json"
if not annotated.is_file():
    parser.error("expected packs/<pack-id>/annotated.json; supply the .ontology-enhancer project directory")


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.is_file() else []


result = {
    "import_key": f"legacy:{args.pack_id}",
    "annotated": json.loads(annotated.read_text()),
    "historical_versions": lines(root / "packs" / args.pack_id / "published" / "versions.jsonl"),
    "decisions": lines(root / "decisions.jsonl"),
}
args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print("Import envelope exported; upload in TE. This does not publish an OV version.")
