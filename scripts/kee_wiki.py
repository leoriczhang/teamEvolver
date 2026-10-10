#!/usr/bin/env python3
"""Offline producer/validator. No KEE/OV credentials required or network calls."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from team_ontology.kee.contracts import ProcedurePack
from team_ontology.kee.wiki import render, validate_directory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["export", "validate"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sources", type=Path)
    args = parser.parse_args()
    if args.command == "export":
        if not args.output:
            parser.error("export requires --output")
        pack = ProcedurePack.model_validate_json(args.path.read_text())
        if args.output.exists() and any(args.output.iterdir()):
            parser.error("output must be a new/empty directory")
        for name, text in render(pack).items():
            target = args.output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        folder = args.output
    else:
        folder = args.path
    sources = json.loads(args.sources.read_text()) if args.sources else None
    result = validate_directory(folder, sources)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
