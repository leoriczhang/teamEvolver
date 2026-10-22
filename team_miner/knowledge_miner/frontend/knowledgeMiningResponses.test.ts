import { describe, expect, it } from "vitest";

import {
  normalizeCompileCapabilities,
  normalizeKnowledgeContent,
  normalizeKnowledgeTree,
  normalizeMiningConfig,
  normalizeWikiGraph,
} from "./knowledgeMiningResponses";

const account = "test-account";
const sourceRoot = "viking://resources/test/input";
const wikiRoot = "viking://resources/test/wiki";

describe("knowledge mining response normalization", () => {
  it("maps backend account records and URI settings while retaining legacy config responses", () => {
    expect(normalizeMiningConfig({
      accounts: [{ account_id: account }],
      account_id: account,
      source_uri: sourceRoot,
      target_uri: wikiRoot,
      skill_uri: "viking://skills/test",
      endpoint: "http://openviking.test",
    })).toMatchObject({
      accounts: [account],
      current: account,
      source_root: sourceRoot,
      wiki_root: wikiRoot,
      skill_uri: "viking://skills/test",
      endpoint: "http://openviking.test",
    });

    const legacy = {
      accounts: [account], current: account, source_root: sourceRoot, wiki_root: wikiRoot,
      skill_uri: "viking://skills/test", endpoint: "http://openviking.test", account_source: "tenant",
    };
    expect(normalizeMiningConfig(legacy)).toMatchObject(legacy);
  });

  it("reads tree wrappers and directory flags without losing existing flat entries", () => {
    const directory = { uri: `${sourceRoot}/资料`, name: "资料", isDir: true };
    const file = { uri: `${sourceRoot}/资料/说明.md`, name: "说明.md", isDir: false, size: 12 };
    for (const tree of [[directory, file], { entries: [directory, file] }]) {
      expect(normalizeKnowledgeTree({ account, kind: "source", tree }, "source", sourceRoot, account))
        .toMatchObject({
          account, kind: "source", root_uri: sourceRoot, exists: true,
          entries: [
            { uri: directory.uri, name: "资料", is_dir: true },
            { uri: file.uri, name: "说明.md", is_dir: false, size: 12 },
          ],
        });
    }
    const legacy = {
      account, kind: "wiki" as const, root_uri: wikiRoot, exists: false, entries: [],
    };
    expect(normalizeKnowledgeTree(legacy, "wiki", wikiRoot, account)).toMatchObject(legacy);
  });

  it("rejects malformed trees instead of presenting an empty successful directory", () => {
    for (const raw of [
      null,
      {},
      { tree: null },
      { tree: { error: "directory service unavailable" } },
      { tree: "not a directory listing" },
      { tree: [null] },
      { tree: [{ name: "missing URI", isDir: true }] },
    ]) {
      expect(() => normalizeKnowledgeTree(raw, "source", sourceRoot, account)).toThrow();
    }
  });

  it("unwraps textual content and derives file metadata while rejecting nontext bodies", () => {
    const uri = `${sourceRoot}/操作说明.md`;
    for (const content of ["# 测试正文\n", { content: "# 测试正文\n" }]) {
      expect(normalizeKnowledgeContent({ uri, content }, "source", account)).toMatchObject({
        account, kind: "source", uri, name: "操作说明.md", content: "# 测试正文\n", editable: true,
      });
    }
    const legacy = { account, kind: "wiki", uri: `${wikiRoot}/说明.md`, name: "保留名称", content: "", editable: false };
    expect(normalizeKnowledgeContent(legacy, "wiki", account)).toMatchObject(legacy);
    for (const content of [null, 42, ["text"], {}, { content: 42 }]) {
      expect(() => normalizeKnowledgeContent({ uri, content }, "source", account)).toThrow();
    }
  });

  it("reads nested capabilities and never grants creation from an error payload", () => {
    expect(normalizeCompileCapabilities({
      account, capabilities: { configured: true, can_create: true, reason_code: null },
      last_compile_time: "2026-10-22T00:00:00Z",
    }, account)).toMatchObject({
      account, configured: true, can_create: true, reason_code: null,
      last_compile_time: "2026-10-22T00:00:00Z",
    });
    expect(normalizeCompileCapabilities({ account, configured: false, can_create: false }, account))
      .toMatchObject({ account, configured: false, can_create: false });

    for (const raw of [
      { account, capabilities: { error: { code: "PROBE_FAILED" } } },
      { account, error: "probe failed", can_create: true },
      { account, capabilities: { error: "probe failed", configured: true, can_create: true } },
    ]) {
      let canCreate = false;
      try {
        canCreate = normalizeCompileCapabilities(raw, account).can_create;
      } catch (error) {
        expect(error).toBeInstanceOf(Error);
      }
      expect(canCreate, JSON.stringify(raw)).toBe(false);
    }
  });

  it("derives graph counts from backend nodes and links and preserves legacy graph metadata", () => {
    const nodes = [{ id: `${wikiRoot}/a.md` }, { id: `${wikiRoot}/b.md` }];
    const links = [{ source: nodes[0].id, target: nodes[1].id }];
    for (const graph of [{ nodes, links }, { nodes, edges: links }]) {
      expect(normalizeWikiGraph({ graph, html: "<html>测试图谱</html>" }, account, wikiRoot))
        .toMatchObject({ account, wiki_root: wikiRoot, page_count: 2, edge_count: 1, html: "<html>测试图谱</html>" });
    }
    const legacy = {
      account, wiki_root: wikiRoot, source: "openviking-api", renderer: "local-html-fallback",
      page_count: 3, edge_count: 2, generated_at: "2026-10-22T00:00:00Z", html: "<html>旧图谱</html>",
    };
    expect(normalizeWikiGraph(legacy, account, wikiRoot)).toMatchObject(legacy);
  });
});
