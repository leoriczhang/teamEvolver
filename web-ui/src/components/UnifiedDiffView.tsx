import { useMemo } from "react";
import { cn } from "@/lib/utils";

type DiffKind = "meta" | "hunk" | "add" | "del" | "context";

type DiffLine = {
  kind: DiffKind;
  text: string;
  prefix: string;
  oldNumber?: number;
  newNumber?: number;
};

const HUNK_RE = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

function parseUnifiedDiff(diff: string): DiffLine[] {
  const rows = diff.replace(/\r\n/g, "\n").split("\n");
  const parsed: DiffLine[] = [];
  let oldNumber = 0;
  let newNumber = 0;

  for (const row of rows) {
    const hunk = row.match(HUNK_RE);
    if (hunk) {
      oldNumber = Number(hunk[1]);
      newNumber = Number(hunk[2]);
      parsed.push({ kind: "hunk", text: row, prefix: "" });
      continue;
    }

    if (row.startsWith("diff ") || row.startsWith("index ") || row.startsWith("---") || row.startsWith("+++")) {
      parsed.push({ kind: "meta", text: row, prefix: "" });
      continue;
    }

    if (row.startsWith("+")) {
      parsed.push({ kind: "add", text: row.slice(1) || " ", prefix: "+", newNumber: newNumber++ });
      continue;
    }

    if (row.startsWith("-")) {
      parsed.push({ kind: "del", text: row.slice(1) || " ", prefix: "-", oldNumber: oldNumber++ });
      continue;
    }

    if (row.startsWith(" ")) {
      parsed.push({
        kind: "context",
        text: row.slice(1) || " ",
        prefix: "",
        oldNumber: oldNumber++,
        newNumber: newNumber++,
      });
      continue;
    }

    parsed.push({ kind: "meta", text: row || " ", prefix: "" });
  }

  return parsed;
}

export default function UnifiedDiffView({
  diff,
  className,
}: {
  diff: string;
  className?: string;
}) {
  const lines = parseUnifiedDiff(diff);

  return (
    <div
      className={cn(
        "overflow-auto rounded-md border border-border bg-background font-mono text-[11px] leading-5",
        className,
      )}
    >
      {lines.map((line, index) => (
        <div
          key={index}
          className={cn(
            "grid min-w-max grid-cols-[3rem_3rem_1.5rem_minmax(28rem,1fr)] border-l-2 border-transparent",
            line.kind === "add" && "border-[#2da44e] bg-[#e6ffec]",
            line.kind === "del" && "border-[#cf222e] bg-[#ffebe9]",
            line.kind === "hunk" && "bg-[#ddf4ff] text-[#0969da]",
            line.kind === "meta" && "bg-[#f6f8fa] text-muted-foreground",
          )}
        >
          <span className="select-none border-r border-border/70 px-2 text-right text-muted-soft">
            {line.oldNumber ?? ""}
          </span>
          <span className="select-none border-r border-border/70 px-2 text-right text-muted-soft">
            {line.newNumber ?? ""}
          </span>
          <span
            className={cn(
              "select-none px-1 text-center font-semibold",
              line.kind === "add" && "text-[#1a7f37]",
              line.kind === "del" && "text-[#cf222e]",
            )}
          >
            {line.prefix}
          </span>
          <span className="whitespace-pre-wrap break-words px-2">{line.text}</span>
        </div>
      ))}
    </div>
  );
}

// ---- Full-text inline diff (candidate SKILL.md with change highlights) ---- //

export type InlineDiffRow = {
  kind: "context" | "add" | "del";
  text: string;
  oldNumber?: number;
  newNumber?: number;
};

/**
 * Line-level LCS diff. Unlike a unified diff this keeps the FULL candidate
 * text in order; lines removed from the current version are emitted inline
 * (in red) right where they were replaced, so the reader sees one continuous
 * document with only the changed lines colored.
 */
export function diffLines(oldText: string, newText: string): InlineDiffRow[] {
  const a = oldText.replace(/\r\n/g, "\n").split("\n");
  const b = newText.replace(/\r\n/g, "\n").split("\n");
  const n = a.length;
  const m = b.length;
  // dp[i][j] = LCS length of a[i..] vs b[j..]
  const dp: Int32Array[] = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const rows: InlineDiffRow[] = [];
  let i = 0;
  let j = 0;
  let oldNum = 1;
  let newNum = 1;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({ kind: "context", text: b[j], oldNumber: oldNum++, newNumber: newNum++ });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({ kind: "del", text: a[i], oldNumber: oldNum++ });
      i++;
    } else {
      rows.push({ kind: "add", text: b[j], newNumber: newNum++ });
      j++;
    }
  }
  while (i < n) rows.push({ kind: "del", text: a[i++], oldNumber: oldNum++ });
  while (j < m) rows.push({ kind: "add", text: b[j++], newNumber: newNum++ });
  return rows;
}

/**
 * Renders the full candidate document; lines that differ from `current` are
 * highlighted with diff colors (green = added, red = removed). When there is
 * no current version (new skill) every line is treated as added.
 */
export function InlineDiffView({
  current,
  candidate,
  className,
}: {
  current: string;
  candidate: string;
  className?: string;
}) {
  const rows = useMemo(
    () => (current ? diffLines(current, candidate) : null),
    [current, candidate],
  );
  const lines: InlineDiffRow[] =
    rows ?? candidate.split("\n").map((text, idx) => ({ kind: "add" as const, text, newNumber: idx + 1 }));

  return (
    <div
      className={cn(
        "overflow-auto rounded-md border border-border bg-background font-mono text-[11px] leading-5",
        className,
      )}
    >
      {lines.map((line, index) => (
        <div
          key={index}
          className={cn(
            "grid min-w-max grid-cols-[3rem_3rem_1.5rem_minmax(28rem,1fr)] border-l-2 border-transparent",
            line.kind === "add" && "border-[#2da44e] bg-[#e6ffec]",
            line.kind === "del" && "border-[#cf222e] bg-[#ffebe9]",
          )}
        >
          <span className="select-none border-r border-border/70 px-2 text-right text-muted-soft">
            {line.oldNumber ?? ""}
          </span>
          <span className="select-none border-r border-border/70 px-2 text-right text-muted-soft">
            {line.newNumber ?? ""}
          </span>
          <span
            className={cn(
              "select-none px-1 text-center font-semibold",
              line.kind === "add" && "text-[#1a7f37]",
              line.kind === "del" && "text-[#cf222e]",
            )}
          >
            {line.kind === "add" ? "+" : line.kind === "del" ? "-" : ""}
          </span>
          <span className="whitespace-pre-wrap break-words px-2">{line.text || " "}</span>
        </div>
      ))}
    </div>
  );
}
