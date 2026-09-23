import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Code2,
  Database,
  Eye,
  FileCode2,
  FileJson,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "@/api/client";
import { Empty, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toastErr } from "@/lib/toast";
import { cn } from "@/lib/utils";

type SourceId = "session" | "skill";
type SourceFilter = "all" | SourceId;

type AssetFamily = {
  key: string;
  label: string;
  object_count: number;
};

export type PlatformAssetSource = {
  id: SourceId;
  label: string;
  description: string;
  backend: "postgres" | "local";
  backend_label: string;
  configured_backend: string;
  root_uri: string;
  object_count: number;
  family_count: number;
  truncated: boolean;
  error?: string;
  families: AssetFamily[];
};

export type PlatformAssetEntry = {
  uri: string;
  key: string;
  name: string;
  is_dir: boolean;
  source_id: SourceId;
  backend: "postgres" | "local";
  relative_path: string;
  purpose?: string;
};

export type PlatformAssetsTreeResponse = {
  tenant_id: string;
  read_only: boolean;
  entries: PlatformAssetEntry[];
  sources: PlatformAssetSource[];
};

type PlatformAssetContent = {
  uri: string;
  key: string;
  name: string;
  source_id: SourceId;
  source_label: string;
  backend: "postgres" | "local";
  backend_label: string;
  purpose?: string;
  size: number;
  is_text: boolean;
  content: string;
};

type AssetTreeNode = {
  entry: PlatformAssetEntry;
  children: AssetTreeNode[];
};

const MARKDOWN_EXTENSIONS = new Set(["md", "markdown", "mdx"]);
const JSON_EXTENSIONS = new Set(["json", "jsonl"]);
const CODE_EXTENSIONS = new Set([
  "css",
  "go",
  "js",
  "jsx",
  "py",
  "rs",
  "sh",
  "sql",
  "ts",
  "tsx",
  "xml",
  "yaml",
  "yml",
]);

export default function PlatformAssetsView({ active }: { active: boolean }) {
  const [data, setData] = useState<PlatformAssetsTreeResponse | null>(null);
  const [selected, setSelected] = useState<PlatformAssetEntry | null>(null);
  const [content, setContent] = useState<PlatformAssetContent | null>(null);
  const [sourceFilter, setSourceFilter] = useState<SourceFilter>("all");
  const [filter, setFilter] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [viewMode, setViewMode] = useState<"preview" | "source">("preview");
  const [loading, setLoading] = useState(false);
  const [contentLoading, setContentLoading] = useState(false);

  const loadTree = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api<PlatformAssetsTreeResponse>("/api/platform-assets/tree");
      setData(result);
      setExpanded((previous) => {
        if (previous.size) return previous;
        return new Set([
          ...result.sources.map((source) => source.root_uri),
          ...result.entries
            .filter((entry) => entry.is_dir && !entry.relative_path.includes("/"))
            .map((entry) => entry.uri),
        ]);
      });
      setSelected((current) => (
        current && result.entries.some((entry) => entry.uri === current.uri)
          ? current
          : null
      ));
    } catch (error: any) {
      toastErr("加载平台资产失败", error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void loadTree();
  }, [active, loadTree]);

  const visibleSources = useMemo(
    () => (data?.sources || []).filter(
      (source) => sourceFilter === "all" || source.id === sourceFilter,
    ),
    [data?.sources, sourceFilter],
  );
  const visibleEntries = useMemo(
    () => (data?.entries || []).filter(
      (entry) => sourceFilter === "all" || entry.source_id === sourceFilter,
    ),
    [data?.entries, sourceFilter],
  );
  const tree = useMemo(
    () => filterTree(
      buildTree(visibleSources, visibleEntries),
      filter.trim().toLocaleLowerCase(),
    ),
    [filter, visibleEntries, visibleSources],
  );
  const selectedSource = useMemo(
    () => data?.sources.find((source) => source.id === selected?.source_id) || null,
    [data?.sources, selected?.source_id],
  );
  const selectedDescendants = useMemo(
    () => selected?.is_dir
      ? visibleEntries.filter((entry) => (
          entry.uri.startsWith(`${selected.uri}/`) && !entry.is_dir
        ))
      : [],
    [selected, visibleEntries],
  );
  const totalObjects = (data?.sources || []).reduce(
    (total, source) => total + source.object_count,
    0,
  );

  async function openEntry(entry: PlatformAssetEntry) {
    if (entry.is_dir) {
      setSelected(entry);
      setContent(null);
      setExpanded((previous) => {
        const next = new Set(previous);
        if (next.has(entry.uri)) next.delete(entry.uri);
        else next.add(entry.uri);
        return next;
      });
      return;
    }
    setSelected(entry);
    setContent(null);
    setContentLoading(true);
    setViewMode("preview");
    try {
      const result = await api<PlatformAssetContent>(
        `/api/platform-assets/content?uri=${encodeURIComponent(entry.uri)}`,
      );
      setContent(result);
    } catch (error: any) {
      toastErr("读取平台资产失败", error.message);
    } finally {
      setContentLoading(false);
    }
  }

  function chooseSource(next: SourceFilter) {
    setSourceFilter(next);
    setSelected(null);
    setContent(null);
  }

  return (
    <div className="w-full px-4 pb-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border py-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-border bg-muted p-0.5" role="tablist" aria-label="平台资产存储分区">
            {[
              { key: "all" as const, label: "全部" },
              { key: "session" as const, label: "Session 流水" },
              { key: "skill" as const, label: "Skill 产物" },
            ].map((item) => (
              <button
                key={item.key}
                type="button"
                role="tab"
                aria-selected={sourceFilter === item.key}
                onClick={() => chooseSource(item.key)}
                className={cn(
                  "h-7 rounded-md px-3 text-[11px] font-semibold transition-colors",
                  sourceFilter === item.key
                    ? "bg-background text-foreground shadow-sm"
                    : "text-muted-foreground hover:text-foreground",
                )}
              >
                {item.label}
              </button>
            ))}
          </div>
          <Pill tone="gray">租户 {data?.tenant_id || "default"} · 只读</Pill>
          <span className="text-xs text-muted-foreground">
            {totalObjects.toLocaleString()} 个对象
          </span>
        </div>
        <Button variant="outline" size="sm" disabled={loading} onClick={() => void loadTree()}>
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />
          刷新
        </Button>
      </div>

      <div className="mb-3 grid gap-3 md:grid-cols-2">
        {(data?.sources || []).map((source) => (
          <button
            key={source.id}
            type="button"
            onClick={() => chooseSource(source.id)}
            className={cn(
              "grid min-h-[92px] grid-cols-[40px_1fr_auto] items-start gap-3 rounded-lg border p-3 text-left transition-colors",
              sourceFilter === source.id
                ? "border-sidebar-primary bg-accent-soft"
                : "border-border bg-surface hover:bg-muted/40",
            )}
          >
            <span className={cn(
              "grid size-10 place-items-center rounded-md",
              source.backend === "postgres"
                ? "bg-blue-100 text-blue-700"
                : "bg-emerald-100 text-emerald-700",
            )}>
              {source.backend === "postgres"
                ? <Database className="size-5" />
                : <HardDrive className="size-5" />}
            </span>
            <span className="min-w-0">
              <span className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-bold">{source.label}</span>
                <Pill tone={source.error ? "red" : source.backend === "postgres" ? "blue" : "green"}>
                  {source.backend_label}
                </Pill>
              </span>
              <span className="mt-1 block text-[11px] leading-5 text-muted-foreground">
                {source.error || source.description}
              </span>
            </span>
            <span className="text-right">
              <span className="block text-xl font-bold">{source.object_count}</span>
              <span className="text-[10px] text-muted-foreground">对象</span>
            </span>
          </button>
        ))}
      </div>

      <div
        className="grid min-h-[560px] overflow-hidden rounded-lg border border-border bg-surface lg:grid-cols-[minmax(290px,0.82fr)_minmax(420px,1.35fr)] xl:grid-cols-[minmax(290px,0.82fr)_minmax(420px,1.35fr)_minmax(260px,0.68fr)]"
        style={{ height: "clamp(560px, calc(100vh - 290px), 790px)" }}
      >
        <section className="flex min-h-0 flex-col border-r border-border">
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-border px-3">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <FolderOpen className="size-4 text-amber-600" />
              资产树
              <span className="text-[11px] font-normal text-muted-foreground">
                {visibleEntries.filter((entry) => !entry.is_dir).length}
              </span>
            </div>
          </header>
          <div className="shrink-0 border-b border-border p-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="搜索逻辑键、文件名或用途"
                className="h-8 pl-8 pr-8 text-xs"
              />
              {filter && (
                <button
                  type="button"
                  aria-label="清空搜索"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                  onClick={() => setFilter("")}
                >
                  <X className="size-3.5" />
                </button>
              )}
            </div>
          </div>
          <div className="min-h-0 flex-1 overflow-auto py-1.5">
            {loading && !data ? (
              <div className="flex items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在读取存储索引…
              </div>
            ) : tree.length ? (
              tree.map((node) => (
                <AssetTreeRow
                  key={node.entry.uri}
                  node={node}
                  depth={0}
                  expanded={expanded}
                  forceExpanded={!!filter.trim()}
                  selectedUri={selected?.uri || ""}
                  onOpen={openEntry}
                />
              ))
            ) : (
              <Empty>{filter ? "没有匹配的平台资产。" : "当前分区没有平台资产。"}</Empty>
            )}
          </div>
        </section>

        <section className="flex min-h-0 flex-col border-r border-border">
          <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-border px-3">
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold">
                {selected?.name || "内容预览"}
              </div>
              <div className="truncate font-mono text-[10px] text-muted-foreground">
                {selected?.key || "从左侧选择资产"}
              </div>
            </div>
            {selected && !selected.is_dir && content?.is_text && (
              <div className="flex shrink-0 rounded-lg bg-muted p-0.5">
                <button
                  type="button"
                  title="渲染预览"
                  aria-label="渲染预览"
                  className={cn(
                    "grid size-7 place-items-center rounded-md",
                    viewMode === "preview" && "bg-background shadow-sm",
                  )}
                  onClick={() => setViewMode("preview")}
                >
                  <Eye className="size-3.5" />
                </button>
                <button
                  type="button"
                  title="查看源码"
                  aria-label="查看源码"
                  className={cn(
                    "grid size-7 place-items-center rounded-md",
                    viewMode === "source" && "bg-background shadow-sm",
                  )}
                  onClick={() => setViewMode("source")}
                >
                  <Code2 className="size-3.5" />
                </button>
              </div>
            )}
          </header>
          <div className="min-h-0 flex-1 overflow-hidden bg-background">
            {contentLoading ? (
              <div className="flex h-full items-center justify-center gap-2 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                正在读取资产…
              </div>
            ) : !selected ? (
              <div className="flex h-full items-center justify-center p-8">
                <Empty>选择目录查看对象统计，或选择文件查看内容。</Empty>
              </div>
            ) : selected.is_dir ? (
              <DirectorySummary entry={selected} descendants={selectedDescendants} />
            ) : content ? (
              <AssetPreview content={content} mode={viewMode} />
            ) : (
              <div className="flex h-full items-center justify-center p-8">
                <Empty>该资产暂时无法预览。</Empty>
              </div>
            )}
          </div>
          <footer className="flex h-8 shrink-0 items-center justify-between border-t border-border px-3 text-[10px] text-muted-foreground">
            <span>{selected?.is_dir ? "逻辑目录" : content ? fileKind(content.name) : "只读资产"}</span>
            <span>{content ? formatBytes(content.size) : `${selectedDescendants.length} 个对象`}</span>
          </footer>
        </section>

        <aside className="hidden min-h-0 flex-col bg-surface-subtle/40 xl:flex">
          <header className="flex h-12 shrink-0 items-center border-b border-border px-3 text-sm font-semibold">
            存储信息
          </header>
          <div className="min-h-0 flex-1 overflow-auto p-4">
            {selectedSource ? (
              <div className="space-y-5">
                <section>
                  <div className="mb-2 flex items-center gap-2">
                    {selectedSource.backend === "postgres"
                      ? <Database className="size-4 text-blue-700" />
                      : <HardDrive className="size-4 text-emerald-700" />}
                    <span className="text-sm font-bold">{selectedSource.backend_label}</span>
                  </div>
                  <div className="space-y-2 text-xs">
                    <MetaRow label="数据域" value={selectedSource.label} />
                    <MetaRow label="租户" value={data?.tenant_id || "default"} mono />
                    <MetaRow label="逻辑键" value={selected?.key || selectedSource.root_uri} mono />
                    <MetaRow label="对象数" value={String(selectedSource.object_count)} />
                  </div>
                </section>
                <section className="border-t border-border pt-4">
                  <div className="mb-2 text-xs font-bold">资产用途</div>
                  <p className="text-xs leading-6 text-muted-foreground">
                    {selected?.purpose || selectedSource.description}
                  </p>
                </section>
                <section className="border-t border-border pt-4">
                  <div className="mb-2 text-xs font-bold">分类分布</div>
                  <div className="space-y-2">
                    {selectedSource.families.map((family) => (
                      <div key={family.key} className="grid grid-cols-[1fr_auto] gap-2 text-[11px]">
                        <span className="min-w-0">
                          <span className="block truncate font-mono text-foreground">{family.key}</span>
                          <span className="block truncate text-muted-foreground">{family.label}</span>
                        </span>
                        <span className="font-semibold tabular-nums">{family.object_count}</span>
                      </div>
                    ))}
                    {!selectedSource.families.length && (
                      <span className="text-xs text-muted-foreground">暂无对象</span>
                    )}
                  </div>
                </section>
              </div>
            ) : (
              <Empty>选择一个存储分区或资产查看详情。</Empty>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}

function AssetTreeRow({
  node,
  depth,
  expanded,
  forceExpanded,
  selectedUri,
  onOpen,
}: {
  node: AssetTreeNode;
  depth: number;
  expanded: Set<string>;
  forceExpanded: boolean;
  selectedUri: string;
  onOpen: (entry: PlatformAssetEntry) => void;
}) {
  const open = forceExpanded || expanded.has(node.entry.uri);
  const isSource = depth === 0;
  return (
    <>
      <button
        type="button"
        title={node.entry.purpose || node.entry.key}
        onClick={() => void onOpen(node.entry)}
        className={cn(
          "group flex w-full items-center gap-1.5 py-1.5 pr-2 text-left text-xs hover:bg-muted/70",
          selectedUri === node.entry.uri && "bg-muted font-semibold text-sidebar-primary",
          isSource && "font-semibold",
        )}
        style={{ paddingLeft: `${10 + depth * 14}px` }}
      >
        {node.entry.is_dir ? (
          open
            ? <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
            : <ChevronRight className="size-3 shrink-0 text-muted-foreground" />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        {node.entry.is_dir ? (
          open
            ? <FolderOpen className="size-4 shrink-0 text-amber-600" />
            : <Folder className="size-4 shrink-0 text-amber-600" />
        ) : (
          <AssetFileIcon name={node.entry.name} />
        )}
        <span className="min-w-0 flex-1">
          <span className="block truncate">{node.entry.name}</span>
          {isSource && node.entry.purpose && (
            <span className="block truncate text-[10px] font-normal text-muted-foreground">
              {node.entry.purpose}
            </span>
          )}
        </span>
      </button>
      {node.entry.is_dir && open && node.children.map((child) => (
        <AssetTreeRow
          key={child.entry.uri}
          node={child}
          depth={depth + 1}
          expanded={expanded}
          forceExpanded={forceExpanded}
          selectedUri={selectedUri}
          onOpen={onOpen}
        />
      ))}
    </>
  );
}

function DirectorySummary({
  entry,
  descendants,
}: {
  entry: PlatformAssetEntry;
  descendants: PlatformAssetEntry[];
}) {
  const extensions = descendants.reduce<Record<string, number>>((counts, item) => {
    const extension = fileExtension(item.name) || "其他";
    counts[extension] = (counts[extension] || 0) + 1;
    return counts;
  }, {});
  return (
    <div className="h-full overflow-auto p-5">
      <div className="flex items-center gap-3">
        <span className="grid size-10 place-items-center rounded-md bg-amber-100 text-amber-700">
          <FolderOpen className="size-5" />
        </span>
        <div>
          <div className="text-base font-bold">{entry.name}</div>
          <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{entry.key}</div>
        </div>
      </div>
      <p className="mt-5 text-sm leading-7 text-muted-foreground">
        {entry.purpose || "平台运行产生的只读内部资产。"}
      </p>
      <div className="mt-5 grid grid-cols-2 gap-3">
        <div className="border-l-2 border-blue-500 pl-3">
          <div className="text-2xl font-bold tabular-nums">{descendants.length}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">目录内对象</div>
        </div>
        <div className="border-l-2 border-emerald-500 pl-3">
          <div className="text-2xl font-bold tabular-nums">{Object.keys(extensions).length}</div>
          <div className="mt-1 text-[11px] text-muted-foreground">文件类型</div>
        </div>
      </div>
      {!!Object.keys(extensions).length && (
        <div className="mt-6 border-t border-border pt-4">
          <div className="mb-3 text-xs font-bold">文件类型分布</div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(extensions)
              .sort((left, right) => right[1] - left[1])
              .map(([extension, count]) => (
                <Pill key={extension} tone="gray">
                  {extension} · {count}
                </Pill>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}

function AssetPreview({
  content,
  mode,
}: {
  content: PlatformAssetContent;
  mode: "preview" | "source";
}) {
  if (!content.is_text) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Empty>该对象是二进制内容，当前仅显示元数据。</Empty>
      </div>
    );
  }
  if (mode === "source") {
    return <SourcePreview content={content.content} />;
  }
  const extension = fileExtension(content.name);
  if (MARKDOWN_EXTENSIONS.has(extension)) {
    return (
      <div className="h-full overflow-auto px-5 py-4">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {content.content}
        </ReactMarkdown>
      </div>
    );
  }
  if (JSON_EXTENSIONS.has(extension)) {
    return <SourcePreview content={formatJson(content.content)} />;
  }
  return <SourcePreview content={content.content} />;
}

function SourcePreview({ content }: { content: string }) {
  return (
    <pre className="h-full overflow-auto whitespace-pre-wrap break-words p-4 font-mono text-xs leading-5">
      {content || "（空文件）"}
    </pre>
  );
}

function MetaRow({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-[56px_1fr] gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("min-w-0 break-all text-foreground", mono && "font-mono text-[10px]")}>
        {value}
      </span>
    </div>
  );
}

function buildTree(
  sources: PlatformAssetSource[],
  entries: PlatformAssetEntry[],
): AssetTreeNode[] {
  const nodes = new Map<string, AssetTreeNode>();
  for (const source of sources) {
    nodes.set(source.root_uri, {
      entry: {
        uri: source.root_uri,
        key: source.root_uri,
        name: source.label,
        is_dir: true,
        source_id: source.id,
        backend: source.backend,
        relative_path: "",
        purpose: `${source.backend_label} · ${source.description}`,
      },
      children: [],
    });
  }
  for (const entry of entries) {
    nodes.set(entry.uri, { entry, children: [] });
  }
  const roots: AssetTreeNode[] = [];
  for (const source of sources) {
    const root = nodes.get(source.root_uri);
    if (root) roots.push(root);
  }
  for (const entry of entries) {
    const node = nodes.get(entry.uri);
    const parent = nodes.get(parentAssetUri(entry));
    if (node && parent) parent.children.push(node);
  }
  const sortNodes = (items: AssetTreeNode[]) => {
    items.sort((left, right) => (
      Number(right.entry.is_dir) - Number(left.entry.is_dir)
      || left.entry.name.localeCompare(right.entry.name)
    ));
    items.forEach((item) => sortNodes(item.children));
  };
  sortNodes(roots);
  return roots;
}

function parentAssetUri(entry: PlatformAssetEntry): string {
  const index = entry.relative_path.lastIndexOf("/");
  return index < 0
    ? `platform://${entry.source_id}`
    : `platform://${entry.source_id}/${entry.relative_path.slice(0, index)}`;
}

function filterTree(nodes: AssetTreeNode[], query: string): AssetTreeNode[] {
  if (!query) return nodes;
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    const haystack = [
      node.entry.name,
      node.entry.key,
      node.entry.purpose || "",
    ].join(" ").toLocaleLowerCase();
    return haystack.includes(query) || children.length
      ? [{ ...node, children }]
      : [];
  });
}

function AssetFileIcon({ name }: { name: string }) {
  const extension = fileExtension(name);
  if (JSON_EXTENSIONS.has(extension)) {
    return <FileJson className="size-4 shrink-0 text-amber-600" />;
  }
  if (CODE_EXTENSIONS.has(extension)) {
    return <FileCode2 className="size-4 shrink-0 text-violet-600" />;
  }
  return <FileText className="size-4 shrink-0 text-blue-600" />;
}

function fileExtension(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index + 1).toLocaleLowerCase() : "";
}

function fileKind(name: string): string {
  const extension = fileExtension(name);
  if (MARKDOWN_EXTENSIONS.has(extension)) return "Markdown";
  if (JSON_EXTENSIONS.has(extension)) return extension.toUpperCase();
  if (CODE_EXTENSIONS.has(extension)) return extension.toUpperCase();
  return extension ? extension.toUpperCase() : "文本";
}

function formatJson(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    if (content.trim().split(/\r?\n/).length > 1) {
      return content
        .split(/\r?\n/)
        .map((line) => {
          try {
            return JSON.stringify(JSON.parse(line), null, 2);
          } catch {
            return line;
          }
        })
        .join("\n");
    }
    return content;
  }
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
