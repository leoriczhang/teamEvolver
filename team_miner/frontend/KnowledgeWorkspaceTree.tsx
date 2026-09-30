import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, File, Folder } from "lucide-react";

import { cn } from "@/lib/utils";

export interface KnowledgeWorkspaceEntry {
  uri: string;
  name: string;
  is_dir: boolean;
  size?: number | null;
  modified_at?: string;
}

export interface KnowledgeWorkspaceTreeData {
  root_uri: string;
  exists: boolean;
  entries: KnowledgeWorkspaceEntry[];
}

interface TreeNode extends KnowledgeWorkspaceEntry {
  children: TreeNode[];
}

function formatBytes(value?: number | null) {
  if (value == null || !Number.isFinite(Number(value))) return "";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function buildTree(entries: KnowledgeWorkspaceEntry[], rootUri: string): TreeNode[] {
  const byUri = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const entry of entries) byUri.set(entry.uri, { ...entry, children: [] });
  for (const entry of entries) {
    const node = byUri.get(entry.uri)!;
    const parentUri = entry.uri.slice(0, entry.uri.lastIndexOf("/"));
    const parent = byUri.get(parentUri);
    if (parent && parent.uri !== rootUri) parent.children.push(node);
    else roots.push(node);
  }
  const sortNodes = (nodes: TreeNode[]) => {
    nodes.sort((left, right) => Number(right.is_dir) - Number(left.is_dir) || left.name.localeCompare(right.name));
    nodes.forEach((node) => sortNodes(node.children));
  };
  sortNodes(roots);
  return roots;
}

function filterTree(nodes: TreeNode[], query: string): TreeNode[] {
  if (!query) return nodes;
  const lowered = query.toLowerCase();
  return nodes.flatMap((node) => {
    const children = filterTree(node.children, query);
    return node.name.toLowerCase().includes(lowered) || children.length
      ? [{ ...node, children }]
      : [];
  });
}

export function KnowledgeWorkspaceTree({
  tree,
  emptyText,
  selectedUri,
  selectedDirectoryUri,
  query,
  onSelect,
}: {
  tree: KnowledgeWorkspaceTreeData | null;
  emptyText: string;
  selectedUri: string;
  selectedDirectoryUri: string;
  query: string;
  onSelect: (entry: KnowledgeWorkspaceEntry) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const selectedRowRef = useRef<HTMLButtonElement | null>(null);
  const lastScrolledUriRef = useRef("");
  const nodes = useMemo(
    () => filterTree(buildTree(tree?.entries || [], tree?.root_uri || ""), query.trim()),
    [query, tree],
  );
  const ancestorDirs = useMemo(() => {
    const paths = new Map<string, string[]>();
    const walk = (items: TreeNode[], ancestors: string[]) => {
      for (const item of items) {
        paths.set(item.uri, ancestors);
        if (item.children.length) walk(item.children, [...ancestors, item.uri]);
      }
    };
    walk(buildTree(tree?.entries || [], tree?.root_uri || ""), []);
    return paths;
  }, [tree]);

  useEffect(() => {
    if (!selectedUri) return;
    const ancestors = ancestorDirs.get(selectedUri);
    if (!ancestors?.length) return;
    setExpanded((previous) => {
      if (ancestors.every((uri) => previous.has(uri))) return previous;
      const next = new Set(previous);
      for (const uri of ancestors) next.add(uri);
      return next;
    });
  }, [selectedUri, ancestorDirs]);

  useEffect(() => {
    if (!selectedUri || lastScrolledUriRef.current === selectedUri) return;
    const row = selectedRowRef.current;
    if (!row) return;
    lastScrolledUriRef.current = selectedUri;
    let container = row.parentElement;
    while (container) {
      const style = window.getComputedStyle(container);
      if (
        (style.overflowY === "auto" || style.overflowY === "scroll")
        && container.scrollHeight > container.clientHeight
      ) break;
      container = container.parentElement;
    }
    if (container) {
      const rowRect = row.getBoundingClientRect();
      const boxRect = container.getBoundingClientRect();
      container.scrollTop += rowRect.top - boxRect.top - (boxRect.height - rowRect.height) / 2;
    } else {
      row.scrollIntoView?.({ block: "center" });
    }
  }, [selectedUri, expanded]);

  if (!tree) {
    return <div className="grid h-full min-h-40 place-items-center text-xs text-muted-foreground">正在读取目录…</div>;
  }
  if (!tree.exists || !tree.entries.length) {
    if (!emptyText) return <div className="h-full" aria-hidden="true" />;
    return <div className="grid h-full min-h-40 place-items-center px-6 text-center text-xs leading-5 text-muted-foreground">{emptyText}</div>;
  }

  function toggle(uri: string) {
    setExpanded((previous) => {
      const next = new Set(previous);
      if (next.has(uri)) next.delete(uri);
      else next.add(uri);
      return next;
    });
  }

  function renderNode(node: TreeNode, depth: number): React.ReactNode {
    const open = expanded.has(node.uri) || Boolean(query.trim());
    const Icon = node.is_dir ? Folder : File;
    return (
      <div key={node.uri}>
        <button
          ref={selectedUri === node.uri ? selectedRowRef : undefined}
          type="button"
          role="treeitem"
          aria-expanded={node.is_dir ? open : undefined}
          aria-label={node.name}
          title={node.uri}
          className={cn(
            "group flex min-h-8 w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-accent-soft/60",
            selectedUri === node.uri && "bg-accent-soft text-accent",
            node.is_dir && selectedDirectoryUri === node.uri && "ring-1 ring-inset ring-accent/45",
          )}
          style={{ paddingLeft: `${8 + depth * 16}px` }}
          onClick={() => {
            if (node.is_dir) toggle(node.uri);
            onSelect(node);
          }}
        >
          {node.is_dir
            ? open ? <ChevronDown className="size-3 text-muted-soft" /> : <ChevronRight className="size-3 text-muted-soft" />
            : <span className="w-3" />}
          <Icon className={cn("size-3.5 shrink-0", node.is_dir ? "text-accent" : "text-slate-500")} />
          <span className="min-w-0 flex-1 truncate font-medium">{node.name}</span>
          {!node.is_dir && node.size != null && <span className="shrink-0 text-[10px] text-muted-soft">{formatBytes(node.size)}</span>}
        </button>
        {node.is_dir && open && node.children.map((child) => renderNode(child, depth + 1))}
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto p-2" role="tree">
      {nodes.length ? nodes.map((node) => renderNode(node, 0)) : (
        <div className="grid min-h-40 place-items-center text-xs text-muted-foreground">没有匹配的文件</div>
      )}
    </div>
  );
}
