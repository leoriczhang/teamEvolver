// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Component, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import KnowledgeBaseMiningView from "./KnowledgeBaseMiningView";
import { resolveKnowledgeHref } from "./KnowledgeBaseMiningViewShared";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {},
  tenantHeaders: (_path: string, initial?: HeadersInit) => {
    const headers = new Headers(initial);
    if (!headers.has("X-Tenant-Id")) headers.set("X-Tenant-Id", "tenant-ui");
    return headers;
  },
}));

vi.mock("@/lib/toast", () => ({
  toastErr: vi.fn(),
  toastOk: vi.fn(),
}));

const mockedApi = vi.mocked(api);

class MiningViewBoundary extends Component<{ children: ReactNode }, { error: string }> {
  state = { error: "" };

  static getDerivedStateFromError(error: Error) {
    return { error: error.message };
  }

  render() {
    return this.state.error ? <div role="alert">{this.state.error}</div> : this.props.children;
  }
}

const sourceRoot = "viking://resources/agent_knowledge_workspace/input/raw_knowledge_base";
const wikiRoot = "viking://resources/agent_knowledge_workspace/output/processed_knowledge";
const current = {
  account: "demo",
  kind: "wiki" as const,
  uri: `${wikiRoot}/guides/a.md`,
  name: "a.md",
  content: "",
  editable: true,
};
const sourceTree = {
  account: "demo",
  kind: "source" as const,
  root_uri: sourceRoot,
  exists: true,
  entries: [
    { uri: `${sourceRoot}/docs/source.md`, name: "source.md", is_dir: false },
  ],
};
const wikiTree = {
  account: "demo",
  kind: "wiki" as const,
  root_uri: wikiRoot,
  exists: true,
  entries: [
    { uri: `${wikiRoot}/concepts/b.md`, name: "b.md", is_dir: false },
  ],
};

function backendWorkspaceResponse(path: string) {
  const request = new URL(path, "http://team-evolver.test");
  if (request.pathname === "/api/knowledge-mining/config") {
    return {
      account_id: "default", user_id: "team", source_uri: sourceRoot, target_uri: wikiRoot,
      skill_uri: "viking://agent/skills/llm-wiki", revision: 7, accounts: [{ account_id: "default" }],
    };
  }
  expect(request.searchParams.get("account")).toBe("default");
  if (request.pathname === "/api/knowledge-mining/wiki-roots") {
    return { roots: [{ uri: wikiRoot, name: "processed_knowledge" }] };
  }
  if (request.pathname === "/api/knowledge-mining/tree") {
    const kind = request.searchParams.get("kind");
    expect(["source", "wiki"]).toContain(kind);
    const root = kind === "source" ? sourceRoot : wikiRoot;
    const name = kind === "source" ? "source.md" : "guide.md";
    return { account: "default", kind, tree: { entries: [{ uri: `${root}/${name}`, name, isDir: false }] } };
  }
  if (request.pathname === "/api/knowledge-mining/tasks") return { account: "default", tasks: [] };
  if (request.pathname === "/api/knowledge-mining/capabilities") {
    return { account: "default", capabilities: { configured: true, can_create: true }, last_compile_time: null };
  }
  throw new Error(`Unexpected API call: ${path}`);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("resolveKnowledgeHref", () => {
  it("resolves a sibling Wiki file and retains its heading fragment", () => {
    expect(resolveKnowledgeHref(
      "../concepts/b.md#详细参数",
      current,
      sourceRoot,
      wikiRoot,
      sourceTree,
      wikiTree,
    )).toEqual({
      kind: "wiki",
      entry: wikiTree.entries[0],
      fragment: "详细参数",
    });
  });

  it("allows a relative link to cross output/input but not leave the knowledge workspace", () => {
    expect(resolveKnowledgeHref(
      "../../../input/raw_knowledge_base/docs/source.md#证据",
      current,
      sourceRoot,
      wikiRoot,
      sourceTree,
      wikiTree,
    )).toEqual({
      kind: "source",
      entry: sourceTree.entries[0],
      fragment: "证据",
    });
    expect(resolveKnowledgeHref(
      "../../../../outside.md",
      current,
      sourceRoot,
      wikiRoot,
      sourceTree,
      wikiTree,
    )).toBeNull();
  });

  it("resolves an absolute viking source URI", () => {
    expect(resolveKnowledgeHref(
      `${sourceRoot}/docs/source.md`,
      current,
      sourceRoot,
      wikiRoot,
      sourceTree,
      wikiTree,
    )?.kind).toBe("source");
  });

  it("keeps relative links in a selected historical Wiki root", () => {
    const historicalRoot = "viking://resources/knowledge-mining/run-123/wiki";
    const historicalCurrent = { ...current, uri: `${historicalRoot}/guides/a.md` };
    const historicalTree = {
      ...wikiTree,
      root_uri: historicalRoot,
      entries: [{ uri: `${historicalRoot}/concepts/b.md`, name: "b.md", is_dir: false }],
    };

    expect(resolveKnowledgeHref(
      "../concepts/b.md",
      historicalCurrent,
      sourceRoot,
      historicalRoot,
      sourceTree,
      historicalTree,
    )?.entry.uri).toBe(`${historicalRoot}/concepts/b.md`);
    expect(resolveKnowledgeHref(
      "../../../outside.md",
      historicalCurrent,
      sourceRoot,
      historicalRoot,
      sourceTree,
      historicalTree,
    )).toBeNull();
  });
});

describe("KnowledgeBaseMiningView collapsible regions", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(cleanup);

  it("collapses the OpenViking account and Wiki backlink regions", async () => {
    mockedApi.mockImplementation(async (path: string) => {
      if (path === "/api/knowledge-mining/config") {
        return {
          accounts: ["demo"],
          current: "demo",
          source_root: sourceRoot,
          wiki_root: wikiRoot,
          skill_uri: "viking://resources/skills/wiki",
          endpoint: "http://openviking.test",
          account_source: "openviking",
        };
      }
      if (path.startsWith("/api/knowledge-mining/wiki-roots")) return { default: wikiRoot, roots: [] };
      if (path.includes("/api/knowledge-mining/tree") && path.includes("kind=source")) return sourceTree;
      if (path.includes("/api/knowledge-mining/tree") && path.includes("kind=wiki")) return wikiTree;
      if (path.startsWith("/api/knowledge-mining/tasks?")) return { tasks: [] };
      if (path.startsWith("/api/knowledge-mining/capabilities?")) {
        return { account: "demo", configured: true, can_create: true };
      }
      if (path.startsWith("/api/knowledge-mining/content?")) {
        return {
          account: "demo",
          kind: "wiki",
          uri: `${wikiRoot}/concepts/b.md`,
          name: "b.md",
          content: "# B",
          editable: true,
          backlinks: [{
            source_uri: `${wikiRoot}/guides/referrer.md`,
            source_name: "referrer.md",
            source_path: "guides/referrer.md",
            labels: ["相关页面"],
            lines: [3],
            count: 1,
          }],
          link_index: { status: "ready" },
        };
      }
      throw new Error(`Unexpected API call: ${path}`);
    });

    render(<KnowledgeBaseMiningView active />);

    const collapseAccount = await screen.findByRole("button", { name: "收起 OpenViking 账号区域" });
    expect(screen.getByRole("combobox", { name: "OpenViking 账号" })).toBeVisible();
    fireEvent.click(collapseAccount);
    expect(screen.queryByRole("combobox", { name: "OpenViking 账号" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "展开 OpenViking 账号区域" }));
    expect(screen.getByRole("combobox", { name: "OpenViking 账号" })).toBeVisible();

    fireEvent.click(screen.getByRole("tab", { name: "知识库" }));
    fireEvent.click(await screen.findByRole("treeitem", { name: "b.md" }));

    const backlinksToggle = await screen.findByRole("button", { name: /被引用/ });
    expect(backlinksToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("referrer.md")).toBeVisible();
    fireEvent.click(backlinksToggle);
    expect(backlinksToggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("referrer.md")).not.toBeInTheDocument();
  });
});

describe("KnowledgeBaseMiningView backend API contract", () => {
  beforeEach(() => {
    mockedApi.mockReset();
    vi.mocked(toastErr).mockClear();
    vi.mocked(toastOk).mockClear();
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(cleanup);

  it("opens the workspace with the backend config, tree and capabilities response shapes", async () => {
    const requestedPaths: string[] = [];
    mockedApi.mockImplementation(async (path: string) => {
      requestedPaths.push(path);
      const request = new URL(path, "http://team-evolver.test");
      if (request.pathname === "/api/knowledge-mining/config") {
        return {
          account_id: "default",
          user_id: "team",
          source_uri: sourceRoot,
          target_uri: wikiRoot,
          skill_uri: "viking://agent/skills/llm-wiki",
          revision: 7,
          accounts: [{ account_id: "default" }],
        };
      }
      expect(request.searchParams.get("account")).toBe("default");
      if (request.pathname === "/api/knowledge-mining/wiki-roots") {
        return { roots: [{ uri: wikiRoot, name: "processed_knowledge" }] };
      }
      if (request.pathname === "/api/knowledge-mining/tree") {
        const kind = request.searchParams.get("kind");
        expect(["source", "wiki"]).toContain(kind);
        const root = kind === "source" ? sourceRoot : wikiRoot;
        const name = kind === "source" ? "source.md" : "guide.md";
        return {
          account: "default",
          kind,
          tree: { entries: [{ uri: `${root}/${name}`, name, isDir: false }] },
        };
      }
      if (request.pathname === "/api/knowledge-mining/tasks") {
        return { account: "default", tasks: [] };
      }
      if (request.pathname === "/api/knowledge-mining/capabilities") {
        return {
          account: "default",
          capabilities: { configured: true, can_create: true },
          last_compile_time: null,
        };
      }
      throw new Error(`Unexpected API call: ${path}`);
    });

    render(<MiningViewBoundary><KnowledgeBaseMiningView active /></MiningViewBoundary>);

    expect(await screen.findByRole("option", { name: "default" })).toHaveValue("default");
    expect(screen.getByRole("combobox", { name: "OpenViking 账号" })).toHaveValue("default");
    expect(await screen.findByRole("treeitem", { name: "source.md" })).toBeVisible();
    for (const name of ["知识源", "知识库", "挖掘"]) {
      expect(screen.getByRole("tab", { name })).toBeVisible();
    }

    fireEvent.click(screen.getByRole("tab", { name: "知识库" }));
    expect(await screen.findByRole("treeitem", { name: "guide.md" })).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "挖掘" }));
    expect(await screen.findByRole("button", { name: "知识库挖掘" })).toBeEnabled();
    expect(screen.getByText(sourceRoot)).toBeVisible();
    expect(screen.getByText(wikiRoot)).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await waitFor(() => {
      const paths = requestedPaths.map((path) => new URL(path, "http://team-evolver.test"));
      expect(paths.some((path) => path.pathname === "/api/knowledge-mining/tree"
        && path.searchParams.get("kind") === "source")).toBe(true);
      expect(paths.some((path) => path.pathname === "/api/knowledge-mining/tree"
        && path.searchParams.get("kind") === "wiki")).toBe(true);
      expect(paths.some((path) => path.pathname === "/api/knowledge-mining/tasks")).toBe(true);
      expect(paths.some((path) => path.pathname === "/api/knowledge-mining/capabilities")).toBe(true);
    });
  });

  it("keeps source and Wiki directories usable when the Compile capability request fails", async () => {
    mockedApi.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/knowledge-mining/capabilities?")) throw new Error("Compile probe unavailable");
      return backendWorkspaceResponse(path);
    });

    render(<MiningViewBoundary><KnowledgeBaseMiningView active /></MiningViewBoundary>);

    expect(await screen.findByRole("treeitem", { name: "source.md" })).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "知识库" }));
    expect(await screen.findByRole("treeitem", { name: "guide.md" })).toBeVisible();
    fireEvent.click(screen.getByRole("tab", { name: "挖掘" }));
    expect(await screen.findByRole("button", { name: "知识库挖掘" })).toBeDisabled();
    expect(await screen.findByText("不可用 · 能力检测失败")).toBeVisible();
    expect(toastErr).toHaveBeenCalledWith("部分工作区信息读取失败", "Compile probe unavailable");
    fireEvent.click(screen.getByRole("tab", { name: "知识源" }));
    expect(screen.getByRole("treeitem", { name: "source.md" })).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("previews and edits minimal content responses and waits for the save job before reloading content", async () => {
    const uri = `${wikiRoot}/guide.md`;
    const original = "# 原始知识\n\n已有来源。";
    const edited = "# 修订知识\n\n保存后的内容。";
    const jobId = "miner:save-ui";
    const statusUrl = `/api/jobs/${jobId}`;
    const resultUrl = `/api/knowledge-mining/jobs/${jobId}/result`;
    const jobStatus = deferred<{ status: string }>();
    const jobResult = deferred<{ files: Array<{ uri: string; size: number }>; total_bytes: number }>();
    const oldBacklink = {
      source_uri: `${wikiRoot}/old.md`, source_name: "old.md", source_path: "old.md",
      labels: ["旧引用"], lines: [], count: 1,
    };
    const newBacklink = {
      source_uri: `${wikiRoot}/new.md`, source_name: "new.md", source_path: "new.md",
      labels: ["新引用"], lines: [], count: 1,
    };
    const refreshedContent = deferred<{
      uri: string; content: string; links: []; backlinks: typeof newBacklink[];
      link_index: { status: "ready" };
    }>();
    const events: string[] = [];
    let contentReads = 0;

    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      const request = new URL(path, "http://team-evolver.test");
      if (path === "/api/knowledge-mining/content" && init?.method === "POST") {
        const headers = new Headers(init.headers);
        expect(headers.get("X-Tenant-Id")).toBe("tenant-ui");
        expect(headers.get("Content-Type")).toBe("application/json");
        expect(JSON.parse(String(init.body))).toMatchObject({
          account: "default", kind: "wiki", uri, content: edited, original_content: original,
        });
        events.push("save-accepted");
        return { job_id: jobId, status: "queued", status_url: statusUrl, account: "default" };
      }
      if (path === statusUrl) {
        expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-ui");
        events.push("job-status");
        return jobStatus.promise;
      }
      if (path === resultUrl) {
        expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-ui");
        events.push("job-result");
        return jobResult.promise;
      }
      if (request.pathname === "/api/knowledge-mining/content") {
        expect(request.searchParams.get("uri")).toBe(uri);
        expect(request.searchParams.get("kind")).toBe("wiki");
        expect(request.searchParams.get("account")).toBe("default");
        contentReads++;
        events.push("read-content");
        if (contentReads === 1) return {
          uri, content: original, backlinks: [oldBacklink], link_index: { status: "ready" },
        };
        expect(new Headers(init?.headers).get("X-Tenant-Id")).toBe("tenant-ui");
        return refreshedContent.promise;
      }
      return backendWorkspaceResponse(path);
    });

    render(<MiningViewBoundary><KnowledgeBaseMiningView active /></MiningViewBoundary>);
    fireEvent.click(await screen.findByRole("tab", { name: "知识库" }));
    fireEvent.click(await screen.findByRole("treeitem", { name: "guide.md" }));
    expect(await screen.findByRole("heading", { name: "原始知识" })).toBeVisible();
    expect(screen.getByText("old.md")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    const editor = screen.getByRole("textbox", { name: "guide.md 文件编辑器" });
    expect(editor).toHaveValue(original);
    fireEvent.change(editor, { target: { value: edited } });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(events).toEqual(["read-content", "save-accepted", "job-status"]));
    expect(screen.getByRole("button", { name: "保存中" })).toBeDisabled();
    expect(editor).toHaveValue(edited);
    expect(contentReads).toBe(1);
    expect(toastOk).not.toHaveBeenCalled();

    await act(async () => { jobStatus.resolve({ status: "succeeded" }); });
    await waitFor(() => expect(events[events.length - 1]).toBe("job-result"));
    expect(editor).toHaveValue(edited);
    expect(contentReads).toBe(1);
    expect(toastOk).not.toHaveBeenCalled();

    await act(async () => { jobResult.resolve({ files: [{ uri, size: edited.length }], total_bytes: edited.length }); });
    await waitFor(() => expect(contentReads).toBe(2));
    expect(screen.getByRole("button", { name: "保存中" })).toBeDisabled();
    expect(editor).toHaveValue(edited);
    expect(toastOk).not.toHaveBeenCalled();

    await act(async () => { refreshedContent.resolve({
      uri, content: edited, links: [], backlinks: [newBacklink], link_index: { status: "ready" },
    }); });
    await waitFor(() => expect(toastOk).toHaveBeenCalledWith("文件已保存", "guide.md"));
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
    expect(editor).toHaveValue(edited);
    expect(screen.queryByText("未保存")).not.toBeInTheDocument();
    expect(screen.queryByText("old.md")).not.toBeInTheDocument();
    expect(screen.getByText("new.md")).toBeVisible();
    expect(toastErr).not.toHaveBeenCalled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(events).toEqual(["read-content", "save-accepted", "job-status", "job-result", "read-content"]);
  });

  it.each(["newer edit", "another file"])("does not replace %s when an earlier save completes", async (followup) => {
    const savedUri = `${wikiRoot}/guide.md`;
    const otherUri = `${sourceRoot}/source.md`;
    const submitted = "# 已提交的修订\n";
    const newerEdit = "# 保存中继续编写的修订\n";
    const otherContent = "# 另一个文件\n\n不能被旧保存覆盖。\n";
    const jobId = "miner:delayed-save";
    const status = deferred<{ status: string }>();
    let savedReads = 0;
    mockedApi.mockImplementation(async (path: string, init?: RequestInit) => {
      if (path === "/api/knowledge-mining/content" && init?.method === "POST") {
        expect(JSON.parse(String(init.body))).toMatchObject({ uri: savedUri, content: submitted });
        return { job_id: jobId, status_url: `/api/jobs/${jobId}` };
      }
      if (path === `/api/jobs/${jobId}`) return status.promise;
      if (path === `/api/knowledge-mining/jobs/${jobId}/result`) return { files: [{ uri: savedUri }] };
      const request = new URL(path, "http://team-evolver.test");
      if (request.pathname === "/api/knowledge-mining/content") {
        const uri = request.searchParams.get("uri");
        if (uri === otherUri) return { uri, content: otherContent };
        expect(uri).toBe(savedUri);
        return { uri, content: ++savedReads === 1 ? "# 原始知识\n" : submitted };
      }
      return backendWorkspaceResponse(path);
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    try {
      render(<MiningViewBoundary><KnowledgeBaseMiningView active /></MiningViewBoundary>);
      fireEvent.click(await screen.findByRole("tab", { name: "知识库" }));
      fireEvent.click(await screen.findByRole("treeitem", { name: "guide.md" }));
      await screen.findByRole("heading", { name: "原始知识" });
      fireEvent.click(screen.getByRole("button", { name: "编辑" }));
      const editor = screen.getByRole("textbox", { name: "guide.md 文件编辑器" });
      fireEvent.change(editor, { target: { value: submitted } });
      fireEvent.click(screen.getByRole("button", { name: "保存" }));
      await waitFor(() => expect(mockedApi).toHaveBeenCalledWith(`/api/jobs/${jobId}`, expect.anything()));
      expect(editor).not.toBeDisabled();

      if (followup === "newer edit") {
        fireEvent.change(editor, { target: { value: newerEdit } });
      } else {
        fireEvent.click(screen.getByRole("tab", { name: "知识源" }));
        fireEvent.click(await screen.findByRole("treeitem", { name: "source.md" }));
        await screen.findByRole("heading", { name: "另一个文件" });
        fireEvent.click(screen.getByRole("button", { name: "编辑" }));
      }

      await act(async () => { status.resolve({ status: "succeeded" }); });
      await waitFor(() => expect(toastOk).toHaveBeenCalledWith("文件已保存", "guide.md"));
      if (followup === "newer edit") {
        expect(editor).toHaveValue(newerEdit);
        expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
        expect(screen.getByText("未保存")).toBeVisible();
        fireEvent.change(editor, { target: { value: submitted } });
        expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
      } else {
        expect(screen.getByRole("textbox", { name: "source.md 文件编辑器" })).toHaveValue(otherContent);
        expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
        expect(screen.queryByText("未保存")).not.toBeInTheDocument();
      }
      expect(toastErr).not.toHaveBeenCalled();
    } finally {
      confirm.mockRestore();
    }
  });

  it("stops polling a finished task and resumes when another task is selected", async () => {
    const scrollToDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTo");
    Element.prototype.scrollTo = vi.fn();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reads = { finished: 0, next: 0, wiki: 0, capabilities: 0 };
    mockedApi.mockImplementation(async (path: string) => {
      const request = new URL(path, "http://team-evolver.test");
      if (request.pathname === "/api/knowledge-mining/tasks") {
        return { tasks: [
          { task_id: "ov-finished", status: "completed" },
          { task_id: "ov-next", status: "queued" },
        ] };
      }
      if (request.pathname === "/api/knowledge-mining/tasks/ov-finished") {
        reads.finished++;
        return { task: { task_id: "ov-finished", status: "succeeded" } };
      }
      if (request.pathname === "/api/knowledge-mining/tasks/ov-next") {
        reads.next++;
        return { task: { task_id: "ov-next", status: "queued" } };
      }
      if (request.pathname === "/api/knowledge-mining/tree" && request.searchParams.get("kind") === "wiki") {
        reads.wiki++;
      }
      if (request.pathname === "/api/knowledge-mining/capabilities") reads.capabilities++;
      return backendWorkspaceResponse(path);
    });
    try {
      render(<MiningViewBoundary><KnowledgeBaseMiningView active /></MiningViewBoundary>);
      await waitFor(() => expect(reads).toEqual({ finished: 1, next: 0, wiki: 2, capabilities: 2 }));
      await act(async () => { await vi.advanceTimersByTimeAsync(7500); });
      expect(reads).toEqual({ finished: 1, next: 0, wiki: 2, capabilities: 2 });

      fireEvent.click(screen.getByRole("tab", { name: "挖掘" }));
      fireEvent.change(screen.getByDisplayValue("已完成 · ov-finished"), { target: { value: "ov-next" } });
      await waitFor(() => expect(reads.next).toBe(1));
      await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
      expect(reads.next).toBe(2);
      expect(reads.finished).toBe(1);
    } finally {
      cleanup();
      vi.useRealTimers();
      if (scrollToDescriptor) Object.defineProperty(Element.prototype, "scrollTo", scrollToDescriptor);
      else Reflect.deleteProperty(Element.prototype, "scrollTo");
    }
  });
});
