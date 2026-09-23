import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { api } from "@/api/client";
import { toastErr } from "@/lib/toast";
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  FileText,
  FolderOpen,
  Search,
  X,
  Globe,
  Loader2,
} from "lucide-react";

type DocPage = {
  id: string;
  path: string;
  title: string;
  filename: string;
};

type DocSection = {
  id: string;
  label: string;
  section: string;
  pages: DocPage[];
};

type DocsTreeResp = {
  lang: string;
  sections: DocSection[];
};

type DocPageResp = {
  path: string;
  title: string;
  content: string;
};

type SearchHit = {
  path: string;
  title: string;
  snippet: string;
  score: number;
};

type SearchResp = {
  query: string;
  lang: string;
  count: number;
  results: SearchHit[];
};

function resolveAsset(src: string | undefined): string {
  if (!src) return "";
  if (src.startsWith("http://") || src.startsWith("https://") || src.startsWith("data:")) {
    return src;
  }
  if (src.startsWith("/docs-assets/") || src.startsWith("./")) {
    return src;
  }
  if (src.startsWith("/assets/")) {
    return "/docs-assets/" + src.slice("/assets/".length);
  }
  if (src.startsWith("../assets/")) {
    return "/docs-assets/" + src.slice("../assets/".length);
  }
  if (src.startsWith("assets/")) {
    return "/docs-assets/" + src.slice("assets/".length);
  }
  return src;
}

function CodeEntry({ children }: { children?: React.ReactNode }) {
  return (
    <code className="rounded bg-muted px-1 py-0.5 text-[12px] font-mono text-accent before:content-[''] after:content-['']">
      {children}
    </code>
  );
}

export default function DocsView({ active }: { active: boolean; user?: any }) {
  const [lang, setLang] = useState<"zh" | "en">("zh");
  const [tree, setTree] = useState<DocSection[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [pageContent, setPageContent] = useState<DocPageResp | null>(null);
  const [pageLoading, setPageLoading] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchHit[]>([]);
  const [searchLoading, setSearchLoading] = useState(false);
  const [treeLoading, setTreeLoading] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const searchInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const firstPage = useMemo(() => {
    for (const s of tree) {
      if (s.pages.length > 0) return s.pages[0].path;
    }
    return null;
  }, [tree]);

  const loadTree = useCallback(
    async (targetLang: "zh" | "en") => {
      setTreeLoading(true);
      try {
        const resp = await api<DocsTreeResp>(`/api/docs/tree?lang=${targetLang}`);
        setTree(resp.sections);
        const sectionState: Record<string, boolean> = {};
        resp.sections.forEach((s) => {
          sectionState[s.id] = true;
        });
        setExpanded(sectionState);
      } catch (e: any) {
        toastErr("文档目录加载失败", e.message);
      } finally {
        setTreeLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (!active) return;
    loadTree(lang);
  }, [active, lang, loadTree]);

  const loadPage = useCallback(
    async (path: string) => {
      setPageLoading(true);
      setSearchOpen(false);
      try {
        const resp = await api<DocPageResp>(
          `/api/docs/page?path=${encodeURIComponent(path)}`
        );
        setPageContent(resp);
        setActivePath(path);
        if (contentRef.current) {
          contentRef.current.scrollTop = 0;
        }
      } catch (e: any) {
        toastErr("文档加载失败", e.message);
      } finally {
        setPageLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    if (firstPage && !activePath) {
      loadPage(firstPage);
    }
  }, [firstPage, activePath, loadPage]);

  useEffect(() => {
    if (activePath && lang) {
      const otherLang = lang === "zh" ? "en" : "zh";
      const pathParts = activePath.split("/");
      if (pathParts[0] === "zh" || pathParts[0] === "en") {
        pathParts[0] = otherLang;
      }
    }
  }, [lang, activePath]);

  useEffect(() => {
    if (!searchQuery.trim()) {
      setSearchResults([]);
      setSearchLoading(false);
      return;
    }
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    setSearchLoading(true);
    searchTimerRef.current = setTimeout(async () => {
      try {
        const resp = await api<SearchResp>(
          `/api/docs/search?q=${encodeURIComponent(searchQuery)}&lang=${lang}&limit=20`
        );
        setSearchResults(resp.results);
      } catch {
        setSearchResults([]);
      } finally {
        setSearchLoading(false);
      }
    }, 250);
    return () => {
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);
    };
  }, [searchQuery, lang]);

  useEffect(() => {
    if (searchOpen && searchInputRef.current) {
      searchInputRef.current.focus();
    }
  }, [searchOpen]);

  const toggleSection = (id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const switchLang = (next: "zh" | "en") => {
    if (next === lang) return;
    setLang(next);
    setPageContent(null);
    setActivePath(null);
    setSearchOpen(false);
    setSearchQuery("");
    setSearchResults([]);
  };

  const titleForPath = (path: string): string => {
    for (const section of tree) {
      for (const page of section.pages) {
        if (page.path === path) return page.title;
      }
    }
    const parts = path.split("/");
    return parts[parts.length - 1]?.replace(/\.md$/, "") || "";
  };

  const handleSearchHit = (hit: SearchHit) => {
    loadPage(hit.path);
  };

  return (
    <div className="flex h-[calc(100vh-60px-56px)] min-h-0">
      {/* Sidebar */}
      <aside className="flex w-[280px] shrink-0 flex-col border-r border-line bg-surface-subtle/40">
        <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
          <div className="relative flex-1">
            <Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              ref={searchInputRef}
              value={searchQuery}
              onChange={(e) => {
                setSearchQuery(e.target.value);
                if (!searchOpen) setSearchOpen(true);
              }}
              onFocus={() => setSearchOpen(true)}
              placeholder="搜索文档…"
              className="h-8 pl-8 pr-7 text-xs"
            />
            {searchQuery && (
              <button
                type="button"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                onClick={() => {
                  setSearchQuery("");
                  setSearchResults([]);
                  setSearchOpen(false);
                }}
              >
                <X className="size-3.5" />
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={() => switchLang(lang === "zh" ? "en" : "zh")}
            className="flex h-8 items-center gap-1 rounded-md border border-border bg-surface px-2 text-[11px] font-semibold text-muted-foreground transition hover:border-border-strong hover:text-foreground"
            title={lang === "zh" ? "Switch to English" : "切换到中文"}
          >
            <Globe className="size-3" />
            {lang === "zh" ? "EN" : "中"}
          </button>
        </div>

        {searchOpen && searchQuery ? (
          <div className="flex-1 overflow-auto p-2">
            <div className="mb-1.5 px-2 text-[10px] font-[800] uppercase tracking-wider text-muted-soft">
              搜索结果 · {searchLoading ? "搜索中…" : `${searchResults.length} 条`}
            </div>
            {searchLoading && searchResults.length === 0 && (
              <div className="flex items-center justify-center py-10 text-xs text-muted-foreground">
                <Loader2 className="mr-2 size-3.5 animate-spin" />
                正在搜索…
              </div>
            )}
            {!searchLoading && searchResults.length === 0 && (
              <div className="px-2 py-6 text-center text-xs text-muted-foreground">
                未找到匹配结果
              </div>
            )}
            {searchResults.map((hit) => (
              <button
                key={hit.path}
                type="button"
                onClick={() => handleSearchHit(hit)}
                className={cn(
                  "mb-1 block w-full rounded-lg px-2.5 py-2 text-left transition-colors",
                  activePath === hit.path
                    ? "bg-sidebar-primary/10 text-foreground"
                    : "hover:bg-muted"
                )}
              >
                <div className="flex items-start gap-1.5">
                  <FileText className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[12px] font-[700]">{hit.title}</div>
                    <div className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground">
                      {hit.snippet || "—"}
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[10px] text-muted-soft">
                      {hit.path}
                    </div>
                  </div>
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="flex-1 overflow-auto py-2">
            {treeLoading && tree.length === 0 && (
              <div className="flex items-center justify-center py-10 text-xs text-muted-foreground">
                <Loader2 className="mr-2 size-3.5 animate-spin" />
                加载目录…
              </div>
            )}
            {tree.map((section) => {
              const isOpen = expanded[section.id] !== false;
              return (
                <div key={section.id} className="mb-1">
                  <button
                    type="button"
                    onClick={() => toggleSection(section.id)}
                    className="flex w-full items-center gap-1.5 px-3 py-1.5 text-[11px] font-[800] uppercase tracking-wider text-muted-soft transition-colors hover:text-foreground"
                  >
                    {isOpen ? (
                      <ChevronDown className="size-3" />
                    ) : (
                      <ChevronRight className="size-3" />
                    )}
                    <FolderOpen className="size-3 opacity-70" />
                    {section.label}
                    <span className="ml-auto text-[10px] font-normal text-muted-foreground">
                      {section.pages.length}
                    </span>
                  </button>
                  {isOpen && (
                    <div className="mt-0.5 space-y-0.5 pl-2 pr-1">
                      {section.pages.map((page) => (
                        <button
                          key={page.id}
                          type="button"
                          onClick={() => loadPage(page.path)}
                          className={cn(
                            "flex w-full items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-left text-[12.5px] transition-colors",
                            activePath === page.path
                              ? "bg-sidebar-primary/10 font-[700] text-sidebar-primary"
                              : "text-foreground/80 hover:bg-muted hover:text-foreground"
                          )}
                        >
                          <FileText className="size-3 shrink-0 opacity-60" />
                          <span className="truncate">{page.title}</span>
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
            {!treeLoading && tree.length === 0 && (
              <div className="px-4 py-10 text-center text-xs text-muted-foreground">
                暂无文档
              </div>
            )}
          </div>
        )}
      </aside>

      {/* Content */}
      <div ref={contentRef} className="flex-1 overflow-auto bg-background">
        {pageLoading && !pageContent && (
          <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            加载文档中…
          </div>
        )}
        {!pageLoading && !pageContent && (
          <div className="grid min-h-[400px] place-items-center text-sm text-muted-foreground">
            <div className="text-center">
              <BookOpen className="mx-auto mb-3 size-10 opacity-20" />
              <p>选择左侧文档开始阅读</p>
            </div>
          </div>
        )}
        {pageContent && (
          <article className="docs-content mx-auto max-w-[860px] px-8 py-8">
            <div className="mb-5 flex items-center gap-2 text-[11px] text-muted-foreground">
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono">
                {pageContent.path}
              </span>
              <span>·</span>
              <span>{lang === "zh" ? "中文" : "English"}</span>
            </div>
            <div className={cn(pageLoading && "opacity-50")}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  a: ({ href, children, ...props }) => {
                    if (href && (href.startsWith("http://") || href.startsWith("https://"))) {
                      return (
                        <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
                          {children}
                        </a>
                      );
                    }
                    return (
                      <a href={href} {...props}>
                        {children}
                      </a>
                    );
                  },
                  img: ({ src, alt, ...props }) => (
                    <img
                      src={resolveAsset(src)}
                      alt={alt}
                      className="my-4 max-w-full rounded-lg border border-border shadow-sm"
                      loading="lazy"
                      {...props}
                    />
                  ),
                  code: ({ className, children, ...props }) => {
                    const isInline = !className;
                    if (isInline) {
                      return <code className="docs-inline-code" {...props}>{children}</code>;
                    }
                    return (
                      <code className={cn("docs-code-block", className)} {...props}>
                        {children}
                      </code>
                    );
                  },
                  pre: ({ children, ...props }) => (
                    <pre className="docs-pre" {...props}>
                      {children}
                    </pre>
                  ),
                  table: ({ children, ...props }) => (
                    <div className="my-4 overflow-x-auto rounded-lg border border-border">
                      <table className="docs-table" {...props}>
                        {children}
                      </table>
                    </div>
                  ),
                  blockquote: ({ children, ...props }) => (
                    <blockquote className="docs-blockquote" {...props}>
                      {children}
                    </blockquote>
                  ),
                  h1: ({ children, ...props }) => (
                    <h1 className="docs-h1" {...props}>
                      {children}
                    </h1>
                  ),
                  h2: ({ children, ...props }) => (
                    <h2 className="docs-h2" {...props}>
                      {children}
                    </h2>
                  ),
                  h3: ({ children, ...props }) => (
                    <h3 className="docs-h3" {...props}>
                      {children}
                    </h3>
                  ),
                  h4: ({ children, ...props }) => (
                    <h4 className="docs-h4" {...props}>
                      {children}
                    </h4>
                  ),
                  p: ({ children, ...props }) => (
                    <p className="docs-p" {...props}>
                      {children}
                    </p>
                  ),
                  ul: ({ children, ...props }) => (
                    <ul className="docs-ul" {...props}>
                      {children}
                    </ul>
                  ),
                  ol: ({ children, ...props }) => (
                    <ol className="docs-ol" {...props}>
                      {children}
                    </ol>
                  ),
                  li: ({ children, ...props }) => (
                    <li className="docs-li" {...props}>
                      {children}
                    </li>
                  ),
                  hr: ({ ...props }) => <hr className="docs-hr" {...props} />,
                }}
              >
                {pageContent.content}
              </ReactMarkdown>
            </div>
          </article>
        )}
      </div>
    </div>
  );
}
