"""docs contract: documented endpoints, CLI and env vars match the code.

The doc reference checker (docs/scripts/check-docs-refs.mjs) validates links
and code paths. This test covers what it cannot: every HTTP route, CLI command
and ``TEAMEVOLVER_*`` variable named in the maintained docs must exist in the
final implementation, and the generated endpoint index must be current.
"""

from __future__ import annotations

import importlib.util
import re

import pytest
from click.testing import CliRunner

from tests.support.paths import REPO_ROOT

ROOT = REPO_ROOT
DOCS = ROOT / "docs"
MAINTAINED = sorted(
    [
        *(DOCS / "zh").rglob("*.md"),
        *(DOCS / "en").rglob("*.md"),
        DOCS / "README.md",
        ROOT / "README.md",
        ROOT / "README.en.md",
        ROOT / "AGENTS.md",
    ]
)

# Paths served by OpenViking or Langfuse, quoted where the
# docs describe what TeamEvolver calls upstream. They are not TeamEvolver routes.
UPSTREAM_PREFIXES = ("/api/v1/", "/api/public/")

ROUTE_RE = re.compile(
    r"`(?:(?:GET|POST|PUT|PATCH|DELETE)\s+)?"
    r"(/(?:api|internal|sync|ingest_session|healthz|readyz|status|history|console)"
    r"(?:[A-Za-z0-9_{}/.:-]*))"
    r"(?:\?[^`]*)?`"
)
# CLI invocations: inline code spans and code-block lines that start with the command.
# `teamEvolver is ...` is the CLI's own "already running" message quoted in troubleshooting.
CLI_RE = re.compile(r"(?:^|`|\$ )\s*teamEvolver (?!is )((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)?)\b", re.M)
CODE_RE = re.compile(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", re.S)
ENV_RE = re.compile(r"(?<![A-Z0-9_])TEAMEVOLVER_[A-Z][A-Z0-9_]*[A-Z0-9]\b")


def _load_index_module():
    spec = importlib.util.spec_from_file_location("docs_endpoint_index", ROOT / "scripts/docs_endpoint_index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def index_module():
    return _load_index_module()


@pytest.fixture(scope="module")
def route_patterns(index_module):
    patterns = []
    for _method, path, _module in index_module.composed_routes():
        regex = re.escape(path)
        regex = re.sub(r"\\\{[a-z_]+:path\\\}", ".+", regex)
        regex = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", regex)
        patterns.append(re.compile("^" + regex + "$"))
    return patterns


def _is_route(path: str, patterns, prefixes) -> bool:
    candidate = path.rstrip("/") or "/"
    candidate = re.sub(r"\{[^}/]+\}", "x", candidate)
    # A bare prefix such as `/api/skills` documents a route family.
    return any(p.match(candidate) for p in patterns) or any(r.startswith(candidate + "/") for r in prefixes)


def _code(text: str) -> str:
    return "\n".join(block or span for block, span in CODE_RE.findall(text))


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_endpoint_index_is_generated_from_composed_app(index_module, lang):
    page = DOCS / lang / "api/00-endpoints.md"
    assert page.read_text() == index_module.render(lang), (
        f"regenerate: python scripts/docs_endpoint_index.py {lang} > docs/{lang}/api/00-endpoints.md"
    )


def test_documented_routes_exist(index_module, route_patterns):
    prefixes = [path for _method, path, _module in index_module.composed_routes()]
    missing = []
    for page in MAINTAINED:
        for match in ROUTE_RE.finditer(page.read_text()):
            path = match.group(1)
            if path.startswith(UPSTREAM_PREFIXES) or path.endswith("/*"):
                continue
            if not _is_route(path, route_patterns, prefixes):
                missing.append(f"{page.relative_to(ROOT)}: {path}")
    assert not missing, "documented routes not served by create_app:\n" + "\n".join(missing)


def test_documented_cli_commands_exist():
    from teamEvolver.cli import main

    runner = CliRunner()
    missing = []
    for page in MAINTAINED:
        for match in CLI_RE.finditer(_code(page.read_text())):
            words = match.group(1).split()
            command = main.commands.get(words[0])
            if command is None:
                missing.append(f"{page.relative_to(ROOT)}: teamEvolver {match.group(1)}")
                continue
            if len(words) > 1 and hasattr(command, "commands") and words[1] not in command.commands:
                missing.append(f"{page.relative_to(ROOT)}: teamEvolver {match.group(1)}")
    assert not missing, "documented CLI commands do not exist:\n" + "\n".join(missing)
    assert runner.invoke(main, ["--help"]).exit_code == 0


def test_documented_env_vars_are_read_by_code():
    sources = []
    for pattern in (
        "teamEvolver/**/*.py",
        "session_ingestion/**/*.py",
        "team_*/**/*.py",
        "container_start.sh",
        "cicd_scripts/*.sh",
        "scripts/*.sh",
        "scripts/*.py",
        "compose.yaml",
        "Dockerfile",
    ):
        sources.extend(ROOT.glob(pattern))
    code = "\n".join(p.read_text(errors="ignore") for p in sources if p.is_file())
    known = set(ENV_RE.findall(code))
    # Prefix families composed at runtime: TEAMEVOLVER_PG_* and TEAMEVOLVER_LOG_*.
    known |= {f"TEAMEVOLVER_PG_{k}" for k in ("HOST", "PORT", "DATABASE", "USERNAME", "PASSWORD", "SSL")}
    known |= {
        f"TEAMEVOLVER_LOG_{k}"
        for k in ("DIR", "LEVEL", "FILE_ENABLED", "CONSOLE_ENABLED", "MAX_FILE_MB", "RETENTION_DAYS", "MAX_TOTAL_MB")
    }
    missing = []
    for page in MAINTAINED:
        for name in sorted(set(ENV_RE.findall(page.read_text()))):
            if name not in known:
                missing.append(f"{page.relative_to(ROOT)}: {name}")
    assert not missing, "documented environment variables are not read anywhere:\n" + "\n".join(missing)


def test_bilingual_pages_mirror_headings():
    mismatched = []
    for zh in sorted((DOCS / "zh").rglob("*.md")):
        en = DOCS / "en" / zh.relative_to(DOCS / "zh")
        assert en.exists(), f"missing English mirror for {zh.relative_to(DOCS)}"
        count = [len(re.findall(r"^#{1,3} ", p.read_text(), re.M)) for p in (zh, en)]
        if count[0] != count[1]:
            mismatched.append(f"{zh.relative_to(DOCS)}: zh {count[0]} headings, en {count[1]}")
    assert not mismatched, "zh/en heading structure differs:\n" + "\n".join(mismatched)
