import { useMemo } from "react";
import { Empty } from "@/components/common";
import { cn } from "@/lib/utils";

type DiffLine = {
  type: "equal" | "add" | "del";
  text: string;
  oldNumber?: number;
  newNumber?: number;
};

// Line-level LCS diff so the editor can show git-style colored changes
// between the stored file (originalContent) and the working draft (content).
function computeLineDiff(original: string, next: string): DiffLine[] {
  const a = original.length ? original.split("\n") : [];
  const b = next.length ? next.split("\n") : [];
  const rows = a.length;
  const cols = b.length;
  const lcs: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  );
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      lcs[i][j] =
        a[i] === b[j]
          ? lcs[i + 1][j + 1] + 1
          : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
    }
  }
  const diff: DiffLine[] = [];
  let i = 0;
  let j = 0;
  let oldNumber = 1;
  let newNumber = 1;
  while (i < rows && j < cols) {
    if (a[i] === b[j]) {
      diff.push({ type: "equal", text: a[i], oldNumber: oldNumber++, newNumber: newNumber++ });
      i += 1;
      j += 1;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      diff.push({ type: "del", text: a[i], oldNumber: oldNumber++ });
      i += 1;
    } else {
      diff.push({ type: "add", text: b[j], newNumber: newNumber++ });
      j += 1;
    }
  }
  while (i < rows) diff.push({ type: "del", text: a[i++], oldNumber: oldNumber++ });
  while (j < cols) diff.push({ type: "add", text: b[j++], newNumber: newNumber++ });
  return diff;
}

export function diffStats(original: string, next: string) {
  const lines = computeLineDiff(original, next);
  return {
    added: lines.filter((line) => line.type === "add").length,
    removed: lines.filter((line) => line.type === "del").length,
  };
}

export function DiffView({
  original,
  next,
  compact = false,
}: {
  original: string;
  next: string;
  compact?: boolean;
}) {
  const lines = useMemo(() => computeLineDiff(original, next), [original, next]);
  const added = lines.filter((line) => line.type === "add").length;
  const removed = lines.filter((line) => line.type === "del").length;
  if (!added && !removed) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <Empty>草稿与已保存版本一致，暂无改动。</Empty>
      </div>
    );
  }
  return (
    <div className="flex h-full min-h-0 flex-col">
      {!compact && (
        <div className="flex shrink-0 items-center gap-3 border-b border-border bg-surface-subtle px-4 py-2 text-[11px] font-semibold">
          <span className="text-[#1a7f37]">+{added} 新增</span>
          <span className="text-[#cf222e]">-{removed} 删除</span>
          <span className="text-muted-foreground">与已保存版本对比</span>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-auto font-mono text-[11px] leading-5">
        {lines.map((line, index) => (
          <div
            key={index}
            className={cn(
              "flex border-l-2 border-transparent whitespace-pre-wrap break-words",
              line.type === "add" && "border-[#2da44e] bg-[#e6ffec]",
              line.type === "del" && "border-[#cf222e] bg-[#ffebe9]",
            )}
          >
            <span className="w-10 shrink-0 select-none border-r border-border/60 px-1 text-right text-muted-soft">
              {line.oldNumber ?? ""}
            </span>
            <span className="w-10 shrink-0 select-none border-r border-border/60 px-1 text-right text-muted-soft">
              {line.newNumber ?? ""}
            </span>
            <span
              className={cn(
                "w-4 shrink-0 select-none text-center",
                line.type === "add" && "font-semibold text-[#1a7f37]",
                line.type === "del" && "font-semibold text-[#cf222e]",
                line.type === "equal" && "text-muted-soft",
              )}
            >
              {line.type === "add" ? "+" : line.type === "del" ? "-" : ""}
            </span>
            <span className="flex-1 px-2">{line.text || "\u00a0"}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

