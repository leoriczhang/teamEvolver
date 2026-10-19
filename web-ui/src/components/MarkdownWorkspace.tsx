import { cn } from "@/lib/utils";
import { useEffect,useRef } from "react";
import ReactMarkdown,{ defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

interface MarkdownNode {
  type?: string;
  value?: string;
  alt?: string;
  depth?: number;
  children?: MarkdownNode[];
  data?: {
    hProperties?: Record<string, unknown>;
  };
}

function markdownNodeText(node: MarkdownNode): string {
  if (typeof node.value === "string") return node.value;
  if (typeof node.alt === "string") return node.alt;
  return (node.children || []).map(markdownNodeText).join("");
}

export function markdownHeadingId(value: string): string {
  return value
    .normalize("NFKC")
    .trim()
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/[\s_-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "section";
}

function remarkHeadingIds() {
  return (tree: MarkdownNode) => {
    const counts = new Map<string, number>();
    function visit(node: MarkdownNode) {
      if (node.type === "heading") {
        const base = markdownHeadingId(markdownNodeText(node));
        const count = counts.get(base) || 0;
        counts.set(base, count + 1);
        node.data ||= {};
        node.data.hProperties = { ...node.data.hProperties, id: count ? `${base}-${count}` : base };
      }
      node.children?.forEach(visit);
    }
    visit(tree);
  };
}

function markdownUrlTransform(url: string): string {
  if (/^viking:\/\//i.test(url)) return url;
  return defaultUrlTransform(url);
}

function scrollToMarkdownAnchor(container: HTMLElement, href: string): boolean {
  const raw = href.startsWith("#") ? href.slice(1) : href;
  if (!raw) return false;
  let fragment = raw;
  try {
    fragment = decodeURIComponent(raw);
  } catch {
    // A malformed escape should not make the document preview unusable.
  }
  const normalized = markdownHeadingId(fragment);
  const target = Array.from(container.querySelectorAll<HTMLElement>("[id]")).find(
    (element) => element.id === fragment || element.id === normalized,
  );
  target?.scrollIntoView({ block: "start", behavior: "smooth" });
  return Boolean(target);
}

export function MarkdownDocument({
  content,
  className,
  onLinkClick,
  anchor,
  onAnchorHandled,
}: {
  content: string;
  className?: string;
  onLinkClick?: (href: string) => boolean;
  anchor?: string;
  onAnchorHandled?: () => void;
}) {
  const articleRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!anchor || !articleRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      if (articleRef.current && scrollToMarkdownAnchor(articleRef.current, anchor)) onAnchorHandled?.();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [anchor, content, onAnchorHandled]);

  return (
    <article ref={articleRef} className={cn("markdown-document", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkHeadingIds]}
        urlTransform={markdownUrlTransform}
        components={{
          a: ({ href, children }) => (
            <a
              href={href}
              onClick={(event) => {
                if (!href) return;
                if (onLinkClick?.(href)) {
                  event.preventDefault();
                  return;
                }
                if (href.startsWith("#") && articleRef.current) {
                  event.preventDefault();
                  scrollToMarkdownAnchor(articleRef.current, href);
                }
              }}
            >
              {children}
            </a>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </article>
  );
}
