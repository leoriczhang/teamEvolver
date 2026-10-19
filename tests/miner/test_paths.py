"""Miner current boundaries and Compile protocol consistency."""

import pytest

from team_miner.schemas import relative_path, uri
from team_miner.state import digest


@pytest.mark.parametrize("path", ["/a.md", "../a.md", ".hidden/a.md", "a/..", "a/%2e%2e/b", "a\\b", "C:/a"])
def test_miner_upload_rejects_workspace_escape(path):
    with pytest.raises(ValueError):
        relative_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "viking://resources/input/../a",
        "viking://resources/input/.hidden",
        "viking://resources/input/%2e%2e/a",
        "/tmp/a",
        "viking://resources/other/a",
    ],
)
def test_miner_uri_rejects_hidden_and_escape(path):
    with pytest.raises(ValueError):
        uri(path, "viking://resources/input")


def test_receipt_identity_is_deterministic():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_memory_and_miner_use_one_compile_implementation():
    import team_memory.openviking as memory
    import team_miner.openviking as miner
    from teamEvolver.openviking.compile import CompileClient

    assert memory.CompileClient is miner.CompileClient is CompileClient
