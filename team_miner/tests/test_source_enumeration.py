"""Source enumeration uses only the unchanged public OV single-directory pagination API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from team_miner import ov_round_orchestration, routes
from team_miner.ov_compile_mining import OvSkillMiningRepository
from team_miner.tests.test_ov_round_bridge import FakeOV, Owner, create

ROOT = "viking://resources/source"


@pytest.fixture
def source_owner(tmp_path):
    return Owner(OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner"), FakeOV())


class OriginalDirectoryTransport:
    def __init__(self, paths, *, root=ROOT, failure=""):
        self.root, self.failure, self.calls = root, failure, []
        self.children = {root: {}}
        for relative in paths:
            parts, directory = relative.split("/"), root
            for index, name in enumerate(parts):
                uri = directory + "/" + name
                is_dir = index < len(parts) - 1
                self.children.setdefault(directory, {})[uri] = {
                    "uri": uri,
                    "name": name,
                    "isDir": is_dir,
                    "size": 0 if is_dir else 10,
                }
                if is_dir:
                    self.children.setdefault(uri, {})
                directory = uri

    def respond(self, request):
        assert request.method == "GET" and request.url.path == "/api/v1/fs/ls"
        params = request.url.params
        assert params["recursive"] == "false" and params["output"] == "original"
        assert params["sort_by"] == "name" and params["sort_order"] == "asc"
        assert params["show_all_hidden"] == "false" and "level_limit" not in params
        uri, offset, limit = params["uri"], int(params["offset"]), int(params["node_limit"])
        self.calls.append((uri, offset, limit))
        if self.failure == "missing" or uri not in self.children:
            return httpx.Response(404, json={"detail": "NOT_FOUND"})
        if self.failure == "child_missing" and uri != self.root:
            return httpx.Response(404, json={"detail": "NOT_FOUND"})
        rows = sorted(self.children[uri].values(), key=lambda row: (not row["isDir"], row["name"].lower(), row["name"]))
        if self.failure == "ignored_offset":
            offset = 0
        result = rows[offset : offset + limit]
        if self.failure == "foreign":
            result = [{"uri": "viking://resources/foreign.md", "isDir": False}]
        elif self.failure == "recursive":
            result = [{"uri": uri + "/nested/file.md", "isDir": False}]
        elif self.failure == "denied":
            result = [{"uri": uri + "/private", "isDir": True, "access": "denied"}]
        elif self.failure == "truncated":
            result = {"entries": result, "truncated": True}
        elif self.failure == "invalid":
            result = {"not_entries": "an unsupported shape"}
        return httpx.Response(200, json={"status": "ok", "result": result})


class EnumerationOwner(routes.KnowledgeMiningMixin):
    def __init__(self, transport):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(transport.respond))

    def _knowledge_endpoint(self):
        return "http://inert-openviking.invalid"

    def _knowledge_headers(self, account, **_kwargs):
        return {"X-OpenViking-Account": account}

    def _knowledge_http(self):
        return self.client


def tree_result(remote, **kwargs):
    async def run():
        owner = EnumerationOwner(remote)
        try:
            return await owner._knowledge_tree("default", remote.root, **kwargs)
        finally:
            await owner.client.aclose()

    return asyncio.run(run())


@pytest.mark.parametrize("count", [2000, 2501])
def test_single_directory_is_paginated_until_an_empty_or_short_final_page(count):
    remote = OriginalDirectoryTransport([f"file-{index:05d}.md" for index in range(count)])
    tree = tree_result(remote)
    assert tree["enumeration_complete"] and tree["exists"]
    assert len(tree["entries"]) == count
    assert [row["uri"] for row in tree["entries"]] == [ROOT + f"/file-{index:05d}.md" for index in range(count)]
    assert [offset for _, offset, _ in remote.calls] == [0, 1000, 2000]


def test_deep_and_wide_tree_is_complete_even_above_the_old_total_node_limit():
    paths = [f"group-{group:03d}/file-{index:03d}.md" for group in range(101) for index in range(99)]
    paths.append("/".join([f"level-{index:02d}" for index in range(35)]) + "/last.md")
    remote = OriginalDirectoryTransport(paths)
    tree = tree_result(remote)
    files = [row["uri"] for row in tree["entries"] if not row["is_dir"]]
    assert len(tree["entries"]) > 10000 and len(files) == 10000
    assert set(files) == {ROOT + "/" + path for path in paths}
    assert tree["enumeration_complete"] and all(limit == 1000 for _, _, limit in remote.calls)


@pytest.mark.parametrize("failure", ["ignored_offset", "foreign", "recursive", "denied", "truncated", "invalid"])
def test_bad_or_incomplete_directory_pages_never_claim_complete_enumeration(failure):
    remote = OriginalDirectoryTransport([f"file-{index:05d}.md" for index in range(1001)], failure=failure)
    with pytest.raises(HTTPException) as error:
        tree_result(remote)
    assert error.value.status_code in {403, 502}
    assert len(remote.calls) <= 2


def test_missing_root_is_distinct_from_a_child_removed_during_enumeration():
    assert tree_result(OriginalDirectoryTransport([], failure="missing"))["exists"] is False
    with pytest.raises(HTTPException) as error:
        tree_result(OriginalDirectoryTransport(["folder/file.md"], failure="child_missing"))
    assert error.value.status_code == 404


def test_hidden_subtrees_and_excluded_wiki_are_not_traversed():
    remote = OriginalDirectoryTransport([".hidden/private.md", "visible.md", "wiki/generated.md"])
    previous = routes._WIKI_ROOT
    routes._WIKI_ROOT = ROOT + "/wiki"
    try:
        tree = tree_result(remote, exclude_wiki=True)
    finally:
        routes._WIKI_ROOT = previous
    assert [row["uri"] for row in tree["entries"]] == [ROOT + "/visible.md"]
    assert [uri for uri, _, _ in remote.calls] == [ROOT]


def test_round_freezer_scans_nested_sources_and_reuses_the_exact_cached_manifest(source_owner):
    rig = source_owner
    deep = "viking://resources/team/source/" + "/".join(f"d-{i}" for i in range(30)) + "/policy.md"
    rig.remote.content[deep] = "后段规则与例外都应进入冻结来源。"
    job = create(rig)

    async def run():
        first = await rig._freeze_round_inputs(job, staged=True)
        rig.remote.content[deep] = "后来修改的内容不应改变已冻结来源。"
        second = await rig._freeze_round_inputs(job, staged=True)
        return first, second

    first, second = asyncio.run(run())
    assert first == second and len(first) == 2
    paths = {row["origin_uri"]: row["local_path"] for row in first}
    assert Path(paths[deep]).read_text() == "后段规则与例外都应进入冻结来源。"


def test_round_freezer_preserves_the_explicit_file_admission_instead_of_accepting_a_partial_list(
    source_owner, monkeypatch
):
    rig = source_owner
    monkeypatch.setattr(ov_round_orchestration, "MAX_INPUT_FILES", 2)
    rig.remote.content.update({"viking://resources/team/source/a.md": "A", "viking://resources/team/source/b.md": "B"})
    job = create(rig)
    with pytest.raises(ValueError, match="没有接受截断"):
        asyncio.run(rig._freeze_round_inputs(job, staged=True))
    root = rig.repository._job_dir(job["job_id"]) / "round_inputs/round-1"
    assert not (root / "manifest.json").exists()
