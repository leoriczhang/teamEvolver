import { useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { EvaluatorAdapterRef } from "./api/client";

interface AdapterInfo extends EvaluatorAdapterRef { label?: string; enabled: boolean }
interface Inventory { available: AdapterInfo[]; source_editable: boolean }
const endpoint = "/api/replay-evaluator-adapter";
const keyOf = (ref?: EvaluatorAdapterRef) => ref ? `${ref.file}:${ref.revision}:${ref.source_hash}` : "";

export default function EvaluatorAdapterPicker({ value, onChange, disabled }: {
  value?: EvaluatorAdapterRef;
  onChange: (value: EvaluatorAdapterRef) => void;
  disabled: boolean;
}) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<{ file: string; code: string; revision: number | null } | null>(null);
  const sequence = useRef(0);

  async function load() {
    const current = ++sequence.current;
    try {
      const result = await api<Inventory>(endpoint);
      if (current === sequence.current) { setInventory(result); setError(""); }
    } catch (cause: any) { if (current === sequence.current) setError(cause.message || "读取评测器适配器失败"); }
  }
  useEffect(() => { void load(); return () => { sequence.current++; }; }, []);

  async function edit(example = false) {
    setBusy(true); setError("");
    try {
      const path = example ? `${endpoint}/example` : `${endpoint}/code?${new URLSearchParams({ file: value!.file, revision: String(value!.revision) })}`;
      const result = await api<{ file: string; code: string; revision?: number }>(path);
      setEditor({ file: result.file, code: result.code, revision: example ? null : result.revision || value?.revision || null });
    } catch (cause: any) { setError(cause.message || "读取评测器适配器代码失败"); }
    finally { setBusy(false); }
  }
  async function importFile(file?: File) {
    if (!file) return;
    setError("");
    try {
      if (!file.name.endsWith(".py") || file.size > 262_144) throw new Error("请提供不超过 256 KiB 的 Python 适配器文件");
      const code = await file.text();
      setEditor({ file: file.name, code, revision: inventory?.available.find(item => item.file === file.name)?.revision || null });
    } catch (cause: any) { setError(cause.message || "读取适配器文件失败"); }
  }
  async function saveCode() {
    if (!editor || !inventory?.source_editable || disabled) return;
    setBusy(true); setError("");
    try {
      const result = await api<AdapterInfo>(`${endpoint}/code`, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: editor.file.trim(), code: editor.code, expected_revision: editor.revision }) });
      await load();
      setEditor(null); onChange({ file: result.file, revision: result.revision, source_hash: result.source_hash });
    } catch (cause: any) { setError(cause.message || "保存适配器失败，请重新读取当前版本后重试"); }
    finally { setBusy(false); }
  }
  const selectedKey = keyOf(value);
  const available = inventory?.available || [];
  return <section aria-label="外部评测器适配器" className="space-y-3 rounded-md border border-border p-3">
    {error && <p role="alert" className="text-destructive">{error}</p>}
    <label className="block font-semibold">外部评测器适配器文件
      <select className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-2" value={selectedKey}
        disabled={disabled || busy || !inventory} onChange={event => {
          const chosen = available.find(item => keyOf(item) === event.target.value);
          if (chosen) onChange({ file: chosen.file, revision: chosen.revision, source_hash: chosen.source_hash });
        }}>
        <option value="">请选择适配器文件</option>
        {value && !available.some(item => keyOf(item) === selectedKey) && <option value={selectedKey}>{value.file} · v{value.revision}（已固定的历史版本）</option>}
        {available.map(item => <option key={keyOf(item)} value={keyOf(item)} disabled={!item.enabled}>
          {item.label || item.file} · {item.file} · v{item.revision}{item.enabled ? "" : "（已禁用）"}
        </option>)}
      </select>
    </label>
    <p className="text-muted-foreground">适配器负责外部接口的请求、变量映射和评分响应转换。所选文件版本随回放任务固定；修改文件不会改变已受理的任务。</p>
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" disabled={busy} onClick={load}>刷新适配器列表</Button>
      {inventory?.source_editable && <>
        <Button size="sm" variant="outline" disabled={disabled || busy || !value} onClick={() => edit()}>编辑所选评测器适配器</Button>
        <Button size="sm" variant="outline" disabled={disabled || busy} onClick={() => edit(true)}>从 HTTP 示例创建</Button>
      </>}
    </div>
    {inventory?.source_editable ? <label className="block font-semibold">上传评测器 Python 适配器
      <input className="mt-1 block w-full" type="file" accept=".py,text/x-python" disabled={disabled || busy} onChange={event => {
        void importFile(event.target.files?.[0]); event.target.value = "";
      }} />
    </label> : inventory && <p className="text-muted-foreground">当前身份可选择已有文件。上传或编辑 Python 源码需要服务 Root Key 认证，与 Replay 适配器相同。</p>}
    {editor && <div className="space-y-2">
      <label className="block font-semibold">评测器适配器文件名
        <Input className="mt-1" value={editor.file} disabled={busy} onChange={event => {
          const file = event.target.value;
          setEditor({ ...editor, file, revision: inventory?.available.find(item => item.file === file)?.revision || null });
        }} />
      </label>
      <label className="block font-semibold">评测器适配器 Python 源码
        <Textarea rows={14} className="mt-1 font-mono text-xs" value={editor.code} disabled={busy}
          onChange={event => setEditor({ ...editor, code: event.target.value })} />
      </label>
      <p className="text-muted-foreground">仅保存可信代码，凭证使用 Secret Ref。保存文件后仍需点击“保存回放评价方式”，才会用于后续任务。</p>
      <div className="flex gap-2">
        <Button size="sm" disabled={busy || !editor.file.trim().endsWith(".py") || !editor.code.trim()} onClick={saveCode}>保存评测器适配器版本</Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => setEditor(null)}>取消编辑适配器</Button>
      </div>
    </div>}
  </section>;
}
