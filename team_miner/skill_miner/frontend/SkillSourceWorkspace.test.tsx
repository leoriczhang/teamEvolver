// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act,cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

import { api } from "@/api/client";
import { toastErr } from "@/lib/toast";
import SkillSourceWorkspace from "./SkillSourceWorkspace";

vi.mock("@/api/client", () => ({ api: vi.fn(), tenantHeaders: (_path: string, init?: HeadersInit) => new Headers(init) }));
vi.mock("@/lib/toast", () => ({ toastErr: vi.fn(), toastOk: vi.fn() }));

const mockedApi = vi.mocked(api);
const sourceUri = "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base/模拟客户对练场景";
const config = { accounts: ["team", "other"], current: "team", root_uri: "viking://resources", sources: [] };
const tree = {
  root_uri: config.root_uri,
  exists: true,
  entries: [{ uri: "viking://resources/existing", name: "已有目录", is_dir: true }],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function openPicker() {
  const onSelect = vi.fn();
  const onRefresh = vi.fn(async () => undefined);
  render(<SkillSourceWorkspace sources={[]} selectedPath="" onSelect={onSelect} onRefresh={onRefresh} />);
  await waitFor(() => expect(mockedApi).toHaveBeenCalledWith("/api/mining/ov-sources"));
  fireEvent.click(screen.getByRole("button", { name: "添加知识源" }));
  await screen.findByText("正在读取 OpenViking 目录…");
  return { onSelect, onRefresh };
}

describe("SkillSourceWorkspace source registration", () => {
  beforeEach(() => { mockedApi.mockReset(); });
  afterEach(cleanup);

  it("allows direct URI registration after directory listing fails", async () => {
    const listing = deferred<typeof tree>();
    const register = vi.fn(async (_body: Record<string, unknown>) => ({
      source: { source_id: "source-new", path: sourceUri, document_count: 5, total_bytes: 100, ready: true, tags: ["skill"] },
    }));
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources" && init?.method === "POST") return register(JSON.parse(String(init.body)));
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-tree?")) return listing.promise;
      throw new Error(`Unexpected API call: ${path}`);
    });
    const { onSelect, onRefresh } = await openPicker();
    await act(async () => listing.reject(new Error("Resources tree request failed")));

    expect(screen.getByRole("status")).toHaveTextContent("目录列表读取失败，可直接填写已知且有权访问的文件夹 URI");
    expect(screen.queryByText(/正在读取.*目录/)).not.toBeInTheDocument();
    const uri = screen.getByRole("textbox", { name: "知识源 URI" });
    expect(uri).toHaveAttribute("placeholder", "填写文件夹 URI，或从下方列表选择");
    fireEvent.change(uri, { target: { value: sourceUri } });
    fireEvent.click(screen.getByRole("button", { name: "确认设为知识源" }));

    await waitFor(() => expect(register).toHaveBeenCalledWith({ account: "team", uri: sourceUri, tags: ["skill"] }));
    expect(onSelect).toHaveBeenCalledWith(sourceUri);
    expect(onRefresh).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("preserves URI input when initial and account-reload listings arrive late", async () => {
    const initial = deferred<typeof tree>();
    const reload = deferred<typeof tree>();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-tree?account=team&")) return initial.promise;
      if (path.startsWith("/api/mining/ov-tree?account=other&")) return reload.promise;
      throw new Error(`Unexpected API call: ${path}`);
    });
    await openPicker();
    const uri = screen.getByRole("textbox", { name: "知识源 URI" });
    fireEvent.change(uri, { target: { value: sourceUri } });
    await act(async () => initial.resolve(tree));
    expect(screen.getByRole("treeitem", { name: "已有目录" })).toBeInTheDocument();
    expect(uri).toHaveValue(sourceUri);

    fireEvent.change(screen.getByRole("combobox", { name: "知识源账号" }), { target: { value: "other" } });
    expect(uri).toHaveValue("");
    const otherUri = "viking://resources/other/readable-directory";
    fireEvent.change(uri, { target: { value: otherUri } });
    await act(async () => reload.resolve(tree));
    expect(uri).toHaveValue(otherUri);
    expect(screen.getByRole("button", { name: "确认设为知识源" })).toBeEnabled();
  });

  it("ignores the old account listing when it arrives after the new account listing", async () => {
    const initial = deferred<typeof tree>();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-tree?account=team&")) return initial.promise;
      if (path.startsWith("/api/mining/ov-tree?account=other&")) return { ...tree,
        entries: [{ uri: "viking://resources/other", name: "Other account folder", is_dir: true }] };
      throw new Error(`Unexpected API call: ${path}`);
    });
    await openPicker();
    fireEvent.change(screen.getByRole("combobox", { name: "知识源账号" }), { target: { value: "other" } });
    await screen.findByRole("treeitem", { name: "Other account folder" });
    await act(async () => initial.resolve(tree));
    expect(screen.getByRole("treeitem", { name: "Other account folder" })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: "已有目录" })).not.toBeInTheDocument();
  });
});

describe("SkillSourceWorkspace source editing", () => {
  beforeEach(() => { mockedApi.mockReset(); });
  afterEach(cleanup);

  it("uses the loaded and then saved content as the edit baseline", async () => {
    const source = {
      source_id: "source-edit", path: sourceUri, document_count: 1,
      total_bytes: 100, ready: true, tags: ["skill"],
    };
    const entry = { uri: `${sourceUri}/rules.md`, name: "rules.md", is_dir: false };
    const original = "# Original\n";
    const save = vi.fn(async (body: { content: string }) => ({ content: `${body.content.trim()}\n` }));
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) {
        return { root_uri: sourceUri, exists: true, entries: [entry] };
      }
      if (path.startsWith("/api/mining/ov-source-content?")) {
        return { source_id: source.source_id, ...entry, content: original, editable: true };
      }
      if (path === "/api/mining/ov-source-content" && init?.method === "POST") {
        return save(JSON.parse(String(init.body)));
      }
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "rules.md" }));
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    const editor = screen.getByRole("textbox", { name: "rules.md 文件编辑器" });
    const saveButton = screen.getByRole("button", { name: "保存" });
    expect(editor).toHaveValue(original);
    expect(saveButton).toBeDisabled();

    fireEvent.change(editor, { target: { value: "# Revised" } });
    expect(screen.getByText("未保存")).toBeInTheDocument();
    expect(saveButton).toBeEnabled();
    fireEvent.click(saveButton);
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({
      source_id: source.source_id, uri: entry.uri,
      content: "# Revised", original_content: original,
    }));
    await waitFor(() => expect(editor).toHaveValue("# Revised\n"));
    expect(saveButton).toBeDisabled();
    expect(screen.queryByText("未保存")).not.toBeInTheDocument();

    fireEvent.change(editor, { target: { value: "# Next revision" } });
    fireEvent.click(saveButton);
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({
      source_id: source.source_id, uri: entry.uri,
      content: "# Next revision", original_content: "# Revised\n",
    }));
    await waitFor(() => expect(editor).toHaveValue("# Next revision\n"));
    expect(saveButton).toBeDisabled();
    expect(screen.queryByText("未保存")).not.toBeInTheDocument();
  });

  it("keeps edits typed while an earlier save is in progress", async () => {
    const source = { source_id: "source-edit", path: sourceUri, document_count: 1, total_bytes: 100, ready: true, tags: ["skill"] };
    const entry = { uri: `${sourceUri}/rules.md`, name: "rules.md", is_dir: false };
    const save = deferred<{ content: string }>();
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries: [entry] };
      if (path.startsWith("/api/mining/ov-source-content?")) return { source_id: source.source_id, ...entry, content: "# Original", editable: true };
      if (path === "/api/mining/ov-source-content" && init?.method === "POST") return save.promise;
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "rules.md" }));
    fireEvent.click(await screen.findByRole("button", { name: "编辑" }));
    const editor = screen.getByRole("textbox", { name: "rules.md 文件编辑器" });
    fireEvent.change(editor, { target: { value: "# First save" } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    fireEvent.change(editor, { target: { value: "# Next unsaved edit" } });
    await act(async () => save.resolve({ content: "# First save\n" }));
    expect(editor).toHaveValue("# Next unsaved edit");
    expect(screen.getByText("未保存")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
  });

  it("ignores a late file read after the user opens another file", async () => {
    const source = { source_id: "source-edit", path: sourceUri, document_count: 2, total_bytes: 100, ready: true, tags: ["skill"] };
    const entries = ["first.md", "second.md"].map((name) => ({ uri: `${sourceUri}/${name}`, name, is_dir: false }));
    const first = deferred<Record<string, unknown>>();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries };
      if (path.includes(encodeURIComponent(entries[0].uri))) return first.promise;
      if (path.includes(encodeURIComponent(entries[1].uri))) return { source_id: source.source_id, ...entries[1], content: "# Second file", editable: true };
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "first.md" }));
    fireEvent.click(screen.getByRole("treeitem", { name: "second.md" }));
    await screen.findByRole("heading", { name: "Second file" });
    await act(async () => first.resolve({ source_id: source.source_id, ...entries[0], content: "# Late first file", editable: true }));
    expect(screen.getByRole("heading", { name: "Second file" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Late first file" })).not.toBeInTheDocument();
  });

  it("ignores the old source tree after switching to another source", async () => {
    const first = { source_id: "source-first", path: sourceUri, name: "First source", document_count: 1, total_bytes: 100, ready: true, tags: ["skill"] };
    const second = { ...first, source_id: "source-second", path: "viking://resources/second", name: "Second source" };
    const firstTree = deferred<Record<string, unknown>>();
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.includes("source_id=source-first")) return firstTree.promise;
      if (path.includes("source_id=source-second")) return { root_uri: second.path, exists: true,
        entries: [{ uri: `${second.path}/second.md`, name: "second.md", is_dir: false }] };
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[first, second]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: /Second source 可用/ }));
    await screen.findByRole("treeitem", { name: "second.md" });
    await act(async () => firstTree.resolve({ root_uri: sourceUri, exists: true,
      entries: [{ uri: `${sourceUri}/first.md`, name: "first.md", is_dir: false }] }));
    expect(screen.getByRole("treeitem", { name: "second.md" })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: "first.md" })).not.toBeInTheDocument();
  });
});

describe("SkillSourceWorkspace management actions", () => {
  const source = { source_id: "source-actions", path: sourceUri, name: "验收知识源", document_count: 0,
    total_bytes: 0, ready: false, status: "unverified", tags: ["skill"] };
  beforeEach(() => { mockedApi.mockReset(); vi.mocked(toastErr).mockClear(); });
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("shows checked directory statistics instead of the unverified inventory placeholders", async () => {
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return { ...config, sources: [{ ...source, document_count: 3, total_bytes: 1024, ready: true, status: "ready" }] };
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries: [] };
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    expect(await screen.findByText("3 个文件 · 1.0 KB")).toBeInTheDocument();
    expect(screen.queryByText("空目录")).not.toBeInTheDocument();
  });

  it("shows a directory read error and refresh retries the tree, not only inventory", async () => {
    let reads = 0;
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) {
        if (++reads === 1) throw new Error("Temporary directory failure");
        return { root_uri: sourceUri, exists: true, entries: [{ uri: `${sourceUri}/recovered.md`, name: "recovered.md", is_dir: false }] };
      }
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    expect(await screen.findByText("目录读取失败：Temporary directory failure。请点击刷新重试。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByRole("treeitem", { name: "recovered.md" })).toBeInTheDocument();
    expect(reads).toBe(2);
  });

  it("uploads a folder under the selected directory and preserves its relative paths", async () => {
    const folder = { uri: `${sourceUri}/selected`, name: "selected", is_dir: true };
    const refresh = vi.fn(async () => undefined);
    const submitted = vi.fn();
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries: [folder] };
      if (path === "/api/mining/ov-source-upload") { submitted(JSON.parse(String(init?.body))); return { file_count: 1 }; }
      throw new Error(`Unexpected API call: ${path}`);
    });
    const { container } = render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={refresh} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "selected" }));
    const file = new File(["folder contents"], "lesson.txt", { type: "text/plain" });
    Object.defineProperty(file, "webkitRelativePath", { value: "bundle/lesson.txt" });
    fireEvent.change(container.querySelector("input[webkitdirectory]")!, { target: { files: [file] } });
    await waitFor(() => expect(submitted).toHaveBeenCalledWith({ source_id: source.source_id, parent_uri: folder.uri,
      files: [{ name: "lesson.txt", relative_path: "bundle/lesson.txt", content_b64: btoa("folder contents") }] }));
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("updates source purposes and refuses to remove the last purpose", async () => {
    const patch = vi.fn();
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries: [] };
      if (path.endsWith(source.source_id) && init?.method === "PATCH") { patch(JSON.parse(String(init.body))); return { ok: true }; }
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(screen.getByRole("button", { name: "Skill 知识源" }));
    expect(toastErr).toHaveBeenCalledWith("无法移除用途标签", "知识源至少需要保留一种用途");
    expect(patch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "知识库知识源" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith({ tags: ["skill", "knowledge"] }));
  });

  it("deletes a selected entry and removes only the registration through separate actions", async () => {
    const entry = { uri: `${sourceUri}/remove.md`, name: "remove.md", is_dir: false };
    const deleted = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries: [entry] };
      if (path.startsWith("/api/mining/ov-source-content?")) return { ...entry, source_id: source.source_id, content: "# Remove", editable: true };
      if (init?.method === "DELETE") { deleted(path, init.body ? JSON.parse(String(init.body)) : null); return { ok: true }; }
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "remove.md" }));
    await screen.findByRole("heading", { name: "Remove" });
    fireEvent.click(screen.getByRole("button", { name: "删除 remove.md" }));
    await waitFor(() => expect(deleted).toHaveBeenCalledWith("/api/mining/ov-entry", { source_id: source.source_id, uri: entry.uri }));
    fireEvent.click(screen.getByRole("button", { name: "删除知识源 验收知识源" }));
    await waitFor(() => expect(deleted).toHaveBeenCalledWith(`/api/mining/ov-sources/${source.source_id}`, null));
  });

  it("keeps the newly opened file when an earlier delete completes late", async () => {
    const entries = ["remove.md", "keep.md"].map((name) => ({ uri: `${sourceUri}/${name}`, name, is_dir: false }));
    const deletion = deferred<{ ok: boolean }>();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/mining/ov-sources") return config;
      if (path.startsWith("/api/mining/ov-source-tree?")) return { root_uri: sourceUri, exists: true, entries };
      if (path.startsWith("/api/mining/ov-source-content?")) {
        const entry = entries.find((item) => path.includes(encodeURIComponent(item.uri)))!;
        return { ...entry, source_id: source.source_id, content: `# ${entry.name}`, editable: true };
      }
      if (path === "/api/mining/ov-entry" && init?.method === "DELETE") return deletion.promise;
      throw new Error(`Unexpected API call: ${path}`);
    });
    render(<SkillSourceWorkspace sources={[source]} selectedPath={sourceUri} onSelect={vi.fn()} onRefresh={vi.fn(async () => undefined)} />);
    fireEvent.click(await screen.findByRole("treeitem", { name: "remove.md" }));
    await screen.findByRole("heading", { name: "remove.md" });
    fireEvent.click(screen.getByRole("button", { name: "删除 remove.md" }));
    fireEvent.click(screen.getByRole("treeitem", { name: "keep.md" }));
    await screen.findByRole("heading", { name: "keep.md" });
    await act(async () => deletion.resolve({ ok: true }));
    expect(screen.getByRole("heading", { name: "keep.md" })).toBeInTheDocument();
  });
});
