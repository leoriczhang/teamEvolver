#!/usr/bin/env python3
"""Extract current route, CLI and configuration inventories into an explicit output directory."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

PKGS = [
    "teamEvolver",
    "session_ingestion",
    "team_skills",
    "team_replay",
    "team_memory",
    "team_miner",
]

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}

# Decorator receivers that we skip (not real HTTP routes), e.g. threadpool.get.
SKIP_RECEIVERS = {"self", "cls", "logger", "log"}


def iter_py_files(root: Path):
    for pkg in PKGS:
        base = root / pkg
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            yield path


def extract_routes(root: Path):
    routes = []
    for path in iter_py_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not isinstance(func, ast.Attribute):
                    continue
                method = func.attr.lower()
                if method not in HTTP_METHODS:
                    continue
                receiver = func.value.id if isinstance(func.value, ast.Name) else None
                if receiver in SKIP_RECEIVERS:
                    continue
                # First positional arg is the path (if it is a literal string).
                path_arg = None
                if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                    path_arg = dec.args[0].value
                routes.append(
                    {
                        "method": method.upper(),
                        "path": path_arg if path_arg is not None else "<dynamic>",
                        "handler": node.name,
                        "receiver": receiver or "<expr>",
                        "file": path.relative_to(root).as_posix(),
                        "line": node.lineno,
                    }
                )
    routes.sort(key=lambda r: (r["path"], r["method"], r["file"], r["line"]))
    return routes


def extract_cli(root: Path):
    cmds = []
    base = root / "teamEvolver" / "cli"
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if not isinstance(target, ast.Attribute):
                    continue
                if target.attr not in {"command", "group"}:
                    continue
                name = None
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant):
                    name = dec.args[0].value
                cmds.append(
                    {
                        "kind": target.attr,
                        "name": name or node.name.replace("_", "-"),
                        "func": node.name,
                        "file": path.relative_to(root).as_posix(),
                        "line": node.lineno,
                    }
                )
    cmds.sort(key=lambda c: (c["file"], c["line"]))
    return cmds


KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_.-]+)\s*:(.*)$")


def extract_config_keys(root: Path):
    keys = {}
    cfg_dir = root / "config"
    for path in sorted(cfg_dir.glob("*.yaml")):
        file_keys = []
        stack = []  # (indent, key)
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            m = KEY_RE.match(raw)
            if not m:
                continue
            indent = len(m.group(1))
            key = m.group(2)
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, key))
            file_keys.append(".".join(k for _, k in stack))
        keys[path.name] = sorted(set(file_keys))
    return keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out = (root / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    routes = extract_routes(root)
    cli = extract_cli(root)
    config = extract_config_keys(root)

    (out / "routes.json").write_text(json.dumps(routes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "cli.json").write_text(json.dumps(cli, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "config.json").write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"routes: {len(routes)}")
    print(f"cli: {len(cli)}")
    print(f"config files: {len(config)} / keys: {sum(len(v) for v in config.values())}")


if __name__ == "__main__":
    main()
