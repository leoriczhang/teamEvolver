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
