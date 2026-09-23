import { useState } from "react";
import { api, datasetPath, jsonBody, type SessionDataset } from "@/api/datasets";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import SessionFilters from "@/components/session/SessionFilters";
import SessionTable from "@/components/session/SessionTable";
import { useSessionList, type SessionListFilters } from "@/hooks/useSessionList";
import { toastErr, toastOk } from "@/lib/toast";

export interface DatasetSelection {
  session_ids?: string[];
  filters?: SessionListFilters;
}

export default function DatasetSelectionDialog({ onClose, onSaved, selection, selectionCount, appendTo }: {
  onClose: () => void;
  onSaved: (dataset: SessionDataset) => void;
  selection?: DatasetSelection;
  selectionCount?: number;
  appendTo?: SessionDataset;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [filters, setFilters] = useState<SessionListFilters>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = useState<"selected" | "filtered">("selected");
  const [saving, setSaving] = useState(false);
  const sessions = useSessionList({ filters, pageSize: 10, enabled: !selection });
  const count = selection ? selectionCount || 0 : mode === "selected" ? selected.length : sessions.total;

  async function save() {
    if (saving) return;
    setSaving(true);
    try {
      const scope = selection || (mode === "selected" ? { session_ids: selected } : { filters });
      const saved = await api<SessionDataset>(appendTo ? `${datasetPath(appendTo.dataset_id)}/items` : "/api/datasets",
        jsonBody(appendTo ? scope : { name: name.trim(), description, ...scope }));
      toastOk(appendTo ? "已添加到数据集" : "数据集已创建", `${saved.name} · ${saved.item_count} 条`);
      onSaved(saved);
    } catch (error: any) {
      toastErr(appendTo ? "添加失败" : "创建失败", error.message);
    } finally { setSaving(false); }
  }

  return <Dialog open onOpenChange={open => !open && !saving && onClose()}>
    <DialogContent className={selection ? "sm:max-w-[520px]" : "sm:max-w-[1100px] max-h-[90vh] overflow-y-auto"}>
      <DialogTitle>{appendTo ? `添加 Session · ${appendTo.name}` : "从会话创建数据集"}</DialogTitle>
      <DialogDescription>保存当前 Session 快照，供导出与重复重回放。每个数据集最多 500 条、64 MiB。</DialogDescription>
      {!appendTo && <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5"><span>数据集名称 *</span><Input autoFocus aria-label="数据集名称" maxLength={120} value={name} onChange={e => setName(e.target.value)} placeholder="例如：客服失败案例回归集" /></label>
        <label className="space-y-1.5"><span>描述</span><Textarea aria-label="数据集描述" maxLength={2000} value={description} onChange={e => setDescription(e.target.value)} placeholder="记录用途或选择范围" /></label>
      </div>}
      {!selection && <>
        <SessionFilters value={filters} onApply={next => { setFilters(next); setSelected([]); sessions.setPage(1); }} skillOptions={sessions.skillCounts} />
        {sessions.error && <p role="alert" className="text-destructive">{sessions.error}</p>}
        <SessionTable rows={sessions.rows} emptyText={sessions.loading ? "加载会话中…" : "没有匹配的会话"} maxHeight="330px"
          selectedIds={selected}
          onToggleSelect={(sid, checked) => setSelected(ids => checked ? [...new Set([...ids, sid])] : ids.filter(id => id !== sid))}
          onSelectAll={checked => setSelected(ids => checked ? [...new Set([...ids, ...(sessions.rows || []).map(row => row.session_id)])] : ids.filter(id => !sessions.rows?.some(row => row.session_id === id)))} />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex gap-4">
            <label><input type="radio" name="dataset-selection" checked={mode === "selected"} onChange={() => setMode("selected")} /> 已勾选 {selected.length} 条</label>
            <label><input type="radio" name="dataset-selection" checked={mode === "filtered"} onChange={() => setMode("filtered")} /> 全部筛选结果 {sessions.total} 条</label>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={sessions.page <= 1 || sessions.loading} onClick={() => sessions.setPage(sessions.page - 1)}>上一页</Button>
            <span>{sessions.page} / {Math.max(1, Math.ceil(sessions.total / 10))}</span>
            <Button variant="outline" size="sm" disabled={sessions.page * 10 >= sessions.total || sessions.loading} onClick={() => sessions.setPage(sessions.page + 1)}>下一页</Button>
          </div>
        </div>
      </>}
      <div className="flex items-center justify-between gap-3 border-t pt-4">
        <span className={count > 500 ? "text-destructive" : "text-muted-foreground"}>将保存 {count} 条{count > 500 && "，请缩小范围"}</span>
        <div className="flex gap-2"><Button variant="outline" disabled={saving} onClick={onClose}>取消</Button>
          <Button disabled={saving || (!appendTo && !name.trim()) || !count || count > 500 || (!selection && sessions.loading)} onClick={save}>
            {saving ? "保存中…" : appendTo ? "添加到数据集" : "创建数据集"}
          </Button></div>
      </div>
    </DialogContent>
  </Dialog>;
}
