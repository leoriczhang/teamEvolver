// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/api/client";
import KnowledgeBaseMiningView, { resolveKnowledgeHref } from "./KnowledgeBaseMiningView";

vi.mock("@/api/client", () => ({
  api: vi.fn(),
  ApiError: class ApiError extends Error {},
  tenantHeaders: () => new Headers(),
}));

vi.mock("@/lib/toast", () => ({
  toastErr: vi.fn(),
  toastOk: vi.fn(),
}));

const mockedApi = vi.mocked(api);

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
