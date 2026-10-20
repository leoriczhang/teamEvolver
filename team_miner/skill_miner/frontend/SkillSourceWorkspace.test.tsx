// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act,cleanup,fireEvent,render,screen,waitFor } from "@testing-library/react";
import { afterEach,beforeEach,describe,expect,it,vi } from "vitest";

import { api } from "@/api/client";
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
});
