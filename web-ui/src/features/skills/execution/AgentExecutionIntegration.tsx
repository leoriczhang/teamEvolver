import { useEffect, useRef, useState } from "react";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

interface Integration {
  profile_id: string;
  current_release_id: string;
  composition_hash: string;
  metadata: Record<string, unknown>;
  variables: Array<{ name: string; managed_context_component_ids: string[] }>;
  requirements: string[];
}

export default function AgentExecutionIntegration({ profileId }: { profileId: string }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState<Integration | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [checking, setChecking] = useState(false);
  const [match, setMatch] = useState<{ matched: boolean; reason: string; release_id?: string; composition_hash?: string;
    decision?: string; eligible_for_evolution: boolean; targets: unknown[] } | null>(null);
  const sequence = useRef(0);
  const matchSequence = useRef(0);

  useEffect(() => () => { sequence.current++; matchSequence.current++; }, [profileId]);

  async function load() {
    const current = ++sequence.current;
    setBusy(true); setError(""); setCopied(false);
    try {
      const result = await api<Integration>(`/api/skill-evolution/execution-profiles/${encodeURIComponent(profileId)}/integration`);
      if (current === sequence.current) setValue(result);
    } catch (cause: any) {
      if (current === sequence.current) setError(cause.message || "读取 Agent 接入信息失败");
    } finally { if (current === sequence.current) setBusy(false); }
  }

  async function copy() {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(value.metadata, null, 2));
      setCopied(true);
    } catch { setError("复制失败，请从下方手动选取接入元数据"); }
  }

  async function checkMatch() {
    if (!sessionId.trim()) return;
    const current = ++matchSequence.current;
    setChecking(true); setError(""); setMatch(null);
    try {
      const result = await api<NonNullable<typeof match>>(`/api/skill-evolution/execution-profiles/${encodeURIComponent(profileId)}/session-match?${new URLSearchParams({ session_id: sessionId.trim() })}`);
      if (current === matchSequence.current) setMatch(result);
    } catch (cause: any) { if (current === matchSequence.current) setError(cause.message || "检查会话匹配失败"); }
    finally { if (current === matchSequence.current) setChecking(false); }
  }

  return <details className="rounded-md border border-border p-3" open={open} onToggle={event => {
    setOpen(event.currentTarget.open);
    if (event.currentTarget.open && !value && !busy) void load();
  }}>
    <summary className="cursor-pointer text-xs font-semibold">接入真实 Agent 的会话</summary>
    <div className="mt-3 space-y-2 text-xs">
      <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
        <li>Agent 使用此处登记的真实 Prompt 与上下文；按实际使用的发布版本上报。</li>
        <li>将下方元数据放入 Langfuse Trace 的 metadata，Generation 记录本次实际模型请求；变量值和对话输入填写本次实际内容。</li>
        <li>在连接数据源中拉取 Session，再在进化工作台选择此执行配置及 Prompt 进化。</li>
      </ol>
      <p className="text-muted-foreground">登记配置不会替 Agent 修改 Prompt。模板相同、变量取值不同仍归属同一执行配置；缺少真实使用记录时不会仅凭名称判定已使用。</p>
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {value && <>
        <p className="break-all">发布：{value.current_release_id} · 组合哈希：{value.composition_hash}</p>
        {value.variables.length > 0 && <div className="space-y-1">{value.variables.map(item => <p key={item.name}>
          {item.name}：{item.managed_context_component_ids.length ? `${item.managed_context_component_ids.length} 个受管理的上下文取值` : "每次请求的输入变量"}
        </p>)}</div>}
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-[11px]">{JSON.stringify(value.metadata, null, 2)}</pre>
        <p className="text-muted-foreground">示例中的默认值和空输入仅用于说明结构，须替换为本次请求实际值；请勿把鉴权密钥写入元数据。</p>
        <Button size="sm" variant="outline" onClick={copy}>{copied ? "已复制接入元数据" : "复制接入元数据"}</Button>
      </>}
      <Button size="sm" variant="outline" disabled={busy} onClick={load}>{busy ? "读取中…" : "重新读取接入信息"}</Button>
      <section aria-label="检查真实会话匹配" className="space-y-2 border-t border-border pt-3">
        <label className="block font-semibold">已归档的 Session ID
          <Input className="mt-1" value={sessionId} disabled={checking} onChange={event => {
            setSessionId(event.target.value); setMatch(null);
          }} placeholder="从会话列表复制 Session ID" />
        </label>
        <Button size="sm" variant="outline" disabled={checking || !sessionId.trim()} onClick={checkMatch}>{checking ? "检查中…" : "检查会话匹配"}</Button>
        {match && <div role="status" className="space-y-1 text-muted-foreground">
          <p className="font-semibold text-foreground">{match.matched ? "会话已匹配此执行配置" : "会话未匹配此执行配置"}</p>
          {match.release_id && <p className="break-all">本次核对发布：{match.release_id}{match.composition_hash ? ` · 组合哈希：${match.composition_hash}` : ""}</p>}
          <p className="break-words">{match.reason}</p>
          <p>匹配请求：{match.targets.length} 个 · {match.eligible_for_evolution ? "已满足此项证据准入检查" : "尚不满足此项证据准入检查"}</p>
          <p>匹配只核对真实使用记录和当前准入状态，不代表会话尚未消费、已有足够进化信号或已启动进化。</p>
        </div>}
      </section>
    </div>
  </details>;
}
