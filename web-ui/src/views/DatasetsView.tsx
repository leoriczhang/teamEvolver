import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, Download, Eye, FolderOpen, Pencil, Plus, RefreshCw, Repeat2, Search, Square, Trash2 } from "lucide-react";
import {
  api, datasetPath, downloadDataset, isActiveRun, jsonBody,
  type DatasetDetail, type DatasetItem, type DatasetRun, type SessionDataset,
} from "@/api/datasets";
import { Empty, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { fmtTime } from "@/lib/format";
import { toastErr, toastOk } from "@/lib/toast";
import DatasetSelectionDialog from "./datasets/DatasetSelectionDialog";

const SIZE = 20;
const th = "border-b border-line bg-muted/40 px-4 py-3 text-left text-xs font-semibold text-muted-foreground whitespace-nowrap";
const td = "border-b border-line px-4 py-3 align-top text-sm";

function Pager({ page, total, onChange }: { page: number; total: number; onChange: (page: number) => void }) {
  return <div className="flex items-center justify-end gap-3 px-4 py-3 text-xs text-muted-foreground">
    <span>共 {total} 条 · {page} / {Math.max(1, Math.ceil(total / SIZE))} 页</span>
    <Button size="sm" variant="outline" disabled={page <= 1} onClick={() => onChange(page - 1)}>上一页</Button>
    <Button size="sm" variant="outline" disabled={page * SIZE >= total} onClick={() => onChange(page + 1)}>下一页</Button>
  </div>;
}
function Status({ status }: { status: string }) {
  const labels: Record<string, string> = {
    queued: "排队中", running: "运行中", cancelling: "停止中", cancelled: "已停止", interrupted: "已中断",
    completed: "完成", failed: "执行失败", skipped: "未执行",
  };
  return <Pill tone={status === "completed" ? "green" : status === "failed" ? "red" : isActiveRun(status) ? "blue" : "gray"}>{labels[status] || status}</Pill>;
}
function Box({ children }: { children: ReactNode }) {
  return <section className="overflow-hidden rounded-xl border border-border bg-surface">{children}</section>;
}
function SearchBox({ value, onChange, placeholder }: { value: string; onChange: (text: string) => void; placeholder: string }) {
  return <div className="relative w-full max-w-[480px]"><Search className="absolute left-3 top-2.5 size-4 text-muted-foreground" />
    <Input className="pl-9" aria-label={placeholder} value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder} /></div>;
}
function useDebounced(value: string) {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setDebounced(value), 250); return () => clearTimeout(timer); }, [value]);
  return debounced;
}

export default function DatasetsView({ active }: { active: boolean }) {
  const [datasetId, setDatasetId] = useState(() => new URLSearchParams(window.location.search).get("dataset") || "");
  const [datasets, setDatasets] = useState<SessionDataset[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  const [tick, setTick] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<SessionDataset | null>(null);
  const [busy, setBusy] = useState("");

  useEffect(() => {
    if (!active) return;
    const url = new URL(window.location.href);
    url.searchParams.set("view", "datasets");
    if (datasetId) url.searchParams.set("dataset", datasetId); else url.searchParams.delete("dataset");
    window.history.replaceState(null, "", url.toString());
  }, [active, datasetId]);
  useEffect(() => {
    if (!active || datasetId) return;
    const controller = new AbortController();
    setLoading(true);
    api<{ datasets: SessionDataset[]; total: number }>(`/api/datasets?search=${encodeURIComponent(debounced)}&limit=${SIZE}&offset=${(page - 1) * SIZE}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) { setDatasets(data.datasets); setTotal(data.total); setError(""); if (page > 1 && !data.datasets.length) setPage(page - 1); } })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, datasetId, debounced, page, tick]);

  async function remove(dataset: SessionDataset) {
    if (!window.confirm(`删除数据集「${dataset.name}」及其重回放记录？来源 Session 会保留。`)) return;
    setBusy(dataset.dataset_id);
    try { await api(datasetPath(dataset.dataset_id), { method: "DELETE" }); toastOk("数据集已删除"); setTick(t => t + 1); }
    catch (e: any) { toastErr("删除失败", e.message); }
    finally { setBusy(""); }
  }
  async function download(dataset: SessionDataset) {
    setBusy(dataset.dataset_id);
    try { await downloadDataset(dataset); toastOk("数据集 ZIP 已导出"); }
    catch (e: any) { toastErr("导出失败", e.message); }
    finally { setBusy(""); }
  }

  if (datasetId) return <DatasetContents key={datasetId} datasetId={datasetId} active={active} onBack={() => { setDatasetId(""); setTick(t => t + 1); }} />;
  return <div className="space-y-4 px-[22px] pb-8 pt-4">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <SearchBox value={search} onChange={value => { setSearch(value); setPage(1); }} placeholder="搜索数据集名称或描述" />
      <div className="flex gap-2"><Button variant="outline" onClick={() => setTick(t => t + 1)} disabled={loading}><RefreshCw className="size-4" />刷新</Button>
        <Button onClick={() => setCreating(true)}><Plus className="size-4" />从会话创建</Button></div>
    </div>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    <Box>
      <div className="overflow-auto"><table className="w-full min-w-[960px] border-collapse">
        <thead><tr>{["名称", "描述", "数据量", "创建时间", "操作"].map(text => <th className={th} key={text}>{text}</th>)}</tr></thead>
        <tbody>{datasets.map(dataset => <tr key={dataset.dataset_id} className="hover:bg-muted/20">
          <td className={td}><button className="flex items-center gap-2 font-semibold text-accent hover:underline" onClick={() => setDatasetId(dataset.dataset_id)}><FolderOpen className="size-4" />{dataset.name}</button></td>
          <td className={`${td} max-w-[400px] text-muted-foreground`}><p className="line-clamp-2">{dataset.description || "—"}</p></td>
          <td className={td}><Pill tone="blue">{dataset.item_count} 条</Pill></td>
          <td className={`${td} whitespace-nowrap text-muted-foreground`}>{fmtTime(dataset.created_at)}</td>
          <td className={td}><div className="flex gap-1">
            <Button variant="ghost" size="icon-sm" aria-label={`查看 ${dataset.name}`} onClick={() => setDatasetId(dataset.dataset_id)}><Eye className="size-4" /></Button>
            <Button variant="ghost" size="icon-sm" aria-label={`编辑 ${dataset.name}`} onClick={() => setEditing(dataset)}><Pencil className="size-4" /></Button>
            <Button variant="ghost" size="icon-sm" aria-label={`导出 ${dataset.name}`} disabled={busy === dataset.dataset_id} onClick={() => download(dataset)}><Download className="size-4" /></Button>
            <Button variant="ghost" size="icon-sm" className="text-destructive" aria-label={`删除 ${dataset.name}`} disabled={busy === dataset.dataset_id} onClick={() => remove(dataset)}><Trash2 className="size-4" /></Button>
          </div></td>
        </tr>)}</tbody>
      </table></div>
      {!datasets.length && <Empty>{loading ? "加载数据集中…" : error ? "数据集暂不可用，请重试" : search ? "没有匹配的数据集" : "暂无数据集。从会话中选取 Session，建立可重复验证的集合。"}</Empty>}
      <Pager page={page} total={total} onChange={setPage} />
    </Box>
    {creating && <DatasetSelectionDialog onClose={() => setCreating(false)} onSaved={dataset => { setCreating(false); setDatasetId(dataset.dataset_id); }} />}
    {editing && <MetadataDialog dataset={editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); setTick(t => t + 1); }} />}
  </div>;
}

function DatasetContents({ datasetId, active, onBack }: { datasetId: string; active: boolean; onBack: () => void }) {
  const path = datasetPath(datasetId);
  const [data, setData] = useState<DatasetDetail | null>(null);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState(false);
  const [running, setRunning] = useState(false);
  const [item, setItem] = useState<DatasetItem | null>(null);
  const [historicalItem, setHistoricalItem] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [busyItem, setBusyItem] = useState("");
  const [runs, setRuns] = useState<DatasetRun[]>([]);
  const [runListPage, setRunListPage] = useState(1);
  const [runTotal, setRunTotal] = useState(0);
  const [runId, setRunId] = useState("");
  const [run, setRun] = useState<DatasetRun | null>(null);
  const [runPage, setRunPage] = useState(1);
  const [runError, setRunError] = useState("");
  const [runTick, setRunTick] = useState(0);
  const [result, setResult] = useState<Record<string, any> | null>(null);
  const [stopping, setStopping] = useState(false);
  const hasActive = runs.some(r => isActiveRun(r.status)) || !!run && isActiveRun(run.status);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    api<DatasetDetail>(`${path}?search=${encodeURIComponent(debounced)}&limit=${SIZE}&offset=${(page - 1) * SIZE}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setData(value); setError(""); if (page > 1 && !value.items.length) setPage(page - 1); } })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); });
    return () => controller.abort();
  }, [active, path, page, debounced, tick]);

  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    api<{ runs: DatasetRun[]; total: number }>(`${path}/runs?limit=${SIZE}&offset=${(runListPage - 1) * SIZE}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) {
        setRuns(value.runs); setRunTotal(value.total); setRunError("");
        setRunId(current => current || value.runs[0]?.run_id || "");
      } })
      .catch(e => { if (!controller.signal.aborted) setRunError(e.message); });
    return () => controller.abort();
  }, [active, path, runListPage, runTick]);
  useEffect(() => {
    if (!active || !runId) return;
    const controller = new AbortController();
    api<DatasetRun>(`${path}/runs/${encodeURIComponent(runId)}?limit=${SIZE}&offset=${(runPage - 1) * SIZE}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) { setRun(value); setRunError(""); } })
      .catch(e => { if (!controller.signal.aborted) setRunError(e.message); });
    return () => controller.abort();
  }, [active, path, runId, runPage, runTick]);
  useEffect(() => {
    if (!active || !hasActive) return;
    const timer = setTimeout(() => setRunTick(t => t + 1), 3000);
    return () => clearTimeout(timer);
  }, [active, hasActive, runTick]);

  async function removeItem(row: DatasetItem) {
    if (!window.confirm("从数据集移除此条 Session？来源 Session 会保留。")) return;
    setBusyItem(row.item_id);
    try { await api(`${path}/items/${row.item_id}`, { method: "DELETE" }); setTick(t => t + 1); toastOk("条目已移除"); }
    catch (e: any) { toastErr("移除失败", e.message); }
    finally { setBusyItem(""); }
  }
  async function openItem(rowId: string, historical = false) {
    setBusyItem(rowId);
    try {
      const sourcePath = historical ? `${path}/runs/${encodeURIComponent(runId)}` : path;
      const detail = await api<DatasetItem>(`${sourcePath}/items/${encodeURIComponent(rowId)}`);
      setHistoricalItem(historical); setItem(detail);
    }
    catch (e: any) { toastErr("加载条目失败", e.message); }
    finally { setBusyItem(""); }
  }
  async function openResult(rowId: string) {
    setBusyItem(rowId);
    try { setResult(await api(`${path}/runs/${encodeURIComponent(runId)}/results/${encodeURIComponent(rowId)}`)); }
    catch (e: any) { toastErr("加载结果失败", e.message); }
    finally { setBusyItem(""); }
  }
  async function stop() {
    setStopping(true);
    try { await api(`${path}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" }); setRunTick(t => t + 1); toastOk("已请求停止", "当前执行中的条目结束后停止后续执行"); }
    catch (e: any) { toastErr("停止失败", e.message); }
    finally { setStopping(false); }
  }
  async function download() {
    if (!data) return;
    setDownloadBusy(true);
    try { await downloadDataset(data); toastOk("数据集 ZIP 已导出"); }
    catch (e: any) { toastErr("导出失败", e.message); }
    finally { setDownloadBusy(false); }
  }
  return <div className="space-y-4 px-[22px] pb-8 pt-4">
    <div className="flex items-center gap-2 text-sm text-muted-foreground"><button className="hover:text-accent" onClick={onBack}>数据集</button><span>/</span><span>{data?.name || "详情"}</span></div>
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 className="text-xl font-bold">{data?.name || "加载中…"}</h2><p className="mt-1 text-xs text-muted-foreground">创建于 {fmtTime(data?.created_at)} · {data?.item_count ?? "—"} 条 Session</p>{data?.description && <p className="mt-2 max-w-[640px] text-sm text-muted-foreground">{data.description}</p>}</div>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" onClick={onBack}><ArrowLeft className="size-4" />返回</Button>
        <Button variant="outline" disabled={!data || hasActive} onClick={() => setEditing(true)}><Pencil className="size-4" />编辑</Button>
        <Button variant="outline" disabled={!data || hasActive} onClick={() => setAdding(true)}><Plus className="size-4" />添加 Session</Button>
        <Button variant="outline" disabled={!data?.item_count || hasActive} onClick={() => setRunning(true)}><Repeat2 className="size-4" />批量重回放</Button>
        <Button disabled={!data || downloadBusy} onClick={download}><Download className="size-4" />{downloadBusy ? "导出中…" : "导出 ZIP"}</Button>
      </div>
    </div>
    {error && <p role="alert" className="text-destructive">{error}<Button variant="ghost" size="sm" onClick={() => setTick(t => t + 1)}>重试</Button></p>}
    <SearchBox value={search} onChange={value => { setSearch(value); setPage(1); }} placeholder="搜索 Trace ID、Session ID 或用户问题" />
    <Box><div className="overflow-auto"><table className="w-full min-w-[960px] border-collapse"><thead><tr>{["业务时间 / 拉取入库", "来源 Trace / Session", "用户问题", "Skill", "操作"].map(text => <th key={text} className={th}>{text}</th>)}</tr></thead>
      <tbody>{data?.items.map(row => <tr key={row.item_id}>
        <td className={`${td} whitespace-nowrap text-xs`}><div>{fmtTime(row.timestamp)}</div><div className="mt-1 text-muted-foreground">拉取 {fmtTime(row.ingested_at)}</div></td>
        <td className={td}><button className="block max-w-[180px] truncate font-mono text-xs text-accent" title={row.trace_id || row.session_id} onClick={() => openItem(row.item_id)}>{row.trace_id || row.session_id}</button><div className="mt-1 text-xs text-muted-foreground">{row.user_alias || "—"}</div></td>
        <td className={td}><button className="line-clamp-2 max-w-[560px] text-left hover:text-accent" title={row.query} onClick={() => openItem(row.item_id)}>{row.query || row.title || "缺少 Query，请编辑补齐"}</button><span className="mt-1 block text-xs text-muted-foreground">{row.requirements.length} 条 Checklist</span></td>
        <td className={td}><div className="flex max-w-[200px] flex-wrap gap-1">{row.used_skills.length ? row.used_skills.map(skill => <Pill key={skill} tone="blue">{skill}</Pill>) : "—"}</div></td>
        <td className={td}><div className="flex gap-1"><Button variant="ghost" size="icon-sm" disabled={busyItem === row.item_id} aria-label={`查看条目 ${row.session_id}`} onClick={() => openItem(row.item_id)}><Eye className="size-4" /></Button><Button variant="ghost" size="icon-sm" className="text-destructive" disabled={hasActive || busyItem === row.item_id} aria-label={`移除条目 ${row.session_id}`} onClick={() => removeItem(row)}><Trash2 className="size-4" /></Button></div></td>
      </tr>)}</tbody></table></div>
      {!data?.items.length && <Empty>{!data ? "加载条目中…" : search ? "没有匹配的条目" : "数据集为空，可添加 Session"}</Empty>}
      <Pager page={page} total={data?.total || 0} onChange={setPage} />
    </Box>
    <Box>
      <div className="flex items-center justify-between border-b border-line px-4 py-3"><h3 className="font-semibold">批量重回放验证</h3><Button variant="outline" size="sm" onClick={() => setRunTick(t => t + 1)}><RefreshCw className="size-3.5" />刷新</Button></div>
      {runError && <p role="alert" className="px-4 pt-3 text-sm text-destructive">{runError}</p>}
      {!runs.length && !run ? <Empty>暂无重回放记录。点击“批量重回放”开始验证。</Empty> : <>
        <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
          <select className="h-9 max-w-[380px] rounded-lg border bg-background px-2 text-xs" aria-label="选择重回放批次" value={runId} onChange={e => { setRunId(e.target.value); setRunPage(1); setRun(null); }}>
            {runId && !runs.some(r => r.run_id === runId) && <option value={runId}>{runId}</option>}
            {runs.map(r => <option key={r.run_id} value={r.run_id}>{fmtTime(r.created_at)} · {r.run_id.slice(-8)}</option>)}
          </select>
          {run && <><Status status={run.status} /><span className="text-muted-foreground">进度 {run.completed}/{run.total} · 成功 {run.succeeded} · 失败 {run.failed} · 未执行 {run.skipped}</span>
            {run.finished_at && <span className="text-xs text-muted-foreground">结束于 {fmtTime(run.finished_at)}</span>}
            {isActiveRun(run.status) && <Button size="sm" variant="outline" disabled={stopping || run.status === "cancelling"} onClick={stop}><Square className="size-3" />停止</Button>}
          </>}
        </div>
        {runTotal > SIZE && <Pager page={runListPage} total={runTotal} onChange={setRunListPage} />}
        {run?.error && <p className="px-4 pb-3 text-sm text-destructive">{run.error}</p>}
        {run && <><div className="mx-4 mb-3 h-1.5 overflow-hidden rounded bg-muted" role="progressbar" aria-label="重回放进度" aria-valuemin={0} aria-valuemax={run.total} aria-valuenow={run.completed}><div className="h-full bg-accent transition-all" style={{ width: `${100 * run.completed / Math.max(1, run.total)}%` }} /></div>
          <div className="overflow-auto"><table className="w-full min-w-[960px] border-collapse"><thead><tr>{["#", "用户问题", "执行状态", "是否成功", "重回放 Trace", "来源 Trace", "操作"].map(text => <th className={th} key={text}>{text}</th>)}</tr></thead>
            <tbody>{run.items?.map((row, index) => <tr key={row.item_id}>
              <td className={td}>{(runPage - 1) * SIZE + index + 1}</td><td className={td}><p className="line-clamp-2 max-w-[480px]" title={row.query}>{row.query}</p>{row.error && <p className="mt-1 max-w-[480px] text-xs text-destructive">{row.error}</p>}</td>
              <td className={td}><Status status={row.status} /></td><td className={td}>{row.success == null ? <span className="text-muted-foreground">未判定</span> : <Pill tone={row.success ? "green" : "red"}>{row.success ? "成功" : "未通过"}</Pill>}</td>
              <td className={td}>{row.replay_trace_id ? <button className="max-w-[160px] truncate font-mono text-xs text-accent" title={row.replay_trace_id} onClick={() => openResult(row.item_id)}>{row.replay_trace_id}</button> : "—"}</td>
              <td className={td}><button className="max-w-[160px] truncate font-mono text-xs text-accent" title={row.trace_id || row.session_id} onClick={() => openItem(row.item_id, true)}>{row.trace_id || row.session_id}</button></td>
              <td className={td}><Button variant="ghost" size="sm" disabled={busyItem === row.item_id || !["completed", "failed", "skipped"].includes(row.status)} onClick={() => openResult(row.item_id)}>结果</Button></td>
            </tr>)}</tbody></table></div><Pager page={runPage} total={run.total} onChange={setRunPage} /></>}
      </>}
    </Box>
    {adding && data && <DatasetSelectionDialog appendTo={data} onClose={() => setAdding(false)} onSaved={() => { setAdding(false); setTick(t => t + 1); }} />}
    {editing && data && <MetadataDialog dataset={data} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); setTick(t => t + 1); }} />}
    {item && <ItemDialog item={item} path={path} readOnly={hasActive || historicalItem} onClose={() => setItem(null)} onSaved={() => { setItem(null); setTick(t => t + 1); }} />}
    {running && <RunDialog path={path} count={data?.item_count || 0} onClose={() => setRunning(false)} onStarted={value => { setRunning(false); setRunId(value.run_id); setRunPage(1); setRunListPage(1); setRun(value); setRunTick(t => t + 1); }} />}
    {result && <ResultDialog result={result} onClose={() => setResult(null)} />}
  </div>;
}

function MetadataDialog({ dataset, onClose, onSaved }: { dataset: SessionDataset; onClose: () => void; onSaved: () => void }) {
  const [name, setName] = useState(dataset.name);
  const [description, setDescription] = useState(dataset.description);
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try { await api(datasetPath(dataset.dataset_id), jsonBody({ name, description }, "PATCH")); toastOk("数据集已更新"); onSaved(); }
    catch (e: any) { toastErr("保存失败", e.message); }
    finally { setBusy(false); }
  }
  return <Dialog open onOpenChange={open => !open && !busy && onClose()}><DialogContent><DialogTitle>编辑数据集</DialogTitle><DialogDescription>修改名称和描述。</DialogDescription>
    <label className="space-y-2">名称<Input aria-label="数据集名称" value={name} maxLength={120} onChange={e => setName(e.target.value)} /></label>
    <label className="space-y-2">描述<Textarea aria-label="数据集描述" value={description} maxLength={2000} onChange={e => setDescription(e.target.value)} /></label>
    <div className="flex justify-end gap-2"><Button variant="outline" disabled={busy} onClick={onClose}>取消</Button><Button disabled={busy || !name.trim()} onClick={save}>{busy ? "保存中…" : "保存"}</Button></div>
  </DialogContent></Dialog>;
}
function ItemDialog({ item, path, readOnly, onClose, onSaved }: { item: DatasetItem; path: string; readOnly: boolean; onClose: () => void; onSaved: () => void }) {
  const [query, setQuery] = useState(item.query);
  const [requirements, setRequirements] = useState(item.requirements.join("\n"));
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    try { await api(`${path}/items/${item.item_id}`, jsonBody({ query, requirements: requirements.split("\n").map(text => text.trim()).filter(Boolean) }, "PATCH")); toastOk("重回放条目已更新"); onSaved(); }
    catch (e: any) { toastErr("保存失败", e.message); }
    finally { setBusy(false); }
  }
  return <Dialog open onOpenChange={open => !open && !busy && onClose()}><DialogContent className="sm:max-w-[850px] max-h-[90vh] overflow-y-auto"><DialogTitle>Session 与重回放条件</DialogTitle>
    <DialogDescription>来源 {item.trace_id || item.session_id}。Checklist 默认采用首轮用户请求，可按每行一条细化。</DialogDescription>
    <label className="space-y-2">初始 Query<Textarea aria-label="初始 Query" className="min-h-[120px]" disabled={readOnly} value={query} onChange={e => setQuery(e.target.value)} /></label>
    <label className="space-y-2">Checklist（每行一条）<Textarea aria-label="Checklist" className="min-h-[140px]" disabled={readOnly} value={requirements} onChange={e => setRequirements(e.target.value)} /></label>
    <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">原始 Session 快照</summary>
      {(item.session?.turns || []).map((turn: any, index: number) => <div className="mt-3 space-y-2 border-t pt-3" key={index}><p className="text-xs font-semibold text-muted-foreground">第 {index + 1} 轮</p><p className="whitespace-pre-wrap break-words">{turn.prompt_text || "—"}</p><p className="whitespace-pre-wrap break-words rounded bg-muted/50 p-3">{turn.response_text || "—"}</p></div>)}
      <details className="mt-3"><summary className="cursor-pointer text-xs text-muted-foreground">完整 JSON（含工具调用）</summary><pre className="mt-2 max-h-[300px] overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify(item.session, null, 2)}</pre></details>
    </details>
    <div className="flex justify-end gap-2"><Button variant="outline" onClick={onClose}>关闭</Button><Button disabled={readOnly || busy || !query.trim() || !requirements.trim()} onClick={save}>{readOnly ? "快照只读" : busy ? "保存中…" : "保存条件"}</Button></div>
  </DialogContent></Dialog>;
}
function RunDialog({ path, count, onClose, onStarted }: { path: string; count: number; onClose: () => void; onStarted: (run: DatasetRun) => void }) {
  const [concurrency, setConcurrency] = useState(2);
  const [timeout, setTimeoutValue] = useState(600);
  const [interactions, setInteractions] = useState(4);
  const [busy, setBusy] = useState(false);
  async function start() {
    setBusy(true);
    try { const run = await api<DatasetRun>(`${path}/runs`, jsonBody({ concurrency, timeout_seconds: timeout, max_interactions: interactions })); toastOk("批量重回放已提交", "后台执行，可离开页面后继续查看"); onStarted(run); }
    catch (e: any) { toastErr("启动失败", e.message); }
    finally { setBusy(false); }
  }
  return <Dialog open onOpenChange={open => !open && !busy && onClose()}><DialogContent><DialogTitle>批量重回放 · {count} 条</DialogTitle>
    <DialogDescription>使用当前租户的重回放适配器，逐条在独立会话中执行，由裁判验证 Checklist。运行过程会调用 Agent 和模型。</DialogDescription>
    <label className="space-y-2">并发数<Input type="number" aria-label="并发数" min={1} max={2} value={concurrency} onChange={e => setConcurrency(Number(e.target.value))} /></label>
    <label className="space-y-2">单条超时（秒）<Input type="number" aria-label="单条超时" min={30} max={1800} value={timeout} onChange={e => setTimeoutValue(Number(e.target.value))} /></label>
    <label className="space-y-2">最多交互轮数<Input type="number" aria-label="最多交互轮数" min={1} max={20} value={interactions} onChange={e => setInteractions(Number(e.target.value))} /></label>
    <div className="flex justify-end gap-2"><Button variant="outline" disabled={busy} onClick={onClose}>取消</Button><Button disabled={busy || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 2 || !Number.isInteger(timeout) || timeout < 30 || timeout > 1800 || !Number.isInteger(interactions) || interactions < 1 || interactions > 20} onClick={start}>{busy ? "提交中…" : "开始重回放"}</Button></div>
  </DialogContent></Dialog>;
}
function ResultDialog({ result, onClose }: { result: Record<string, any>; onClose: () => void }) {
  return <Dialog open onOpenChange={open => !open && onClose()}><DialogContent className="sm:max-w-[900px] max-h-[90vh] overflow-y-auto"><DialogTitle>重回放结果</DialogTitle><DialogDescription>{result.request_id || "此条未生成重回放 Trace"}</DialogDescription>
    <div className="flex flex-wrap gap-3 text-sm"><Pill tone={result.completed ? "green" : result.ok ? "amber" : "red"}>{result.completed ? "Checklist 通过" : result.ok ? "Checklist 未通过" : "未完成执行"}</Pill><span>交互 {result.interaction_turns ?? "—"} 轮</span><span>用时 {result.elapsed_seconds ?? "—"} 秒</span></div>
    {result.error && <p className="whitespace-pre-wrap text-destructive">{result.error}</p>}
    <p className="whitespace-pre-wrap break-words rounded-lg bg-muted/40 p-4">{result.final_response || "暂无最终回复"}</p>
    <details open className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">Checklist 判定</summary>
      {result.checklist_report?.items?.length ? <ul className="mt-3 space-y-3">
        {result.checklist_report.items.map((entry: any, index: number) => <li key={entry.id || index} className="space-y-1 border-b pb-3 last:border-0">
          <div className="flex items-start gap-2"><Pill tone={entry.satisfied ? "green" : "red"}>{entry.satisfied ? "通过" : "未通过"}</Pill><span className="whitespace-pre-wrap">{entry.text || entry.requirement || entry.id}</span></div>
          {(entry.evidence || entry.reason) && <p className="whitespace-pre-wrap text-xs text-muted-foreground">{typeof (entry.evidence || entry.reason) === "string" ? entry.evidence || entry.reason : JSON.stringify(entry.evidence || entry.reason)}</p>}
        </li>)}
      </ul> : <p className="mt-3 text-sm text-muted-foreground">{result.checklist_report?.judge === "unavailable" ? "裁判不可用，未能判定完成情况。" : "暂无逐项判定详情。"}</p>}
    </details>
    <details className="rounded-lg border p-3"><summary className="cursor-pointer font-semibold">交互与工具调用轨迹</summary><pre className="mt-3 max-h-[500px] overflow-auto whitespace-pre-wrap break-all text-xs">{JSON.stringify({ interactions: result.interactions, messages: result.messages, artifacts: result.artifacts }, null, 2)}</pre></details>
    <div className="flex justify-end"><Button variant="outline" onClick={onClose}>关闭</Button></div>
  </DialogContent></Dialog>;
}
