"""Offline Git round trip using only the installed runtime and a disposable repo."""
import json
import subprocess
import tempfile
from pathlib import Path

from team_skills.transfer.git import GitAdapter
from team_skills.transfer.packages import package


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=RuntimeCheck", "-c", "user.email=check@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    remote, seed = root / "remote.git", root / "seed"
    seed.mkdir()
    git(seed, "init", "--bare", str(remote))
    git(seed, "init", "-b", "main")
    skill = seed / "nested/skills/demo"
    skill.mkdir(parents=True)
    markdown = b"---\nname: demo\ndescription: Runtime verification\n---\nfirst\n"
    (skill / "SKILL.md").write_bytes(markdown)
    (seed / "README").write_text("preserved")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "seed")
    revision = git(seed, "rev-parse", "HEAD")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "main")
    adapter = GitAdapter(allow_local=True)
    options = {"url": str(remote), "branch": "main", "commit": revision, "path": "nested/skills"}
    assert adapter.read(options)[0].files["SKILL.md"] == markdown
    output = package("demo", {"SKILL.md": markdown.replace(b"first", b"exported")})
    result = adapter.write([output], {**options, "new_branch": "runtime-result"})
    assert git(remote, "show", "main:README") == git(remote, "show", "runtime-result:README") == "preserved"
    assert adapter.read({**options, "branch": "runtime-result", "commit": ""})[0].files == output.files
    # Hosted creation is separately tested using each provider's HTTP contract.
    # Here an empty bare repository stands in for the newly created remote.
    created = root / "created.git"
    git(seed, "init", "--bare", str(created))
    adapter._create_repository = lambda options: (str(created), "https://git.example.test/new")
    adapter.write([output], {"mode": "new_repository"})
    assert git(created, "show", "main:skills/demo/SKILL.md").endswith("exported")
    print(json.dumps({"git": git(seed, "--version"), "revision_subdirectory": True,
                      "new_branch": result.metadata["branch"], "new_repository": True}))
