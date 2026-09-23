from __future__ import annotations

import asyncio
import base64
import io
import zipfile
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from teamEvolver.proxy import knowledge_mining as knowledge_mining_module
from teamEvolver.proxy.knowledge_document_converter import convert_knowledge_upload
from teamEvolver.proxy.knowledge_mining import (
    _SOURCE_ROOT,
    _WIKI_ROOT,
    KnowledgeMiningMixin,
    _build_wiki_link_index,
    _content_editable,
    _extract_markdown_links,
    _knowledge_compile_progress,
    _knowledge_directory_uri,
    _knowledge_file_uri,
    _knowledge_wiki_root,
    _normalize_entries,
    _resolve_wiki_link,
    _safe_relative_path,
)
from teamEvolver.proxy.wiki_graph import build_wiki_graph, render_wiki_graph_html
from teamEvolver.storage import LocalObjectStore


def test_source_tree_excludes_wiki_output_subtree() -> None:
    entries = _normalize_entries(
        {
            "entries": [
                {"uri": f"{_SOURCE_ROOT}/docs", "name": "docs", "isDir": True},
                {"uri": f"{_SOURCE_ROOT}/docs/guide.md", "name": "guide.md", "isDir": False},
                {"uri": _WIKI_ROOT, "name": "output", "isDir": True},
                {"uri": f"{_WIKI_ROOT}/index.md", "name": "index.md", "isDir": False},
            ]
        },
        excluded_root=_WIKI_ROOT,
    )

    assert [entry["uri"] for entry in entries] == [
        f"{_SOURCE_ROOT}/docs",
        f"{_SOURCE_ROOT}/docs/guide.md",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("guide.md", "guide.md"),
        ("docs/guide.md", "docs/guide.md"),
        ("docs\\guide.md", "docs/guide.md"),
    ],
)
def test_safe_relative_path_preserves_folder_uploads(raw: str, expected: str) -> None:
    assert _safe_relative_path(raw) == expected


@pytest.mark.parametrize("raw", ["", "../secret", "docs/../secret", ".hidden", "docs/.hidden"])
def test_safe_relative_path_rejects_unsafe_paths(raw: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _safe_relative_path(raw)
    assert exc_info.value.status_code == 400


def test_knowledge_upload_converter_normalizes_text_and_csv_to_markdown() -> None:
    text = convert_knowledge_upload("说明.txt", "第一行\n第二行".encode())
    csv_file = convert_knowledge_upload("规则.csv", "场景,时限\n退款,7 天\n".encode())

    assert text.source_format == "txt"
    assert 'source_file: "说明.txt"' in text.markdown
    assert "# 说明" in text.markdown
    assert "第一行\n第二行" in text.markdown
    assert csv_file.source_format == "csv"
    assert "| 场景 | 时限 |" in csv_file.markdown
    assert "| 退款 | 7 天 |" in csv_file.markdown


def test_knowledge_upload_converter_extracts_pptx_slide_text() -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><a:t>季度复盘</a:t><a:t>收入增长 20%</a:t></p:cSld></p:sld>",
        )

    converted = convert_knowledge_upload("复盘.pptx", payload.getvalue())

    assert converted.source_format == "pptx"
    assert "## 第 1 页" in converted.markdown
    assert "季度复盘" in converted.markdown
    assert "收入增长 20%" in converted.markdown


def test_knowledge_upload_converter_rejects_unsupported_legacy_office() -> None:
    with pytest.raises(ValueError, match="不支持 .doc 文件"):
        convert_knowledge_upload("legacy.doc", b"legacy")


@pytest.mark.parametrize(
    ("kind", "expected_root"),
    [
        ("source", _SOURCE_ROOT),
        ("wiki", _WIKI_ROOT),
    ],
)
def test_knowledge_upload_converts_before_openviking_write(
    monkeypatch,
    kind: str,
    expected_root: str,
) -> None:
    calls: list[tuple[str, dict]] = []
    scheduled: list[tuple[str, str, float]] = []

    class Owner(KnowledgeMiningMixin):
        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_request(self, _account, _method, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/v1/fs/mkdir":
                return {"created": True}
            if path == "/api/v1/content/write":
                return {"task_id": "write_123"}
            raise AssertionError(f"unexpected path: {path}")

        def _start_knowledge_link_rebuild(self, account, *, wiki_root=_WIKI_ROOT, delay=0.0):
            scheduled.append((account, wiki_root, delay))

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    app = FastAPI()
    Owner()._register_knowledge_mining_routes(app)
    client = TestClient(app)

    response = client.post(
        "/api/knowledge-mining/upload",
        json={
            "account": "demo",
            "kind": kind,
            "wiki_root": _WIKI_ROOT,
            "files": [
                {
                    "name": "guide.txt",
                    "relative_path": "guide.txt",
                    "content_b64": base64.b64encode("关键事实".encode()).decode(),
                }
            ],
        },
    )

    assert response.status_code == 202
    assert response.json()["files"][0]["markdown_path"] == "guide.md"
    write = next(call for call in calls if call[0] == "/api/v1/content/write")
    assert write[1]["json"]["uri"] == f"{expected_root}/guide.md"
    assert write[1]["json"]["mode"] == "create"
    assert write[1]["json"]["wait"] is False
    assert "source_format: txt" in write[1]["json"]["content"]
    assert "关键事实" in write[1]["json"]["content"]
    assert len([call for call in calls if call[0] == "/api/v1/content/write"]) == 1
    assert not any(path.startswith("/api/v1/resources") for path, _kwargs in calls)
    assert scheduled == ([('demo', _WIKI_ROOT, 0.5)] if kind == "wiki" else [])


def test_knowledge_upload_preserves_folder_below_selected_directory(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class Owner(KnowledgeMiningMixin):
        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_request(self, _account, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"ok": True}

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    app = FastAPI()
    Owner()._register_knowledge_mining_routes(app)
    parent = f"{_SOURCE_ROOT}/selected"

    response = TestClient(app).post(
        "/api/knowledge-mining/upload",
        json={
            "account": "demo",
            "kind": "source",
            "parent_uri": parent,
            "files": [
                {
                    "name": "guide.txt",
                    "relative_path": "docs/nested/guide.txt",
                    "content_b64": base64.b64encode("关键事实".encode()).decode(),
                }
            ],
        },
    )

    assert response.status_code == 202
    mkdir_uris = [call[2]["json"]["uri"] for call in calls if call[1] == "/api/v1/fs/mkdir"]
    assert mkdir_uris == [parent, f"{parent}/docs", f"{parent}/docs/nested"]
    write = next(call for call in calls if call[1] == "/api/v1/content/write")
    assert write[2]["json"]["uri"] == f"{parent}/docs/nested/guide.md"


def test_knowledge_directory_uri_accepts_root_and_rejects_escape() -> None:
    assert _knowledge_directory_uri("source", None) == _SOURCE_ROOT
    assert _knowledge_directory_uri("source", f"{_SOURCE_ROOT}/docs") == f"{_SOURCE_ROOT}/docs"
    with pytest.raises(HTTPException):
        _knowledge_directory_uri("source", f"{_WIKI_ROOT}/docs")


def test_knowledge_entry_delete_calls_openviking_fs(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class Owner(KnowledgeMiningMixin):
        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_request(self, _account, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return {"deleted": True}

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    app = FastAPI()
    Owner()._register_knowledge_mining_routes(app)
    target = f"{_SOURCE_ROOT}/docs"

    response = TestClient(app).request(
        "DELETE",
        "/api/knowledge-mining/entry",
        json={"account": "demo", "kind": "source", "uri": target},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert calls == [("DELETE", "/api/v1/fs", {"timeout": 180.0, "params": {"uri": target}})]


def test_pdf_upload_produces_exactly_one_markdown_write(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    converted: list[tuple[str, bytes]] = []

    def convert_pdf(filename: str, raw: bytes):
        converted.append((filename, raw))
        return SimpleNamespace(
            markdown="# policy\n\n## page 1\n\nOne source, one Markdown\n",
            source_format="pdf",
            source_encoding="binary",
        )

    class Owner(KnowledgeMiningMixin):
        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_request(self, _account, _method, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/v1/fs/mkdir":
                return {"created": True}
            if path == "/api/v1/content/write":
                return {"task_id": "write_pdf"}
            raise AssertionError(f"resource parser must not be called: {path}")

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    monkeypatch.setattr(knowledge_mining_module, "convert_knowledge_upload", convert_pdf)
    app = FastAPI()
    Owner()._register_knowledge_mining_routes(app)

    response = TestClient(app).post(
        "/api/knowledge-mining/upload",
        json={
            "account": "demo",
            "kind": "source",
            "files": [
                {
                    "name": "policy.pdf",
                    "relative_path": "policy.pdf",
                    "content_b64": base64.b64encode(b"pdf payload").decode(),
                }
            ],
        },
    )

    assert response.status_code == 202
    writes = [kwargs["json"] for path, kwargs in calls if path == "/api/v1/content/write"]
    assert len(writes) == 1
    assert writes[0]["uri"] == f"{_SOURCE_ROOT}/policy.md"
    assert "One source, one Markdown" in writes[0]["content"]
    assert converted == [("policy.pdf", b"pdf payload")]
    assert not any(path.startswith("/api/v1/resources") for path, _kwargs in calls)


@pytest.mark.parametrize(
    ("kind", "uri"),
    [
        ("source", f"{_SOURCE_ROOT}/docs/guide.md"),
        ("wiki", f"{_WIKI_ROOT}/index.md"),
    ],
)
def test_knowledge_file_uri_accepts_files_inside_selected_workspace(kind: str, uri: str) -> None:
    assert _knowledge_file_uri(kind, uri) == uri


@pytest.mark.parametrize(
    ("kind", "uri"),
    [
        ("source", _SOURCE_ROOT),
        ("source", f"{_WIKI_ROOT}/index.md"),
        ("wiki", f"{_SOURCE_ROOT}/docs/guide.md"),
        ("wiki", f"{_WIKI_ROOT}/../secret.md"),
        ("other", f"{_SOURCE_ROOT}/docs/guide.md"),
    ],
)
def test_knowledge_file_uri_rejects_workspace_escape(kind: str, uri: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _knowledge_file_uri(kind, uri)
    assert exc_info.value.status_code == 400


def test_historical_wiki_root_can_scope_files_without_escaping_resources() -> None:
    root = "viking://resources/knowledge-mining/run-123/wiki"

    assert _knowledge_wiki_root(root + "/") == root
    assert _knowledge_file_uri("wiki", root + "/index.md", wiki_root=root) == root + "/index.md"

    with pytest.raises(HTTPException):
        _knowledge_wiki_root("viking://resources/knowledge-mining/../secret")
    with pytest.raises(HTTPException):
        _knowledge_file_uri("wiki", f"{_WIKI_ROOT}/index.md", wiki_root=root)


def test_knowledge_workspace_uses_dedicated_input_and_output_roots() -> None:
    assert _SOURCE_ROOT == "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base"
    assert _WIKI_ROOT == "viking://resources/agent_knowledge_workspace/output/processed_knowledge"


def test_compile_progress_prefers_reported_counters_and_falls_back_to_real_stage() -> None:
    reported = _knowledge_compile_progress(
        {"status": "running", "stage": "agent", "progress": {"completed": 3, "total": 4}}
    )
    staged = _knowledge_compile_progress({"status": "running", "stage": "candidate_knowledge"})

    assert reported == {
        "percent": 75.0,
        "mode": "reported",
        "stage": "agent",
        "label": "agent",
    }
    assert staged == {
        "percent": 56,
        "mode": "stage",
        "stage": "candidate_knowledge",
        "label": "提炼候选知识",
    }


def test_knowledge_editor_only_allows_openviking_text_extensions() -> None:
    assert _content_editable(f"{_WIKI_ROOT}/index.md") is True
    assert _content_editable("viking://resources/data.json") is True
    assert _content_editable("viking://resources/manual.pdf") is False


def test_compile_request_targets_ov_api_with_account_and_idempotency_headers(monkeypatch) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, **kwargs):
            captured.update({"method": method, "url": url, **kwargs})
            return httpx.Response(
                202,
                json={"status": "ok", "result": {"task_id": "cmp_test", "status": "pending"}},
                request=httpx.Request(method, url),
            )

    monkeypatch.setattr(knowledge_mining_module.httpx, "AsyncClient", FakeClient)
    owner = KnowledgeMiningMixin()
    owner.config = SimpleNamespace(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_team_api_key="secret",
        sharing_viking_user="team",
    )

    result = asyncio.run(
        owner._knowledge_request(
            "demo-account",
            "POST",
            "/api/v1/compile",
            extra_headers={"Idempotency-Key": "te-km-1234567890123456"},
            json={
                "from": [_SOURCE_ROOT],
                "to": _WIKI_ROOT,
                "skill": "viking://agent/skills/llm-wiki",
                "instruction": "生成 Wiki",
            },
        )
    )

    assert result["task_id"] == "cmp_test"
    assert captured["method"] == "POST"
    assert captured["url"] == "https://openviking.example/api/v1/compile"
    assert captured["headers"]["X-OpenViking-Account"] == "demo-account"
    assert captured["headers"]["Idempotency-Key"] == "te-km-1234567890123456"
    assert captured["json"]["from"] == [_SOURCE_ROOT]
    assert captured["json"]["to"] == _WIKI_ROOT


def test_knowledge_requests_reuse_one_keepalive_client(monkeypatch) -> None:
    instances: list[object] = []
    requests: list[str] = []

    class FakeClient:
        is_closed = False

        def __init__(self, **_kwargs):
            instances.append(self)

        async def request(self, method, url, **_kwargs):
            requests.append(url)
            return httpx.Response(
                200,
                json={"status": "ok", "result": {"method": method}},
                request=httpx.Request(method, url),
            )

        async def aclose(self):
            self.is_closed = True

    monkeypatch.setattr(knowledge_mining_module.httpx, "AsyncClient", FakeClient)
    owner = KnowledgeMiningMixin()
    owner.config = SimpleNamespace(
        sharing_viking_endpoint="https://openviking.example",
        sharing_viking_team_api_key="secret",
        sharing_viking_user="team",
    )

    async def scenario():
        await owner._knowledge_request("demo", "GET", "/api/v1/first")
        await owner._knowledge_request("demo", "GET", "/api/v1/second")
        await owner.aclose_knowledge_http()

    asyncio.run(scenario())

    assert len(instances) == 1
    assert requests == [
        "https://openviking.example/api/v1/first",
        "https://openviking.example/api/v1/second",
    ]


def test_local_trusted_mode_sends_configured_server_key() -> None:
    owner = KnowledgeMiningMixin()
    owner.config = SimpleNamespace(
        sharing_viking_deployment="local",
        sharing_viking_team_api_key="root-key",
        sharing_viking_user="team",
    )

    headers = owner._knowledge_headers("default")

    assert headers["X-OpenViking-Account"] == "default"
    assert headers["X-OpenViking-User"] == "team"
    assert headers["X-API-Key"] == "root-key"
    assert headers["Authorization"] == "Bearer root-key"


def test_compile_capability_probe_allows_older_openviking() -> None:
    class Owner(KnowledgeMiningMixin):
        async def _knowledge_request(self, *_args, **_kwargs):
            raise HTTPException(status_code=404, detail="page not found")

    result = asyncio.run(Owner()._knowledge_compile_capabilities("demo-account"))

    assert result["can_create"] is True
    assert result["probe_supported"] is False
    assert result["reason_code"] == "CAPABILITY_PROBE_UNAVAILABLE"


def test_knowledge_compile_passes_only_the_last_successful_compile_time(tmp_path, monkeypatch) -> None:
    compile_requests: list[dict] = []

    class FakeCompileClient:
        def __init__(self, **_kwargs):
            pass

        async def publish_shared_skill(self, **_kwargs):
            return {"ok": True}

    class Owner(KnowledgeMiningMixin):
        def __init__(self) -> None:
            self.config = SimpleNamespace(
                sharing_viking_endpoint="https://openviking.example",
                sharing_viking_team_api_key="secret",
                sharing_viking_user="team",
            )
            self._knowledge_compile_state_store = LocalObjectStore(tmp_path / "compile-state")

        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_compile_capabilities(self, _account):
            return {"can_create": True}

        async def _knowledge_request(self, _account, _method, path, **kwargs):
            assert path == "/api/v1/compile"
            compile_requests.append(kwargs["json"])
            return {"task_id": f"cmp_{len(compile_requests)}", "status": "accepted"}

        def _start_knowledge_compile_watch(self, _account, _task_id):
            pass

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    monkeypatch.setattr(knowledge_mining_module, "CompileClient", FakeCompileClient)
    owner = Owner()
    app = FastAPI()
    owner._register_knowledge_mining_routes(app)
    client = TestClient(app)
    payload = {
        "account": "demo",
        "source_uri": _SOURCE_ROOT,
        "target_uri": _WIKI_ROOT,
        "skill_uri": "viking://agent/skills/llm-wiki",
    }

    first = client.post("/api/knowledge-mining/tasks", json=payload)
    assert first.status_code == 202
    assert "args" not in compile_requests[0]
    assert compile_requests[0] == {
        "from": [_SOURCE_ROOT],
        "to": _WIKI_ROOT,
        "skill": "viking://agent/skills/llm-wiki",
    }

    first_body = first.json()
    owner._finalize_knowledge_compile("demo", first_body["task_id"], "completed")
    second = client.post("/api/knowledge-mining/tasks", json=payload)

    assert second.status_code == 202
    assert compile_requests[1]["args"] == {
        "last_compile_time": first_body["compile_started_at"],
    }
    assert second.json()["last_compile_time"] == first_body["compile_started_at"]


def test_failed_compile_does_not_advance_incremental_watermark(tmp_path) -> None:
    owner = KnowledgeMiningMixin()
    owner.config = SimpleNamespace(sharing_viking_endpoint="https://openviking.example")
    owner._knowledge_compile_state_store = LocalObjectStore(tmp_path)
    owner._record_knowledge_compile_submission(
        "demo",
        "cmp_failed",
        compile_started_at="2026-09-23T08:00:00+00:00",
        source_uri=_SOURCE_ROOT,
        target_uri=_WIKI_ROOT,
        skill_uri="viking://agent/skills/llm-wiki",
        last_compile_time="",
    )

    owner._finalize_knowledge_compile("demo", "cmp_failed", "failed")

    assert owner._knowledge_last_compile_time("demo") == ""


def test_markdown_link_extraction_ignores_images_and_code() -> None:
    links = _extract_markdown_links(
        "[正文链接](concepts/b.md)\n"
        "![图片](concepts/image.md)\n"
        "`[行内代码](concepts/inline.md)`\n"
        "```md\n[代码块](concepts/fenced.md)\n```\n"
        "[引用式][b]\n\n[b]: concepts/b.md\n"
    )

    assert [(link["label"], link["href"]) for link in links] == [
        ("正文链接", "concepts/b.md"),
        ("引用式", "concepts/b.md"),
    ]


@pytest.mark.parametrize(
    ("source", "href", "expected"),
    [
        (
            f"{_WIKI_ROOT}/guides/a.md",
            "../concepts/b.md#details",
            f"{_WIKI_ROOT}/concepts/b.md",
        ),
        (
            f"{_WIKI_ROOT}/guides/a.md",
            "/concepts/b.md",
            f"{_WIKI_ROOT}/concepts/b.md",
        ),
        (
            f"{_WIKI_ROOT}/guides/a.md",
            "../concepts/b",
            f"{_WIKI_ROOT}/concepts/b.md",
        ),
        (f"{_WIKI_ROOT}/guides/a.md", "https://example.com/b.md", ""),
        (f"{_WIKI_ROOT}/guides/a.md", "../../outside.md", ""),
    ],
)
def test_resolve_wiki_link_only_accepts_existing_pages(source: str, href: str, expected: str) -> None:
    pages = {
        f"{_WIKI_ROOT}/guides/a.md",
        f"{_WIKI_ROOT}/concepts/b.md",
    }
    assert _resolve_wiki_link(source, href, pages) == expected


def test_build_wiki_link_index_calculates_bidirectional_links() -> None:
    index = _build_wiki_link_index(
        {
            f"{_WIKI_ROOT}/a.md": "[B 页面](b.md)\n再次参见 [B](b.md#details)",
            f"{_WIKI_ROOT}/b.md": "# B",
            f"{_WIKI_ROOT}/c.md": "[B](b.md)",
        },
        account="demo-account",
        compile_task_id="cmp_123",
    )

    assert index["page_count"] == 3
    assert index["edge_count"] == 2
    assert index["link_count"] == 3
    assert index["compile_task_id"] == "cmp_123"
    assert index["pages"][f"{_WIKI_ROOT}/a.md"]["links"] == [
        {
            "target_uri": f"{_WIKI_ROOT}/b.md",
            "target_name": "b.md",
            "target_path": "b.md",
            "labels": ["B 页面", "B"],
            "lines": [1, 2],
            "count": 2,
        }
    ]
    assert [
        backlink["source_uri"]
        for backlink in index["pages"][f"{_WIKI_ROOT}/b.md"]["backlinks"]
    ] == [
        f"{_WIKI_ROOT}/a.md",
        f"{_WIKI_ROOT}/c.md",
    ]


def test_wiki_graph_uses_compiled_pages_and_resolved_link_index() -> None:
    documents = {
        f"{_WIKI_ROOT}/index.md": (
            "---\ntype: index\ntitle: 团队知识\n---\n# 团队知识\n[概念 A](concept/a.md)"
        ),
        f"{_WIKI_ROOT}/concept/a.md": (
            "---\ntype: concept\ntitle: 概念 A\ndescription: 核心概念。\n---\n# 概念 A\n正文"
        ),
    }
    index = _build_wiki_link_index(documents, account="demo-account")

    graph = build_wiki_graph(documents, index, wiki_root=_WIKI_ROOT)

    assert [(node["title"], node["category"]) for node in graph["nodes"]] == [
        ("团队知识", "index"),
        ("概念 A", "concept"),
    ]
    assert graph["links"] == [
        {
            "source": f"{_WIKI_ROOT}/index.md",
            "target": f"{_WIKI_ROOT}/concept/a.md",
            "label": "概念 A",
            "count": 1,
        }
    ]


def test_wiki_graph_ignores_nested_source_titles_in_frontmatter() -> None:
    root = "viking://resources/knowledge-mining/run-123/wiki"
    documents = {
        f"{root}/index.md": (
            "---\ntype: index\ntitle: 企业知识库\ndescription: 页面说明\nsources:\n"
            "  - resource: viking://resources/source.md\n"
            "    title: 不应覆盖页面标题\n---\n# 企业知识库\n正文"
        )
    }
    index = _build_wiki_link_index(documents, account="default", wiki_root=root)

    graph = build_wiki_graph(documents, index, wiki_root=root)

    assert graph["nodes"][0]["title"] == "企业知识库"
    assert graph["nodes"][0]["description"] == "页面说明"


def test_wiki_graph_html_is_self_contained_and_script_safe() -> None:
    graph = {
        "nodes": [
            {
                "id": f"{_WIKI_ROOT}/index.md",
                "uri": f"{_WIKI_ROOT}/index.md",
                "path": "index.md",
                "title": "</script><script>alert(1)</script>",
                "description": "",
                "category": "index",
                "category_label": "导航",
                "color": "#f43f5e",
                "degree": 0,
                "body": "正文",
            }
        ],
        "links": [],
    }

    rendered = render_wiki_graph_html(graph, title="知识库")

    assert "OPENVIKING · HTML KNOWLEDGE GRAPH" in rendered
    assert "https://d3js.org" not in rendered
    assert 'type="application/json" id="graph-data"' in rendered
    assert "</script><script>alert(1)</script>" not in rendered


def test_wiki_graph_document_loader_reads_markdown_through_openviking_api() -> None:
    calls: list[str] = []

    class Owner(KnowledgeMiningMixin):
        async def _knowledge_tree(self, *_args, **_kwargs):
            return {
                "entries": [
                    {"uri": f"{_WIKI_ROOT}/index.md", "is_dir": False},
                    {"uri": f"{_WIKI_ROOT}/data.json", "is_dir": False},
                    {"uri": f"{_WIKI_ROOT}/concept", "is_dir": True},
                    {"uri": f"{_WIKI_ROOT}/concept/a.md", "is_dir": False},
                ]
            }

        async def _knowledge_request(self, _account, _method, _path, **kwargs):
            uri = kwargs["params"]["uri"]
            calls.append(uri)
            return {"content": f"# {uri.rsplit('/', 1)[-1]}"}

    documents = asyncio.run(Owner()._knowledge_read_wiki_documents("demo-account", max_pages=10))

    assert list(documents) == [
        f"{_WIKI_ROOT}/concept/a.md",
        f"{_WIKI_ROOT}/index.md",
    ]
    assert calls == list(documents)


def test_historical_wiki_discovery_returns_only_nonempty_compile_outputs() -> None:
    class Owner(KnowledgeMiningMixin):
        async def _knowledge_request(self, *_args, **_kwargs):
            return [
                {
                    "uri": "viking://resources/knowledge-mining/run-old/wiki",
                    "isDir": True,
                    "modTime": "2026-08-01T00:00:00Z",
                },
                {
                    "uri": "viking://resources/knowledge-mining/run-old/wiki/index.md",
                    "isDir": False,
                    "modTime": "2026-08-01T01:00:00Z",
                },
                {
                    "uri": "viking://resources/knowledge-mining/run-new/wiki",
                    "isDir": True,
                    "modTime": "2026-09-01T00:00:00Z",
                },
                {
                    "uri": "viking://resources/knowledge-mining/run-new/wiki/index.md",
                    "isDir": False,
                    "modTime": "2026-09-01T01:00:00Z",
                },
                {
                    "uri": "viking://resources/knowledge-mining/run-new/wiki/topic/a.md",
                    "isDir": False,
                    "modTime": "2026-09-01T02:00:00Z",
                },
                {
                    "uri": "viking://resources/knowledge-mining/run-empty/wiki",
                    "isDir": True,
                    "modTime": "2026-09-02T00:00:00Z",
                },
            ]

    roots = asyncio.run(Owner()._knowledge_wiki_roots("default"))

    assert [(item["name"], item["page_count"]) for item in roots] == [
        ("run-new", 2),
        ("run-old", 1),
    ]


def test_completed_compile_rebuilds_full_wiki_once_per_task(tmp_path) -> None:
    class Owner(KnowledgeMiningMixin):
        def __init__(self) -> None:
            self.store = LocalObjectStore(tmp_path)
            self.reads: list[str] = []

        def _knowledge_endpoint(self) -> str:
            return "https://openviking.example"

        def _knowledge_link_store(self) -> LocalObjectStore:
            return self.store

        async def _knowledge_tree(self, *_args, **_kwargs):
            return {
                "entries": [
                    {"uri": f"{_WIKI_ROOT}/a.md", "is_dir": False},
                    {"uri": f"{_WIKI_ROOT}/b.md", "is_dir": False},
                    {"uri": f"{_WIKI_ROOT}/image.png", "is_dir": False},
                ]
            }

        async def _knowledge_request(self, _account, _method, _path, *, params, **_kwargs):
            uri = params["uri"]
            self.reads.append(uri)
            return {"content": "[B](b.md)" if uri.endswith("a.md") else "# B"}

    async def scenario():
        owner = Owner()
        first = await owner._knowledge_rebuild_link_index("demo", compile_task_id="cmp_1")
        second = await owner._knowledge_rebuild_link_index("demo", compile_task_id="cmp_1")
        return owner, first, second

    owner, first, second = asyncio.run(scenario())

    assert owner.reads == [
        f"{_WIKI_ROOT}/a.md",
        f"{_WIKI_ROOT}/b.md",
    ]
    assert first == second
    assert first["processed_compile_task_ids"] == ["cmp_1"]
    assert first["pages"][f"{_WIKI_ROOT}/b.md"]["backlinks"][0]["source_uri"].endswith("a.md")


def test_opening_wiki_without_link_index_schedules_rebuild_instead_of_scanning() -> None:
    scheduled: list[tuple[str, str]] = []

    class Owner(KnowledgeMiningMixin):
        def _load_knowledge_link_index(self, _account, _wiki_root=_WIKI_ROOT):
            return None

        def _start_knowledge_link_rebuild(self, account, *, wiki_root=_WIKI_ROOT, delay=0.0):
            scheduled.append((account, wiki_root))

        async def _knowledge_rebuild_link_index(self, *_args, **_kwargs):
            raise AssertionError("file-open must not scan the complete Wiki")

    result = asyncio.run(
        Owner()._knowledge_page_links(
            "demo",
            f"{_WIKI_ROOT}/index.md",
            wiki_root=_WIKI_ROOT,
        )
    )

    assert result == {
        "links": [],
        "backlinks": [],
        "link_index": {"status": "pending"},
    }
    assert scheduled == [("demo", _WIKI_ROOT)]


@pytest.mark.parametrize("kind", ["source", "wiki"])
def test_knowledge_file_save_does_not_wait_for_openviking_ingestion(monkeypatch, kind: str) -> None:
    calls: list[tuple[str, str, dict]] = []
    scheduled: list[tuple[str, str, float]] = []
    target = f"{_SOURCE_ROOT if kind == 'source' else _WIKI_ROOT}/guide.md"

    class Owner(KnowledgeMiningMixin):
        def _knowledge_account(self, account):
            return str(account)

        async def _knowledge_request(self, _account, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET":
                return {"content": "before"}
            return {"task_id": "write-1", "status": "accepted"}

        def _start_knowledge_link_rebuild(self, account, *, wiki_root=_WIKI_ROOT, delay=0.0):
            scheduled.append((account, wiki_root, delay))

    monkeypatch.setattr(knowledge_mining_module, "_require_admin_request", lambda _request: None)
    app = FastAPI()
    Owner()._register_knowledge_mining_routes(app)

    response = TestClient(app).post(
        "/api/knowledge-mining/content",
        json={
            "account": "demo",
            "kind": kind,
            "wiki_root": _WIKI_ROOT,
            "uri": target,
            "content": "after",
            "original_content": "before",
        },
    )

    assert response.status_code == 200
    assert response.json()["saved"] is True
    write = next(call for call in calls if call[0] == "POST")
    assert write[1] == "/api/v1/content/write"
    assert write[2]["json"]["wait"] is False
    assert scheduled == ([('demo', _WIKI_ROOT, 0.5)] if kind == "wiki" else [])


def test_compile_watcher_rebuilds_links_after_success(monkeypatch) -> None:
    rebuilt: list[tuple[str, str]] = []

    async def no_wait(_seconds):
        return None

    class Owner(KnowledgeMiningMixin):
        async def _knowledge_request(self, *_args, **_kwargs):
            return {"status": "completed"}

        def _finalize_knowledge_compile(self, _account, _task_id, _status):
            return None

        async def _knowledge_rebuild_link_index(self, account, *, compile_task_id, force=False):
            rebuilt.append((account, compile_task_id))
            return {}

    monkeypatch.setattr(knowledge_mining_module.asyncio, "sleep", no_wait)
    asyncio.run(Owner()._knowledge_watch_compile("demo", "cmp_2"))

    assert rebuilt == [("demo", "cmp_2")]
