"""release guardrails: one dependency source, hashed locks, wheel-only runtime."""

import re

import tomllib

from tests.support.paths import REPO_ROOT

ROOT = REPO_ROOT
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


def _name(requirement):
    return re.split(r"[\s\[<>=!~;]", requirement, 1)[0].lower().replace("_", "-")


def _locked(path):
    text = (ROOT / path).read_text()
    pins = re.findall(r"^([A-Za-z0-9_.\-]+)==\S+", text, re.M)
    return text, {p.lower().replace("_", "-") for p in pins}


def test_pyproject_is_the_only_direct_dependency_source():
    for retired in ("requirements.txt", "install.sh", "docker/build-requirements.in", "docker/entrypoint.sh"):
        assert not (ROOT / retired).exists(), retired
    deps = {_name(r) for r in PROJECT["project"]["dependencies"]}
    assert "asyncpg" in deps, "PostgreSQL driver is a required dependency"
    assert set(PROJECT["project"]["optional-dependencies"]) == {"dev"}


def test_locks_are_hashed_and_cover_pyproject():
    runtime, pinned = _locked("requirements.lock")
    build, build_pinned = _locked("build-requirements.lock")
    for text in (runtime, build):
        blocks = [b for b in re.split(r"\n(?=[A-Za-z])", text) if re.match(r"[A-Za-z0-9_.\-]+==", b)]
        assert blocks and all("--hash=sha256:" in b for b in blocks)
    direct = {_name(r) for r in PROJECT["project"]["dependencies"] if "python_version < '3.11'" not in r}
    assert direct <= pinned, direct - pinned
    assert {_name(r) for r in PROJECT["build-system"]["requires"]} <= build_pinned


def test_wheel_carries_schema_and_console_assets():
    data = PROJECT["tool"]["setuptools"]["package-data"]["teamEvolver"]
    assert "database/schema/*.sql" in data and "web/dist/assets/*" in data


def test_runtime_image_installs_only_the_release():
    dockerfile = "\n".join(
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert "PYTHONPATH" not in dockerfile
    assert "COPY release/" in dockerfile and "install.sh" in dockerfile
    assert not re.search(r"^COPY (teamEvolver|team_|session_ingestion)", dockerfile, re.M)
    assert re.search(r"apt-get install[^\n]*\bgit\b", dockerfile)
    rules = [line for line in (ROOT / ".dockerignore").read_text().splitlines() if line and not line.startswith("#")]
    assert rules == ["*", "!release/"], "build context is the release only"
    assert "PYTHONPATH" not in (ROOT / "compose.yaml").read_text()


def test_container_start_requires_an_explicit_production_role():
    script = (ROOT / "container_start.sh").read_text()
    assert "api|worker) ;;" in script and "exit 64" in script
    assert "TEAMEVOLVER_ROLE:-api" not in script


def test_offline_install_verifies_manifest_and_hashes():
    install = (ROOT / "cicd_scripts/install.sh").read_text()
    assert "manifest.json" in install and install.count("--no-index") == 3
    assert install.count("--require-hashes") == 2 and "pip check" in install
