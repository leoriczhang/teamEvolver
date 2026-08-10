from __future__ import annotations

import pytest

from teamEvolver.evolve.kernel.bundle_changes import (
    BundleChangeError,
    materialize_bundle_changes,
    select_editable_files,
)
from teamEvolver.skills.bundle import (
    SkillBundleError,
    candidate_skill_bundle,
    decode_bundle_payload,
    encode_bundle_payload,
)


def _skill() -> dict[str, str]:
    return {
        "name": "demo",
        "description": "Demo skill",
        "category": "general",
        "content": "# Demo\n\nRun the bundled scripts.",
    }


def test_bundle_payload_round_trips_text_and_binary() -> None:
    bundle = {
        "SKILL.md": b"# Demo\n",
        "scripts/run.py": b"print('ok')\n",
        "assets/logo.bin": b"\x00\xff\x10",
    }
    payload = encode_bundle_payload(bundle)

    assert decode_bundle_payload(payload) == bundle
    assert payload["tree_sha256"]
    assert {
        item["encoding"] for item in payload["files"]
    } == {"utf-8", "base64"}


def test_bundle_payload_rejects_tampered_content() -> None:
    payload = encode_bundle_payload({"SKILL.md": b"# Demo\n"})
    payload["files"][0]["content"] = "# Changed\n"

    with pytest.raises(SkillBundleError, match="mismatch"):
        decode_bundle_payload(payload)


def test_legacy_bundle_files_are_preserved() -> None:
    skill = {
        **_skill(),
        "bundle_files": {
            "scripts/run.py": "print('legacy')\n",
            "references/readme.txt": "legacy\n",
        },
    }

    bundle = candidate_skill_bundle(skill)

    assert bundle["scripts/run.py"] == b"print('legacy')\n"
    assert bundle["references/readme.txt"] == b"legacy\n"
    assert b"name: demo" in bundle["SKILL.md"]


def test_select_editable_files_prioritizes_referenced_paths() -> None:
    selected = select_editable_files(
        {
            "scripts/large.py": b"x" * 20,
            "scripts/used.py": b"print(1)\n",
            "scripts/other.sh": b"echo ok\n",
            "assets/logo.bin": b"\xff\x00",
        },
        extensions=[".py", ".sh"],
        max_file_bytes=100,
        max_prompt_bytes=10,
        priority_paths=["scripts/used.py"],
    )

    assert selected == {"scripts/used.py": "print(1)\n"}


def test_materialize_upsert_delete_and_preserve_binary() -> None:
    current = {
        "SKILL.md": b"old",
        "scripts/run.py": b"print('old')\n",
        "scripts/legacy.sh": b"echo legacy\n",
        "assets/logo.bin": b"\x00\xff",
    }
    candidate, diff = materialize_bundle_changes(
        _skill(),
        current_bundle=current,
        file_changes=[
            {
                "path": "scripts/run.py",
                "operation": "upsert",
                "content": "print('new')\n",
                "reason": "Fix repeated behavior.",
            },
            {
                "path": "scripts/legacy.sh",
                "operation": "delete",
                "reason": "Remove obsolete entrypoint.",
            },
            {
                "path": "scripts/check.sh",
                "operation": "upsert",
                "content": "echo ok\n",
                "reason": "Add deterministic check.",
            },
        ],
        editable_paths=["scripts/run.py", "scripts/legacy.sh"],
        extensions=[".py", ".sh"],
        max_file_bytes=1024,
        allow_delete=True,
    )
    bundle = candidate_skill_bundle(candidate)

    assert bundle["scripts/run.py"] == b"print('new')\n"
    assert bundle["scripts/check.sh"] == b"echo ok\n"
    assert "scripts/legacy.sh" not in bundle
    assert bundle["assets/logo.bin"] == b"\x00\xff"
    assert diff["changed_count"] == 4  # SKILL.md + three explicit operations.


@pytest.mark.parametrize(
    "change,match",
    [
        (
            {
                "path": "../escape.py",
                "operation": "upsert",
                "content": "pass\n",
                "reason": "bad",
            },
            "Unsafe bundle path",
        ),
        (
            {
                "path": "SKILL.md",
                "operation": "delete",
                "reason": "bad",
            },
            "SKILL.md",
        ),
        (
            {
                "path": "scripts/run.py",
                "operation": "delete",
                "reason": "bad",
            },
            "deletion is disabled",
        ),
    ],
)
def test_materialize_rejects_unsafe_changes(change: dict, match: str) -> None:
    with pytest.raises(BundleChangeError, match=match):
        materialize_bundle_changes(
            _skill(),
            current_bundle={"scripts/run.py": b"pass\n"},
            file_changes=[change],
            editable_paths=["scripts/run.py"],
            extensions=[".py", ".sh"],
            max_file_bytes=1024,
            allow_delete=False,
        )
