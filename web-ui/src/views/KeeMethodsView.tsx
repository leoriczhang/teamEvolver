import { useCallback, useEffect, useRef, useState } from "react";
import { api as nativeApi } from "@/api/client";
import { Button } from "@/components/ui/button";
import { createRequestId } from "@/lib/requestId";

const api = <T,>(path: string, opts?: Omit<RequestInit, "body"> & {body?: unknown}) =>
  nativeApi<T>(path, opts ? {...opts, headers: {...opts.headers, "Content-Type": "application/json"}, body: opts.body === undefined ? undefined : JSON.stringify(opts.body)} : undefined);

type Job = { id: string; state: string; error?: string; request: Record<string, any>; result: Record<string, any> };
const root = "/kee/v1";
const pretty = (v: unknown) => JSON.stringify(v, null, 2);

export default function KeeMethodsView({ active, userId, admin }: { active: boolean; userId: string; admin: boolean }) {
  const [jobs, setJobs] = useState<Job[]>([]); const [selected, setSelected] = useState("");
  const [collection, setCollection] = useState(""); const [uri, setUri] = useState("");
  const [audience, setAudience] = useState(userId);
  const readers = () => Array.from(new Set(audience.split(",").map(v => v.trim()).filter(Boolean)));
  const [packId, setPackId] = useState("handover"); const [clarification, setClarification] = useState("");
  const [target, setTarget] = useState("viking://resources/agent_knowledge_workspace/wiki/methods");
  const [candidate, setCandidate] = useState(""); const [note, setNote] = useState("");
  const [gaps, setGaps] = useState(false); const [sources, setSources] = useState<any[]>([]);
  const [releases, setReleases] = useState<any[]>([]); const [error, setError] = useState("");
  const [busy, setBusy] = useState(false); const [feedback, setFeedback] = useState("");
  const actionLock = useRef(false); const pendingBuild = useRef<{signature: string; key: string} | null>(null);
  const [wikiText, setWikiText] = useState("");
  const job = jobs.find(j => j.id === selected);
  const load = useCallback(async () => {
    if (admin) setJobs(await api<Job[]>(root + "/methods/jobs"));
    setReleases((await api<{items: any[]}>(root + "/wiki/releases")).items);
  }, [admin]);
  useEffect(() => {
    if (!active) return;
    void load().catch(e => setError(String(e.message || e)));
    const timer = window.setInterval(() => void load().catch(() => {}), 3000);
    return () => window.clearInterval(timer);
  }, [active, load]);
  useEffect(() => { setCandidate(job?.result.candidate ? pretty(job.result.candidate) : ""); }, [selected, job?.result.candidate_digest]);
  useEffect(() => {
    if (!admin || !job?.result.collection_id) return;
    let cancelled = false;
    api<{sources: any[]}>(`${root}/sources/${job.result.collection_id}`).then(v => { if (!cancelled) setSources(v.sources); }).catch(e => { if (!cancelled) setError(String(e.message || e)); });
    return () => { cancelled = true; };
  }, [admin, selected, job?.result.collection_id]);
  async function run(work: () => Promise<unknown>) {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(true); setError("");
    try { await work(); await load(); } catch (e: any) { setError(String(e.message || e)); } finally { actionLock.current = false; setBusy(false); }
  }
  const operation = (name: string, body?: unknown) => run(async () => {
    const value = await api<Job>(`${root}/methods/jobs/${selected}/${name}`, {method: "POST", body});
    setSelected(value.id);
  });
  return <div className="space-y-5" data-testid="kee-methods">
    <div><h2 className="text-2xl font-bold">知识与方法 · Ontology</h2>
      <p className="text-muted-foreground">来源、澄清、方法编制、证据审核与 Wiki 发布。Agent 沿用 OV 读取已批准 Wiki。</p></div>
    {error && <div role="alert" className="rounded border border-red-400 bg-red-50 p-3 text-red-700">{error}</div>}
    {admin && <section className="space-y-3 rounded border p-4"><h3 className="font-semibold">来源与澄清</h3>
      <label className="block">Viking 文件或 Wiki 目录<input aria-label="Viking 来源" className="w-full rounded border p-2" value={uri} onChange={e => setUri(e.target.value)} /></label>
      <Button disabled={busy || !uri} onClick={() => void run(async () => {
        const value = await api<{collection_id: string}>(root + "/sources", {method: "POST", body: {roots: [uri], uploads: [], readers: readers()}});
        setCollection(value.collection_id);
      })}>冻结来源</Button>
      <label className="block">或上传资料<input type="file" accept=".md,.txt,.json,.html,.pdf,.docx,.xlsx" onChange={e => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        void run(async () => {
          for (const file of files) {
            const bytes = new Uint8Array(await file.arrayBuffer());
            let binary = "";
            for (let offset = 0; offset < bytes.length; offset += 16384) binary += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
            const value = await api<{collection_id: string}>(root + "/documents", {method: "POST", body: {filename: file.name, content_base64: btoa(binary), source_id: file.name, readers: readers()}});
            setCollection(value.collection_id);
          }

        });
      }} /></label>
      <label className="block">可读 TE 成员（逗号分隔）<input aria-label="可读成员" value={audience} onChange={e => setAudience(e.target.value)} className="w-full rounded border p-2" /></label>
      <p className="break-all text-sm">来源集合：{collection || "尚未选择"}</p>
      <label className="block">方法包标识<input aria-label="方法包标识" className="ml-3 rounded border p-2" value={packId} onChange={e => setPackId(e.target.value)} /></label>
      <label className="block">澄清与编制要求<textarea aria-label="澄清" className="w-full rounded border p-2" value={clarification} onChange={e => setClarification(e.target.value)} /></label>
      <Button disabled={busy || !collection} onClick={() => void run(async () => {
        const signature = JSON.stringify([collection, packId, clarification]);
        if (pendingBuild.current?.signature !== signature) pendingBuild.current = {signature, key: createRequestId()};
        const existing = jobs.find(j => j.request.collection_id === collection && j.request.pack_id === packId && j.request.clarification === clarification && j.state !== "cancelled");
        const value = existing || await api<Job>(root + "/methods/build", {method: "POST", body: {submission_key: pendingBuild.current.key, collection_id: collection, pack_id: packId, clarification}});
        setSelected(value.id);
      })}>开始内部 Compile</Button>
    </section>}
    {admin && <section className="rounded border p-4"><h3 className="font-semibold">构建、评审与历史任务</h3>
      {jobs.map(j => <button key={j.id} className={`my-1 block w-full rounded border p-2 text-left ${selected === j.id ? "bg-accent/10" : ""}`} onClick={() => setSelected(j.id)}>
        {j.id} · {j.state} · {j.result.executor || "历史 OV 执行器"}{j.error ? " · " + j.error : ""}
      </button>)}
    </section>}
    {job && admin && <section className="space-y-3 rounded border p-4">
      <div className="grid gap-4 lg:grid-cols-2"><div><h3 className="font-semibold">方法、Schema 与候选编辑</h3>
        <p className="text-sm">保存修改会使原 Schema 确认及批准失效。确认 Schema 后不再调用模型。</p>
        <textarea aria-label="候选结构" className="h-96 w-full rounded border p-3 font-mono text-xs" value={candidate} onChange={e => setCandidate(e.target.value)} />
        <Button disabled={busy || !candidate} onClick={() => void run(() => api(`${root}/methods/jobs/${selected}/candidate`, {method: "PUT", body: {expected_digest: job.result.candidate_digest, candidate: JSON.parse(candidate)}}))}>保存候选</Button>
      </div><div><h3 className="font-semibold">冻结原文与证据</h3><div className="h-96 overflow-auto rounded border p-3 text-sm">
        {sources.map(s => <details key={s.source_id}><summary>{s.uri || s.source_id} · {s.revision.slice(0, 12)}</summary><pre className="whitespace-pre-wrap">{s.text}</pre></details>)}
        {job.result.candidate?.methods?.map((m: any) => <div key={m.method_id} className="mt-3 border-t pt-2"><b>{m.title}</b>
          {m.evidence.map((e: any, i: number) => <blockquote key={i} className="my-2 border-l-2 pl-2">{e.quote}<p className="text-xs">{e.source_id} · 字符 {e.start}—{e.end}</p></blockquote>)}
        </div>)}
      </div></div></div>
      <div className="flex flex-wrap gap-2">
        {job.state === "schema_review" && <Button disabled={busy} onClick={() => void operation("confirm-schema", {expected_digest: job.result.candidate_digest})}>确认 Schema</Button>}
        {job.state === "failed" && <Button disabled={busy} onClick={() => void operation("retry")}>恢复／对账重试</Button>}
        {!job.result.release && !["cancelled", "published"].includes(job.state) && <Button disabled={busy} onClick={() => void operation("cancel")}>取消任务</Button>}
      </div>
      <label className="block">Wiki 发布目标<input aria-label="发布目标" className="w-full rounded border p-2" value={target} onChange={e => setTarget(e.target.value)} /></label>
      <label className="block">审核意见<textarea aria-label="审核意见" className="w-full rounded border p-2" value={note} onChange={e => setNote(e.target.value)} /></label>
      <label><input type="checkbox" checked={gaps} onChange={e => setGaps(e.target.checked)} /> 已核对并接受覆盖缺口</label>
      <div className="flex flex-wrap gap-2">
        {["review_ready", "prepared", "approved", "imported"].includes(job.state) && <Button disabled={busy} onClick={() => void operation("prepare", {target_uri: target, readers: readers()})}>Prepare</Button>}
        {job.state === "prepared" && <Button disabled={busy || !note} onClick={() => void operation("approve", {note, acknowledge_gaps: gaps})}>人工批准</Button>}
        {job.state === "approved" && <Button disabled={busy} onClick={() => void operation("commit", {commit_key: job.result.approval.approval_id})}>发布 Wiki</Button>}
        {job.result.release && job.state !== "withdrawn" && job.state !== "running" && <Button disabled={busy} onClick={() => void operation("withdraw")}>撤回整个版本</Button>}
        {job.result.candidate && <Button disabled={busy} onClick={() => void operation("rollback", {commit_key: createRequestId()})}>创建回滚候选</Button>}
        {job.result.candidate && <Button disabled={busy} onClick={() => void run(async () => {
          const value = await api<unknown>(`${root}/methods/jobs/${selected}/export`);
          const blob = new Blob([pretty(value)], {type: "application/json"}); const url = URL.createObjectURL(blob);
          const a = document.createElement("a"); a.href = url; a.download = "kee-wiki-export.json"; a.click(); URL.revokeObjectURL(url);
        })}>导出结构与 Wiki</Button>}
      </div>
      {job.result.approval?.note && <p className="text-sm">已保存审核意见：{job.result.approval.note}</p>}
      <p className="text-sm">正文校验：{job.result.verified_pages?.length || 0} 页 · 发布阶段：{job.result.release?.state || "尚未发布"} · 模型调用：{job.result.usage?.calls ?? "未执行"}</p>
      {job.result.release?.receipt && <pre className="overflow-auto rounded bg-muted p-2 text-xs">{pretty(job.result.release.receipt)}</pre>}
    </section>}
    <section className="space-y-2 rounded border p-4"><h3 className="font-semibold">已发布 Wiki（只读）</h3>
      <p className="text-sm">OV 当前没有包级原子提交保证；发布中和索引等待均不表示已发布。</p>
      {releases.map(r => <div key={r.key} className="break-all">{r.release.state} · {r.release.receipt.entry_uri} · {r.release.pack_digest.slice(0, 16)}
        {r.release.state === "published" && <select aria-label="阅读 Wiki 页面" className="ml-2 rounded border p-1" defaultValue="" onChange={e => {
          if (!e.target.value) return;
          const parts = r.key.split("/");
          void run(async () => { const value = await api<{content: string}>(`${root}/wiki/releases/${parts[1]}/${parts[2]}/pages/${e.target.value}`); setWikiText(value.content); });
        }}><option value="">选择页面阅读</option>{Object.keys(r.release.pages).map(path => <option key={path} value={path}>{path}</option>)}</select>}
      </div>)}
      {wikiText && <pre aria-label="Wiki 正文" className="max-h-96 overflow-auto whitespace-pre-wrap rounded border p-3 text-sm">{wikiText}</pre>}
    </section>
    <section className="space-y-2 rounded border p-4"><h3 className="font-semibold">使用反馈</h3>
      <textarea aria-label="使用反馈" value={feedback} onChange={e => setFeedback(e.target.value)} className="w-full rounded border p-2" />
      <Button disabled={busy || !feedback} onClick={() => void run(async () => { await api<unknown>(root + "/feedback", {method: "POST", body: {note: feedback}}); setFeedback(""); })}>提交反馈</Button>
    </section>
  </div>;
}
