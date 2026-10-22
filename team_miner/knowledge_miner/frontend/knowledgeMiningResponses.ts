import type {
  CompileCapabilities, KnowledgeContent, MiningConfig, TreeEntry, TreeResponse, WikiGraphResponse,
} from "./KnowledgeBaseMiningViewShared";

function record(value: unknown): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("知识库挖掘接口返回格式无效");
  return value as Record<string, any>;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** Keep the view model separate from the tenant configuration API contract. */
export function normalizeMiningConfig(value: unknown): MiningConfig {
  const raw = record(value);
  const current = text(raw.account_id) || text(raw.current);
  const accounts = Array.isArray(raw.accounts)
    ? raw.accounts.map((item: unknown) => typeof item === "string" ? item : text(record(item).account_id))
    : [current];
  const sourceRoot = text(raw.source_uri) || text(raw.source_root);
  const wikiRoot = text(raw.target_uri) || text(raw.wiki_root);
  if (!current || !accounts.includes(current) || !sourceRoot || !wikiRoot || !text(raw.skill_uri)) {
    throw new Error("知识库挖掘配置不完整，请检查租户 Miner 配置");
  }
  return {
    accounts: [...new Set(accounts.filter(Boolean))], current, source_root: sourceRoot, wiki_root: wikiRoot,
    skill_uri: raw.skill_uri, endpoint: text(raw.endpoint),
    account_source: text(raw.account_source) || "tenant-config", error: text(raw.error) || undefined,
  };
}

export function normalizeKnowledgeTree(
  value: unknown, kind: "source" | "wiki", root: string, account: string,
): TreeResponse {
  const raw = record(value);
  const data = raw.tree === undefined ? raw : raw.tree;
  const envelope = Array.isArray(data) ? {} : record(data);
  if (raw.error || raw.status === "error" || envelope.error || envelope.status === "error") {
    throw new Error("知识库目录读取失败");
  }
  if (envelope.truncated || envelope.incomplete || envelope.partial) throw new Error("目录列表不完整，请刷新后重试");
  const rows = Array.isArray(data) ? data : envelope.entries ?? envelope.items;
  if (!Array.isArray(rows)) throw new Error("知识库目录接口未返回有效条目列表");
  const entries: TreeEntry[] = rows.map((item) => {
    const entry = record(item);
    if (!text(entry.uri)) throw new Error("知识库目录条目缺少 URI");
    const directory = entry.is_dir ?? entry.isDir;
    if (directory !== undefined && typeof directory !== "boolean") throw new Error("知识库目录类型无效");
    return {
      ...entry, uri: entry.uri, name: text(entry.name) || entry.uri.split("/").pop(),
      is_dir: directory ?? entry.type === "directory",
    };
  });
  return {
    account: text(raw.account) || account, kind, entries,
    root_uri: text(raw.root_uri) || text(envelope.root_uri) || root,
    exists: typeof raw.exists === "boolean" ? raw.exists
      : typeof envelope.exists === "boolean" ? envelope.exists : true,
  };
}

export function normalizeKnowledgeContent(value: unknown, kind: "source" | "wiki", account: string): KnowledgeContent {
  const raw = record(value);
  const content = typeof raw.content === "string" ? raw.content : record(raw.content).content;
  if (typeof content !== "string" || !text(raw.uri)) throw new Error("知识文件接口未返回有效正文");
  return {
    ...raw, account: text(raw.account) || account, kind, uri: raw.uri,
    name: text(raw.name) || raw.uri.split("/").pop(), content,
    editable: typeof raw.editable === "boolean" ? raw.editable : /\.md$/i.test(raw.uri),
  };
}

export function normalizeCompileCapabilities(value: unknown, account: string): CompileCapabilities {
  const raw = record(value);
  const capabilities = raw.capabilities === undefined ? raw : record(raw.capabilities);
  if (raw.error || raw.status === "error" || capabilities.error || capabilities.status === "error") {
    throw new Error("OpenViking Compile 能力检测失败");
  }
  if (typeof capabilities.can_create !== "boolean") throw new Error("OpenViking 未返回有效的 Compile 能力信息");
  return {
    ...capabilities, account: text(raw.account) || account,
    configured: typeof capabilities.configured === "boolean" ? capabilities.configured : null,
    can_create: capabilities.can_create,
    last_compile_time: raw.last_compile_time ?? capabilities.last_compile_time ?? null,
  };
}

export function normalizeWikiGraph(value: unknown, account: string, wikiRoot: string): WikiGraphResponse {
  const raw = record(value);
  if (typeof raw.html !== "string") throw new Error("知识库图谱任务未返回 HTML");
  const graph = raw.graph === undefined ? null : record(raw.graph);
  const nodes = graph?.nodes;
  const links = graph?.links ?? graph?.edges;
  if (graph && (!Array.isArray(nodes) || !Array.isArray(links))) throw new Error("知识库图谱节点或关系无效");
  return {
    account, wiki_root: wikiRoot, source: "openviking-api", renderer: "local-html-fallback",
    page_count: graph ? nodes.length : Number(raw.page_count || 0),
    edge_count: graph ? links.length : Number(raw.edge_count || 0),
    generated_at: text(raw.generated_at), html: raw.html,
  };
}
