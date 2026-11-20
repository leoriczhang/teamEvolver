import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { PullProgressPanel } from "./PullProgressPanel";

type Adapter = { file: string; revision?: number; connection_profiles?: Record<string, unknown>; connection_options?: string[] };
type Connection = {
  connection_id: string; revision: number; adapter_id: string; adapter_revision: number;
  enabled: boolean; label: string; settings: object; default_filters: object; schedule: object;
};
const endpoint = "/api/datasource/connections";
const schedule = { enabled: false, time: "02:00", timezone: "Asia/Shanghai", window: "previous_day" };

export default function NamedConnections({ files }: { files: Adapter[] }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<Connection[]>([]);
  const [selected, setSelected] = useState("");
  const [id, setId] = useState("");
  const [file, setFile] = useState("");
  const [revision, setRevision] = useState("1");
  const [settings, setSettings] = useState("{}");
  const [filters, setFilters] = useState("{}");
  const [daily, setDaily] = useState(JSON.stringify(schedule, null, 2));
  const [enabled, setEnabled] = useState(true);
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [job, setJob] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [baseRevision, setBaseRevision] = useState(0);
  useEffect(() => {
    if (!open) return;
    let stale = false;
    api<{ connections: Connection[] }>(endpoint).then(data => { if (!stale) setRows(data.connections); })
      .catch(e => { if (!stale) setError(e.message); });
    return () => { stale = true; };
  }, [open]);
  function select(row?: Connection) {
    setSelected(row?.connection_id || ""); setId(row?.connection_id || ""); setName(row?.label || "");
    setFile(row?.adapter_id || ""); setRevision(String(row?.adapter_revision || 1));
    setEnabled(row?.enabled ?? true); setBaseRevision(row?.revision || 0);
    setSettings(JSON.stringify(row?.settings || {}, null, 2)); setFilters(JSON.stringify(row?.default_filters || {}, null, 2));
    setDaily(JSON.stringify(row?.schedule || schedule, null, 2));
    setDirty(false); setJob(""); setError("");
  }
  const adapter = files.find(item => item.file === file && item.revision === Number(revision));
  const profiles = Object.keys(adapter?.connection_profiles || {});
  async function save() {
    setBusy(true); setError("");
    try {
      const row = await api<Connection>(endpoint + "/" + encodeURIComponent(id), {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          label: name || id, adapter_id: file, adapter_revision: Number(revision), enabled, settings: JSON.parse(settings),
          default_filters: JSON.parse(filters), schedule: JSON.parse(daily), expected_revision: baseRevision,
        }),
      });
      setRows(old => [...old.filter(item => item.connection_id !== row.connection_id), row]); select(row);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  async function pull() {
    setBusy(true); setError("");
    try {
      const result = await api<{ job_id: string }>(endpoint + "/" + encodeURIComponent(selected) + "/pull", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_timestamp: new Date(from).toISOString(), to_timestamp: new Date(to).toISOString() }),
      });
      setJob(result.job_id);
    } catch (e) { setError(e instanceof Error ? e.message : "拉取失败"); }
    finally { setBusy(false); }
  }
  return <details className="space-y-3 border-b pb-4" onToggle={e => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer font-semibold">独立数据源连接</summary>
    {open && <div className="space-y-3">
      <label>选择连接 <select disabled={busy} aria-label="选择独立连接" value={selected}
        onChange={e => select(rows.find(row => row.connection_id === e.target.value))}>
        <option value="">新增连接</option>{rows.map(row =>
          <option key={row.connection_id} value={row.connection_id}>{row.label} · v{row.revision}</option>)}
      </select></label>
      <fieldset disabled={busy} className="grid gap-3 sm:grid-cols-2" onChange={() => setDirty(true)}>
        <label>连接 ID<Input disabled={!!selected} value={id} onChange={e => setId(e.target.value)} /></label>
        <label>连接名称<Input value={name} onChange={e => setName(e.target.value)} /></label>
        <label>数据源适配器文件 <select value={file} onChange={e => {
          setFile(e.target.value); setRevision(String(files.find(f => f.file === e.target.value)?.revision || 1));
        }}><option value="">选择已登记文件</option>{files.map(f => <option key={f.file}>{f.file}</option>)}</select></label>
        <label>固定文件版本<Input type="number" min="1" value={revision} onChange={e => setRevision(e.target.value)} /></label>
        {profiles.length > 0 && <label>从适配器选择连接配置 <select aria-label="从适配器选择连接配置" value=""
          onChange={e => { if (!e.target.value) return;
            try { setSettings(JSON.stringify({ ...JSON.parse(settings), profile: e.target.value }, null, 2)); setError(""); }
            catch { setError("请先修正连接设置 JSON"); } }}>
          <option value="">选择后填入下方设置</option>{profiles.map(profile => <option key={profile}>{profile}</option>)}
        </select></label>}
        <label>连接设置 JSON（profile / options）<Textarea value={settings} onChange={e => setSettings(e.target.value)} />{adapter?.connection_options?.length ? <small>可调选项 {adapter.connection_options.join("、")}</small> : null}</label>
        <label>默认过滤条件 JSON<Textarea value={filters} onChange={e => setFilters(e.target.value)} /></label>
        <label>每日拉取计划 JSON<Textarea value={daily} onChange={e => setDaily(e.target.value)} /></label>
        <label><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> 启用此连接</label>
      </fieldset>
      <Button disabled={busy || !id || !file || !dirty} onClick={save}>保存独立连接</Button>
      {selected && <div className="flex flex-wrap items-end gap-3">
        <label>起始时间（本地）<Input type="datetime-local" value={from} onChange={e => setFrom(e.target.value)} /></label>
        <label>结束时间（本地）<Input type="datetime-local" value={to} onChange={e => setTo(e.target.value)} /></label>
        <Button disabled={busy || dirty || !enabled || !from || !to || to <= from} onClick={pull}>拉取此连接</Button>
      </div>}
      {job && <PullProgressPanel key={job} jobId={job} />}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>}
  </details>;
}
