"""Exercise visible knowledge-source workflows through the production Worker operations."""

from __future__ import annotations

import asyncio

import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner.core_repository import OvSkillMiningRepository
from team_miner.skill_miner.source_operations import execute, safe_uri
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.support.mining_sources import (
    SourceWorkspaceRemote,
    office_upload,
    source_upload,
)

ROOT = "viking://resources/source-lifecycle"


@pytest.fixture
def workspace(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "cache", backend=MemoryMetadataBackend())
    return SourceWorkspaceRemote(repository, ROOT)


def run(remote, operation, **body):
    return asyncio.run(execute(operation, body, remote))


def test_registered_source_upload_edit_delete_and_unlink_preserve_remote_directory(workspace):
    created = run(workspace, "source.create", uri=ROOT, tags=["skill"])["source"]
    assert created["status"] == "empty"
    identifier = created["source_id"]
    assert run(workspace, "source.update", source_id=identifier, tags=["knowledge"])["source"]["tags"] == ["knowledge"]
    updated = run(workspace, "source.update", source_id=identifier, tags=["knowledge", "skill"])
    assert updated["source"]["tags"] == ["skill", "knowledge"]
    uploaded = run(workspace, "source.upload", source_id=identifier, files=[
        source_upload("folder/lesson.txt", "Confirm the issue."),
        source_upload("rules.md", "# Escalation\nRecord the case."),
    ])
    assert uploaded["file_count"] == 2
    duplicate = run(workspace, "source.upload", source_id=identifier,
                    files=[source_upload("rules.md", "# Another case")])
    assert duplicate["written"][0]["uri"] == ROOT + "/rules-2.md"
    detail = run(workspace, "source.detail", source_id=identifier)["source"]
    assert detail["ready"] and detail["document_count"] == 3
    assert detail["total_bytes"] == sum(len(value.encode()) for value in workspace.files.values())
    tree = run(workspace, "source.tree", source_id=identifier)
    assert {entry["uri"] for entry in tree["entries"]} == {ROOT + "/folder", *workspace.files}
    assert "tree" not in tree["source"]
    uri = ROOT + "/folder/lesson.md"
    original = run(workspace, "source.content", source_id=identifier, uri=uri)
    assert original["editable"] and "source_format: txt" in original["content"]
    run(workspace, "source.save", source_id=identifier, uri=uri,
        original_content=original["content"], content="# Revised\n")
    assert run(workspace, "source.content", source_id=identifier, uri=uri)["content"] == "# Revised\n"
    with pytest.raises(HTTPException) as conflict:
        run(workspace, "source.save", source_id=identifier, uri=uri,
            original_content=original["content"], content="# Stale\n")
    assert conflict.value.status_code == 409
    run(workspace, "source.delete", source_id=identifier, uri=ROOT + "/folder")
    assert uri not in workspace.files and ROOT + "/folder" not in workspace.directories
    run(workspace, "source.remove", source_id=identifier)
    assert workspace.repository.list_source_records() == []
    assert ROOT in workspace.directories and workspace.files[ROOT + "/rules.md"].startswith("# Escalation")


@pytest.mark.parametrize("suffix", [".md", ".markdown", ".txt", ".csv", ".docx", ".xlsx", ".pptx", ".pdf"])
def test_browser_supported_formats_convert_and_can_be_read_back(workspace, suffix):
    raw = {".md": "# Confirm the issue", ".markdown": "# Confirm the issue", ".txt": "Confirm the issue",
           ".csv": "step,action\n1,Confirm the issue"}.get(suffix)
    if raw is None:
        raw = office_upload(suffix)
    source = run(workspace, "source.create", uri=ROOT)["source"]
    uploaded = run(workspace, "source.upload", source_id=source["source_id"],
                   files=[source_upload("lesson" + suffix, raw)])
    assert uploaded["file_count"] == 1
    body = run(workspace, "source.content", source_id=source["source_id"], uri=ROOT + "/lesson.md")
    assert "Confirm the issue" in body["content"]
    assert body["editable"]


def test_source_list_reports_missing_and_unavailable_without_hiding_good_sources(workspace):
    ready = workspace.repository.save_source("team", ROOT)
    workspace.files[ROOT + "/ready.md"] = "# Ready"
    missing = workspace.repository.save_source("team", ROOT + "-missing")
    unavailable = workspace.repository.save_source("team", ROOT + "-unavailable")
    workspace.unavailable_roots.add(unavailable["uri"])
    listed = {row["source_id"]: row for row in run(workspace, "source.list")["sources"]}
    assert listed[ready["source_id"]]["ready"]
    assert listed[missing["source_id"]]["status"] == "missing"
    assert listed[unavailable["source_id"]]["status"] == "unavailable"
    default = next(row for row in listed.values() if row["default"])
    with pytest.raises(ValueError, match="默认"):
        run(workspace, "source.remove", source_id=default["source_id"])
    with pytest.raises(ValueError, match="默认"):
        run(workspace, "source.update", source_id=default["source_id"], tags=["skill"])


@pytest.mark.parametrize("uri", ["viking://resources/source-lifecycle/../other", "viking://resources/.hidden",
                                "viking://resources/source/%2e%2e/file", "viking://resources/source\\file"])
def test_source_path_rejects_hidden_encoded_and_traversal_uris(uri):
    with pytest.raises(ValueError):
        safe_uri(uri)


def test_source_worker_rejects_cross_account_and_outside_directory_before_io(workspace):
    record = workspace.repository.save_source("other", ROOT)
    with pytest.raises(HTTPException) as denied:
        run(workspace, "source.tree", source_id=record["source_id"])
    assert denied.value.status_code == 403 and workspace.calls == []
    record = workspace.repository.save_source("team", ROOT)
    with pytest.raises(ValueError, match="目录内"):
        run(workspace, "source.delete", source_id=record["source_id"], uri=ROOT + "-neighbor/file.md")
    assert workspace.calls == []


@pytest.mark.parametrize("files", [[], [source_upload("empty.txt", "")],
                                   [{"name": "invalid.txt", "content_b64": "!!!"}],
                                   [source_upload("unsupported.exe", "source")],
                                   [source_upload(f"{index}.md", "source") for index in range(51)]])
def test_invalid_upload_batches_fail_before_creating_any_remote_content(workspace, files):
    source = run(workspace, "source.create", uri=ROOT)["source"]
    workspace.calls.clear()
    with pytest.raises(ValueError):
        run(workspace, "source.upload", source_id=source["source_id"], files=files)
    assert workspace.calls == [] and workspace.files == {}


def test_missing_registration_and_empty_or_unknown_purpose_fail_without_changing_registry(workspace):
    with pytest.raises(HTTPException) as absent:
        run(workspace, "source.create", uri=ROOT + "-missing")
    assert absent.value.status_code == 404
    assert workspace.repository.list_source_records() == []
    source = run(workspace, "source.create", uri=ROOT)["source"]
    for tags in ([], ["arbitrary-purpose"]):
        with pytest.raises(ValueError):
            run(workspace, "source.update", source_id=source["source_id"], tags=tags)
        assert workspace.repository.get_source(source["source_id"])["tags"] == ["skill"]


def test_read_only_and_oversized_edits_do_not_write_remote_content(workspace):
    source = run(workspace, "source.create", uri=ROOT)["source"]
    workspace.files[ROOT + "/readonly.bin"] = "read-only content"
    workspace.files[ROOT + "/editable.md"] = "# Original"
    read_only = run(workspace, "source.content", source_id=source["source_id"], uri=ROOT + "/readonly.bin")
    assert not read_only["editable"]
    with pytest.raises(ValueError):
        run(workspace, "source.save", source_id=source["source_id"], uri=read_only["uri"],
            original_content=read_only["content"], content="modified")
    with pytest.raises(HTTPException) as large:
        run(workspace, "source.save", source_id=source["source_id"], uri=ROOT + "/editable.md",
            original_content="# Original", content="x" * (2 * 1024 * 1024 + 1))
    assert large.value.status_code == 413
    assert not any(method == "POST" and path.endswith("/write") for method, path, _ in workspace.calls)
    assert workspace.files[ROOT + "/editable.md"] == "# Original"
