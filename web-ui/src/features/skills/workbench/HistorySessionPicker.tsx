import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Dialog,DialogContent,DialogHeader,DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import type { LedgerRow,PageResponse } from "@/features/sessions/api/client";
import SessionSkillChips from "@/features/sessions/components/SessionSkillChips";
import { fmtTime } from "@/lib/format";
import { useEffect,useState } from "react";

export default function HistorySessionPicker({ skillName, busy, onGenerate }: {
  skillName: string; busy: boolean; onGenerate: (ids: string[]) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<PageResponse<LedgerRow> | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError("");
    const timer = setTimeout(() => {
      const query = new URLSearchParams({ skill: skillName, search, offset: String(offset), limit: "10" });
      void api<PageResponse<LedgerRow>>(`/api/sessions/page?${query}`, { signal: controller.signal })
        .then(value => { if (!controller.signal.aborted) setPage(value); })
        .catch(e => { if (!controller.signal.aborted) setError(e.message); })
        .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 200);
    return () => { controller.abort(); clearTimeout(timer); };
  }, [open, search, offset, skillName]);
  return <>
    <Button variant="outline" size="sm" disabled={busy || !skillName} onClick={() => {
      setSelected(new Set()); setSearch(""); setOffset(0); setOpen(true);
    }}>{busy ? "生成中…" : "从历史 Session 生成数据集"}</Button>
    <Dialog open={open} onOpenChange={setOpen}><DialogContent className="max-w-3xl">
      <DialogHeader><DialogTitle>选择 {skillName} 的历史 Session</DialogTitle></DialogHeader>
      <Input aria-label="搜索历史 Session" value={search} placeholder="搜索标题或 Session ID"
        onChange={e => { setSearch(e.target.value); setOffset(0); }} />
      <p className="text-xs text-muted-foreground">最多选择 30 个会话；版本来自会话实际快照。</p>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="max-h-[50vh] space-y-2 overflow-y-auto">
        {loading ? <p className="text-sm">正在读取历史 Session…</p> : page?.sessions?.map(row =>
          <div key={row.session_id} className="rounded-lg border border-border p-3">
            <label className="flex items-start gap-2 text-sm">
              <input type="checkbox" checked={selected.has(row.session_id)}
                disabled={!selected.has(row.session_id) && selected.size >= 30}
                onChange={e => setSelected(current => {
                  const next = new Set(current);
                  if (e.target.checked) next.add(row.session_id); else next.delete(row.session_id);
                  return next;
                })} />
              <span className="min-w-0 break-words">{row.title || row.session_id}</span>
            </label>
            <p className="mt-1 break-all text-[11px] text-muted-foreground">{row.session_id}</p>
            <p className="my-1 text-[11px] text-muted-foreground">
              业务时间 {fmtTime(row.timestamp)} · 入库时间 {fmtTime(row.ingested_at)}
            </p>
            <SessionSkillChips row={row} />
          </div>)}
        {!loading && page && !page.sessions?.length && <p className="text-sm">没有匹配的历史 Session。</p>}
      </div>
      <div className="flex items-center gap-2">
        <Button variant="outline" size="sm" disabled={loading || offset === 0}
          onClick={() => setOffset(n => Math.max(0, n - 10))}>上一页</Button>
        <Button variant="outline" size="sm" disabled={loading || !page?.has_more}
          onClick={() => setOffset(n => n + 10)}>下一页</Button>
        <Button className="ml-auto" disabled={busy || !selected.size}
          onClick={async () => { await onGenerate([...selected]); setOpen(false); }}>
          {busy ? "生成中…" : `生成数据集（${selected.size}）`}
        </Button>
      </div>
    </DialogContent></Dialog>
  </>;
}
