import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Bold,
  Braces,
  Code2,
  Eye,
  Heading1,
  Heading2,
  Italic,
  Link2,
  List,
  ListOrdered,
  Pilcrow,
  Quote,
  Save,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

type EditorMode = "write" | "read";

interface OutlineItem {
  level: number;
  text: string;
  offset: number;
  index: number;
}

interface MarkdownWorkspaceProps {
  value: string;
  onChange: (value: string) => void;
  onSave: () => void;
  saving?: boolean;
  dirty?: boolean;
  fileName?: string;
}

export function MarkdownDocument({ content, className }: { content: string; className?: string }) {
  return (
    <article className={cn("markdown-document", className)}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </article>
  );
}

export default function MarkdownWorkspace({
  value,
  onChange,
  onSave,
  saving = false,
  dirty = false,
  fileName = "Markdown",
}: MarkdownWorkspaceProps) {
  const [mode, setMode] = useState<EditorMode>("write");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const previewRef = useRef<HTMLDivElement>(null);

  const outline = useMemo<OutlineItem[]>(() => {
    const items: OutlineItem[] = [];
    let offset = 0;
    value.split("\n").forEach((line) => {
      const match = /^(#{1,4})\s+(.+?)\s*$/.exec(line);
      if (match) {
        items.push({
          level: match[1].length,
          text: match[2].replace(/[*_`~]/g, "").trim(),
          offset,
          index: items.length,
        });
      }
      offset += line.length + 1;
    });
    return items;
  }, [value]);

  useEffect(() => {
    function saveShortcut(event: KeyboardEvent) {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== "s") return;
      event.preventDefault();
      if (dirty && !saving) onSave();
    }
    window.addEventListener("keydown", saveShortcut);
    return () => window.removeEventListener("keydown", saveShortcut);
  }, [dirty, onSave, saving]);

  function replaceSelection(before: string, after = before, placeholder = "文本") {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const selected = value.slice(start, end) || placeholder;
    const next = `${value.slice(0, start)}${before}${selected}${after}${value.slice(end)}`;
    onChange(next);
    requestAnimationFrame(() => {
      textarea.focus();
      const nextStart = start + before.length;
      textarea.setSelectionRange(nextStart, nextStart + selected.length);
    });
  }

  function prefixLines(prefix: string, placeholder = "内容") {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const selectionStart = textarea.selectionStart;
    const selectionEnd = textarea.selectionEnd;
    const lineStart = value.lastIndexOf("\n", Math.max(0, selectionStart - 1)) + 1;
    const selected = value.slice(lineStart, selectionEnd) || placeholder;
    const replaced = selected
      .split("\n")
      .map((line) => `${prefix}${line}`)
      .join("\n");
    const next = `${value.slice(0, lineStart)}${replaced}${value.slice(selectionEnd)}`;
    onChange(next);
    requestAnimationFrame(() => {
      textarea.focus();
      textarea.setSelectionRange(lineStart + prefix.length, lineStart + replaced.length);
    });
  }

  function insertLink() {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const label = value.slice(start, end) || "链接文字";
    const markdown = `[${label}](https://)`;
    onChange(`${value.slice(0, start)}${markdown}${value.slice(end)}`);
    requestAnimationFrame(() => {
      textarea.focus();
      const urlStart = start + label.length + 3;
      textarea.setSelectionRange(urlStart, urlStart + 8);
    });
  }

  function jumpToHeading(item: OutlineItem) {
    if (mode === "write") {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus();
      textarea.setSelectionRange(item.offset, item.offset);
      return;
    }
    const headings = previewRef.current?.querySelectorAll("h1, h2, h3, h4");
    headings?.[item.index]?.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function handleEditorKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Tab") return;
    event.preventDefault();
    const textarea = event.currentTarget;
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const next = `${value.slice(0, start)}  ${value.slice(end)}`;
    onChange(next);
    requestAnimationFrame(() => textarea.setSelectionRange(start + 2, start + 2));
  }

  const lineCount = value ? value.split("\n").length : 0;

  return (
    <div className="markdown-workspace">
      <div className="markdown-toolbar">
        <div className="flex min-w-0 items-center gap-1">
          <span className="mr-2 hidden max-w-[180px] truncate text-xs font-semibold text-foreground sm:block">
            {fileName}
          </span>
          <ToolbarButton label="一级标题" onClick={() => prefixLines("# ")}><Heading1 /></ToolbarButton>
          <ToolbarButton label="二级标题" onClick={() => prefixLines("## ")}><Heading2 /></ToolbarButton>
          <ToolbarDivider />
          <ToolbarButton label="加粗" onClick={() => replaceSelection("**")}><Bold /></ToolbarButton>
          <ToolbarButton label="斜体" onClick={() => replaceSelection("*")}><Italic /></ToolbarButton>
          <ToolbarButton label="行内代码" onClick={() => replaceSelection("`")}><Code2 /></ToolbarButton>
          <ToolbarDivider />
          <ToolbarButton label="无序列表" onClick={() => prefixLines("- ")}><List /></ToolbarButton>
          <ToolbarButton label="有序列表" onClick={() => prefixLines("1. ")}><ListOrdered /></ToolbarButton>
          <ToolbarButton label="引用" onClick={() => prefixLines("> ")}><Quote /></ToolbarButton>
          <ToolbarButton label="代码块" onClick={() => replaceSelection("```\n", "\n```", "代码")}><Braces /></ToolbarButton>
          <ToolbarButton label="链接" onClick={insertLink}><Link2 /></ToolbarButton>
        </div>

        <div className="ml-auto flex shrink-0 items-center gap-1.5">
          <div className="markdown-mode-switch" role="tablist" aria-label="Markdown 编辑模式">
            <button type="button" role="tab" aria-selected={mode === "write"} onClick={() => setMode("write")}>
              <Pilcrow /> 编辑
            </button>
            <button type="button" role="tab" aria-selected={mode === "read"} onClick={() => setMode("read")}>
              <Eye /> 阅读
            </button>
          </div>
          <Button size="sm" disabled={!dirty || saving} onClick={onSave} title="保存（⌘/Ctrl + S）">
            <Save className="size-3.5" /> {saving ? "保存中…" : "保存"}
          </Button>
        </div>
      </div>

      <div className="markdown-workspace-body">
        <aside className="markdown-outline" aria-label="文档大纲">
          <div className="mb-2 px-2 text-[10px] font-bold uppercase tracking-[0.1em] text-muted-soft">文档大纲</div>
          {outline.length ? (
            <div className="space-y-0.5">
              {outline.map((item) => (
                <button
                  key={`${item.offset}-${item.text}`}
                  type="button"
                  className="block w-full truncate rounded-md py-1.5 pr-2 text-left text-[11.5px] text-muted-foreground hover:bg-muted hover:text-foreground"
                  style={{ paddingLeft: `${8 + (item.level - 1) * 12}px` }}
                  title={item.text}
                  onClick={() => jumpToHeading(item)}
                >
                  {item.text}
                </button>
              ))}
            </div>
          ) : (
            <p className="px-2 text-[11px] leading-relaxed text-muted-soft">添加 Markdown 标题后会自动生成大纲。</p>
          )}
        </aside>

        <main className="markdown-paper-shell">
          {mode === "write" ? (
            <textarea
              ref={textareaRef}
              autoFocus
              spellCheck={false}
              value={value}
              aria-label="Markdown 正文编辑器"
              onChange={(event) => onChange(event.target.value)}
              onKeyDown={handleEditorKeyDown}
              className="markdown-source-editor"
            />
          ) : (
            <div ref={previewRef} className="markdown-reading-pane">
              <MarkdownDocument content={value} />
            </div>
          )}
        </main>
      </div>

      <div className="markdown-statusbar">
        <span>{dirty ? "有未保存修改" : "所有修改已保存"}</span>
        <span className="ml-auto">{lineCount} 行 · {value.length.toLocaleString()} 字符 · Markdown</span>
      </div>
    </div>
  );
}

function ToolbarButton({ label, onClick, children }: { label: string; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" className="markdown-tool-button" aria-label={label} title={label} onClick={onClick}>
      {children}
    </button>
  );
}

function ToolbarDivider() {
  return <span className="mx-1 h-4 w-px bg-border" aria-hidden="true" />;
}
