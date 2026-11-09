import { useCallback, useEffect, useRef, useState } from "react";
import ExecutionObjectList from "@/features/skills/execution/ExecutionObjectList";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import type { ExecutionProfile } from "@/features/skills/api/client";
import { toastErr, toastOk } from "@/lib/toast";
import ExecutionReleasePanel from "./ExecutionReleasePanel";
import ManagedVariableContextForm from "./ManagedVariableContextForm";
import AgentExecutionIntegration from "./execution/AgentExecutionIntegration";

const URL = "/api/skill-evolution/execution-profiles";
const inputClass = "mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs";
const environmentExample = JSON.stringify({
  request: { model: "actual-model-id", tools: [], temperature: 0 },
  template_variables: {},
  validation: {
    holdout_dataset_ids: [], max_probe_cases: 4, max_extra_runs: 5,
    repetitions: 2, max_total_tokens: 1000000,
  },
}, null, 2);

function parseEnvironment(text: string): Record<string, unknown> {
  let value: unknown;
  try { value = JSON.parse(text.trim() || "{}"); }
  catch { throw new Error("冻结执行环境必须是有效的 JSON 对象"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("冻结执行环境必须是 JSON 对象");
  }
  function check(item: unknown) {
    if (!item || typeof item !== "object") return;
    for (const [key, child] of Object.entries(item)) {
      if (["api_key", "password", "authorization", "access_token", "secret_key", "messages"].includes(key.toLowerCase())) {
        throw new Error("执行环境不能包含 messages 或凭证；凭证须使用 Secret Ref");
      }
      check(child);
    }
  }
  check(value);
  return value as Record<string, unknown>;
}

export default function ExecutionProfilePanel({ active, profileId, onChange, isAdmin }: {
  active: boolean;
  profileId: string;
  onChange: (profileId: string) => void;
  isAdmin: boolean;
}) {
  const [profiles, setProfiles] = useState<ExecutionProfile[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [registerVariables, setRegisterVariables] = useState(false);
  const [model, setModel] = useState("");
  const [variableDefaults, setVariableDefaults] = useState<Record<string, string>>({});
  const [registrationError, setRegistrationError] = useState("");
  const [preview, setPreview] = useState<{ raw: Record<string, unknown>; summary: { component_count: number; affected_skill_count: number } } | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const previewSequence = useRef(0);
  const [draft, setDraft] = useState({
    profile_id: "", runtime_type: "agentshub", agent_id: "", user_id: "",
    affected_skills: "", prompt: "", context: "", slot: "system", environment: "",
  });
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api<{ profiles: ExecutionProfile[] }>(URL);
      setProfiles(result.profiles || []);
      setError("");
    } catch (cause: any) {
      setError(cause.message || "执行配置加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { if (active) void load(); }, [active, load]);
  useEffect(() => { setRegisterVariables(false); }, [profileId]);
  const selected = profiles.find(profile => profile.profile_id === profileId);
  const versionParams = new URLSearchParams(window.location.search);
  versionParams.set("view", "dashboard");
  versionParams.set("tab", "prompts");
  if (profileId) versionParams.set("execution_profile", profileId);
  else versionParams.delete("execution_profile");
  versionParams.delete("execution_release");
  const skills = [...new Set(draft.affected_skills.split(/[\s,，]+/).filter(Boolean))];
  const placeholders = [...new Set([...`${draft.prompt}\n${draft.context}`.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g)].map(match => match[1]))];
  function changeDraft(values: Partial<typeof draft>) {
    previewSequence.current++;
    setPreview(null); setRegistrationError(""); setPreviewing(false);
    setDraft(current => ({ ...current, ...values }));
  }
  function invalidatePreview() {
    previewSequence.current++;
    setPreview(null); setRegistrationError(""); setPreviewing(false);
  }
  const canCreate = isAdmin && draft.profile_id.trim() && draft.agent_id.trim() && draft.user_id.trim()
    && draft.runtime_type.trim() && draft.prompt.trim();

  function registrationBody(): Record<string, unknown> {
    const environment = parseEnvironment(draft.environment);
    if (model.trim()) environment.request = { ...(environment.request as Record<string, unknown> || {}), model: model.trim() };
    if (placeholders.length) {
      const entered = Object.fromEntries(Object.entries(variableDefaults).filter(([name]) => placeholders.includes(name)));
      const defaults = { ...(environment.template_variables as Record<string, unknown> || {}), ...entered };
      const missing = placeholders.filter(name => !Object.prototype.hasOwnProperty.call(defaults, name));
      if (missing.length) throw new Error(`请填写变量原始默认值，或明确使用空值：${missing.join("、")}`);
      environment.template_variables = defaults;
      environment.template_input_contract = {
        schema_version: "teamevolver.template-input-contract.v1", variable_names: Object.keys(defaults),
      };
    }
    const prefix = draft.profile_id.trim();
    const components = [];
    if (draft.prompt.trim()) components.push({
      component_id: `${prefix}:agent-prompt`, kind: "agent_prompt", slot: draft.slot,
      order: components.length, content: draft.prompt,
    });
    if (draft.context.trim()) components.push({
      component_id: `${prefix}:business-context`, kind: "context_fragment", slot: "context",
      order: components.length, content: draft.context,
    });
    return { profile_id: prefix, runtime_type: draft.runtime_type.trim(), agent_id: draft.agent_id.trim(),
      user_id: draft.user_id.trim(), affected_skill_ids: skills, environment, components };
  }

  async function previewRegistration() {
    if (!canCreate) return;
    const current = ++previewSequence.current;
    setPreviewing(true); setRegistrationError(""); setPreview(null);
    try {
      const raw = registrationBody();
      const result = await api<{ profile_id: string; preview_hash: string; summary: { component_count: number; affected_skill_count: number } }>(`${URL}/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(raw),
      });
      if (current !== previewSequence.current) return;
      if (result.profile_id !== raw.profile_id || !result.preview_hash) throw new Error("登记预览与当前输入不一致，请重新预览");
      setPreview({ raw, summary: result.summary });
    } catch (cause: any) {
      if (current === previewSequence.current) {
        setRegistrationError(cause.message || "请检查登记内容");
        toastErr("预览业务执行基线失败", cause.message);
      }
    } finally { if (current === previewSequence.current) setPreviewing(false); }
  }

  async function create() {
    if (!preview || saving) return;
    setSaving(true); setRegistrationError("");
    try {
      const result = await api<ExecutionProfile>(URL, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(preview.raw),
      });
      await load();
      onChange(result.profile_id || draft.profile_id.trim());
      toastOk("业务执行基线已登记", "可继续登记变量上下文，并将 Agent 会话接入此配置");
      setPreview(null); setVariableDefaults({}); setModel("");
      setDraft({ ...draft, profile_id: "", prompt: "", context: "" });
    } catch (cause: any) {
      setRegistrationError(cause.message || "登记失败，请重新预览"); setPreview(null);
      toastErr("登记业务执行基线失败", cause.message);
    } finally { setSaving(false); }
  }

  return (
    <div className="space-y-3 rounded-lg border border-border p-3">
      <label className="block text-xs font-semibold">
        本次业务 Agent 执行配置
        <select className={inputClass} value={profileId} disabled={!isAdmin || loading}
          onChange={event => onChange(event.target.value)}>
          <option value="">未指定执行配置</option>
          {profileId && !selected && <option value={profileId}>{profileId}（不可用或尚未加载）</option>}
          {profiles.map(profile => <option key={profile.profile_id} value={profile.profile_id}>
            {profile.profile_id} · {profile.agent_id} · v{profile.revision}
          </option>)}
        </select>
      </label>
      <p className="text-[11px] text-muted-foreground">
        此处绑定业务 Agent 的真实 Prompt／上下文基线；下方 TE 引擎 Prompt 用于生成和评测候选，两者独立。
        基线登记后保持只读，后续内容修改走候选评审。
      </p>
      {isAdmin && <a className="inline-block text-xs font-semibold text-accent underline" href={`?${versionParams}`}>
        查看 Prompt 历史版本与指标
      </a>}
      {error && <p role="alert" className="text-xs text-destructive">业务执行配置加载失败：{error}</p>}
      {selected && <details>
        <summary className="cursor-pointer text-xs font-semibold">查看当前发布内容</summary>
        <div className="mt-2 space-y-2 text-xs text-muted-foreground">
          <div>Agent：{selected.agent_id} · 用户：{selected.user_id} · Runtime：{selected.runtime_type}</div>
          <div>关联 Skill：{selected.affected_skill_ids.join("、") || "无（纯 Prompt 基线）"}</div>
          <div>Skill 依赖：{selected.skill_dependencies?.length ?? selected.affected_skill_ids.length} 个</div>
          <div className="break-all">当前组合发布：{selected.current_release_id}</div>
          <div className="break-all">组合哈希：{selected.composition_hash}</div>
          <div>冻结执行环境
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2">
              {JSON.stringify(selected.environment || {}, null, 2)}
            </pre>
          </div>
          <ExecutionObjectList plan={selected.execution_plan}>{component => <div key={component.component_id}>
            <div>{component.component_id} · {component.slot} · v{component.revision}</div>
            {component.binding?.inline_variable && <p className="mt-1 break-all">
              变量上下文：{component.binding.inline_variable.name} · 注入位置：{component.binding.inline_variable.component_id}
            </p>}
            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2">{component.content}</pre>
          </div>}</ExecutionObjectList>
        </div>
      </details>}
      {selected && <AgentExecutionIntegration key={`${selected.profile_id}:${selected.current_release_id}`} profileId={selected.profile_id} />}
      {selected && <ExecutionReleasePanel key={selected.profile_id} profileId={selected.profile_id} isAdmin={isAdmin}
        onUpdated={detail => setProfiles(current => current.map(profile => profile.profile_id === detail.profile_id ? detail : profile))} />}
      {selected && isAdmin && <div className="space-y-3 border-t border-border pt-3">
        {!registerVariables && <Button size="sm" variant="outline" onClick={() => setRegisterVariables(true)}>
          以此配置登记变量上下文
        </Button>}
        {registerVariables && <ManagedVariableContextForm key={`${selected.profile_id}:${selected.current_release_id}`}
          source={selected} onCancel={() => setRegisterVariables(false)} onRegistered={async result => {
            await load();
            setProfiles(current => [...current.filter(profile => profile.profile_id !== result.profile_id), result]);
            setRegisterVariables(false);
            onChange(result.profile_id);
          }} />}
      </div>}
      {isAdmin && <details>
        <summary className="cursor-pointer text-xs font-semibold">登记现有业务 Agent 执行基线</summary>
        <div className="mt-3 space-y-3">
          <p className="text-[11px] text-muted-foreground">填写 Agent 当前实际使用的内容及全部受影响 Skill，作为后续对照基线。纯 Prompt Agent 可不关联任何 Skill。登记不触发运行或热更新。</p>
          <div className="grid gap-3 sm:grid-cols-2">
            {([
              ["profile_id", "执行配置 ID"], ["runtime_type", "Runtime 类型"],
              ["agent_id", "业务 Agent ID"], ["user_id", "执行用户 ID"],
            ] as const).map(([key, label]) => <label key={key} className="text-xs font-semibold">
              {label}<input className={inputClass} value={draft[key]} disabled={saving}
                maxLength={key === "profile_id" ? 160 : undefined}
                onChange={event => changeDraft({ [key]: event.target.value })} />
            </label>)}
          </div>
          <label className="block text-xs font-semibold">全部受影响的 Skill 名称（逗号或换行分隔）
            <textarea className={inputClass} value={draft.affected_skills} disabled={saving}
              onChange={event => changeDraft({ affected_skills: event.target.value })} />
          </label>
          <p className="text-[11px] text-muted-foreground">Skill 名称可留空，登记为 0 Skill 依赖的纯 Prompt／上下文基线，不创建占位 Skill。</p>
          <label className="block text-xs font-semibold">业务 Prompt 槽位
            <select className={inputClass} value={draft.slot} disabled={saving}
              onChange={event => changeDraft({ slot: event.target.value })}>
              <option value="system">system</option><option value="developer">developer</option>
            </select>
          </label>
          <label className="block text-xs font-semibold">当前业务 Agent Prompt
            <textarea className={`${inputClass} min-h-32`} value={draft.prompt} disabled={saving}
              onChange={event => changeDraft({ prompt: event.target.value })} />
          </label>
          {!!placeholders.length && <section aria-label="模板变量默认值" className="space-y-2 rounded-md border border-border p-3">
            <p className="text-xs font-semibold">发现 {placeholders.length} 个模板变量</p>
            <p className="text-[11px] text-muted-foreground">填写 Agent 当前实际使用的默认值；每次请求的实际取值由 Session 上报。规则、示例等可在登记后选为上下文组件；对话历史和当前输入通常保持为请求变量。</p>
            {placeholders.map(name => <label className="block text-xs font-semibold" key={name}>
              {name} 原始默认值
              <textarea className={`${inputClass} min-h-16`} value={variableDefaults[name] ?? ""} disabled={saving || previewing}
                onChange={event => { invalidatePreview(); setVariableDefaults(current => ({ ...current, [name]: event.target.value })); }} />
              {!Object.prototype.hasOwnProperty.call(variableDefaults, name) && <button type="button" className="mt-1 text-[11px] font-normal text-accent underline" onClick={() => {
                invalidatePreview(); setVariableDefaults(current => ({ ...current, [name]: "" }));
              }}>为 {name} 明确使用空值</button>}
            </label>)}
          </section>}
          <p className="text-[11px] text-muted-foreground">用户提示词和对话属于 Agent 的实际请求输入；请随 Session 上报，避免把某次用户提问登记成固定规则。</p>
          <label className="block text-xs font-semibold">当前受管理的业务上下文注入（可选）
            <textarea className={`${inputClass} min-h-24`} value={draft.context} disabled={saving}
              onChange={event => changeDraft({ context: event.target.value })} />
          </label>
          <p className="text-[11px] text-muted-foreground">上下文组件需要关联真实业务 Prompt，才能核对 Agent 是否实际使用。请同时登记当前 Prompt，后续可只选择上下文参与进化。</p>
          <label className="block text-xs font-semibold">业务 Agent 模型（可选）
            <input className={inputClass} value={model} disabled={saving || previewing} placeholder="Agent 实际调用的模型 ID" onChange={event => {
              invalidatePreview(); setModel(event.target.value);
            }} />
          </label>
          <label className="block text-xs font-semibold">冻结执行环境 JSON（可选）
            <textarea className={`${inputClass} min-h-48 font-mono`} value={draft.environment}
              placeholder={environmentExample} disabled={saving}
              onChange={event => changeDraft({ environment: event.target.value })} />
          </label>
          <p className="text-[11px] text-muted-foreground">
            request 填写真实模型、工具与采样配置；template_variables 填写模板变量；validation 锁定验收数据集及运行预算。
            不要填写 messages 或任何凭证。留空仅登记基线，缺少执行配置时不能运行验证。
          </p>
          <details className="text-[11px] text-muted-foreground">
            <summary className="cursor-pointer">查看执行环境 JSON 示例</summary>
            <pre className="mt-1 overflow-auto rounded-md bg-muted p-2">{environmentExample}</pre>
          </details>
          {registrationError && <p role="alert" className="text-xs text-destructive">{registrationError}</p>}
          <Button size="sm" variant="outline" disabled={!canCreate || saving || previewing} onClick={previewRegistration}>
            {previewing ? "预览中…" : "预览业务执行基线"}
          </Button>
          {preview && <section aria-label="业务基线登记预览" className="space-y-2 rounded-md border border-border p-3 text-xs">
            <p>执行配置：{String(preview.raw.profile_id)} · {preview.summary.component_count} 个组件 · {preview.summary.affected_skill_count} 个 Skill 依赖</p>
            <p className="text-muted-foreground">本次仅登记真实基线，后续进化使用接入的 Session。登记后可选择需要参与进化的变量取值。</p>
            <p className="text-muted-foreground">预览不会占用配置 ID；提交时会重新检查名称和依赖是否仍可用。</p>
            <Button size="sm" disabled={saving} onClick={create}>{saving ? "登记中…" : "登记业务执行基线"}</Button>
          </section>}
        </div>
      </details>}
    </div>
  );
}
