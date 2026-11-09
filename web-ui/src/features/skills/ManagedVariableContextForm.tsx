import { useEffect, useRef, useState } from "react";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import type {
  ExecutionProfile,
  VariableContextEvolutionScope,
  VariableContextRegistrationPreview,
  VariableContextRegistrationRequest,
} from "@/features/skills/api/client";
import { toastOk } from "@/lib/toast";
import ExecutionObjectList from "./execution/ExecutionObjectList";

const inputClass = "mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs";

function variableNames(profile: ExecutionProfile): string[] {
  const environment = profile.execution_plan.environment || profile.environment || {};
  const contract = environment.template_input_contract as { schema_version?: unknown; variable_names?: unknown } | undefined;
  if (contract?.schema_version !== "teamevolver.template-input-contract.v1" || !Array.isArray(contract.variable_names)) return [];
  return [...new Set(contract.variable_names.filter((name): name is string => typeof name === "string"))];
}

function parseSamples(text: string, names: string[], selected: string[]): Array<Record<string, unknown>> {
  let rows: unknown;
  try { rows = JSON.parse(text); }
  catch { throw new Error("变量样本必须是有效的 JSON 数组"); }
  if (!Array.isArray(rows) || rows.length === 0 || rows.length > 200) {
    throw new Error("请提供 1–200 条变量样本，每条样本为一个 JSON 对象");
  }
  const allowed = new Set(names);
  return rows.map((value, index) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(`第 ${index + 1} 条样本必须是 JSON 对象`);
    }
    const row = value as Record<string, unknown>;
    for (const name of Object.keys(row)) {
      if (!allowed.has(name)) throw new Error(`第 ${index + 1} 条样本包含未声明的变量：${name}`);
      const item = row[name];
      if (!["string", "number", "boolean"].includes(typeof item) || (typeof item === "number" && !Number.isFinite(item))) {
        throw new Error(`第 ${index + 1} 条样本的 ${name} 必须是文本、数字或布尔值`);
      }
    }
    for (const name of selected) {
      if (!Object.prototype.hasOwnProperty.call(row, name)) throw new Error(`第 ${index + 1} 条样本缺少所选变量：${name}`);
      if (typeof row[name] !== "string") throw new Error(`第 ${index + 1} 条样本的 ${name} 必须是文本`);
    }
    return row;
  });
}

export default function ManagedVariableContextForm({ source, onRegistered, onCancel }: {
  source: ExecutionProfile;
  onRegistered: (profile: ExecutionProfile) => void | Promise<void>;
  onCancel: () => void;
}) {
  const names = variableNames(source);
  const [profileId, setProfileId] = useState("");
  const [samples, setSamples] = useState("");
  const [inputMode, setInputMode] = useState<"variable_values" | "variable_samples">("variable_values");
  const [groupedValues, setGroupedValues] = useState<Record<string, string[]>>({});
  const [selected, setSelected] = useState<string[]>([]);
  const [scope, setScope] = useState<VariableContextEvolutionScope>("context");
  const [preview, setPreview] = useState<VariableContextRegistrationPreview | null>(null);
  const [request, setRequest] = useState<VariableContextRegistrationRequest | null>(null);
  const [error, setError] = useState("");
  const [previewing, setPreviewing] = useState(false);
  const [registering, setRegistering] = useState(false);
  const sequence = useRef(0);
  const endpoint = `/api/skill-evolution/execution-profiles/${encodeURIComponent(source.profile_id)}/variable-contexts`;
  const busy = previewing || registering;

  useEffect(() => {
    sequence.current++;
    setProfileId(""); setSamples(""); setSelected([]); setScope("context"); setInputMode("variable_values"); setGroupedValues({});
    setPreview(null); setRequest(null); setError(""); setPreviewing(false); setRegistering(false);
    return () => { sequence.current++; };
  }, [source.profile_id, source.current_release_id]);

  function invalidate() {
    sequence.current++;
    setPreview(null);
    setRequest(null);
    setError("");
    setPreviewing(false);
  }

  async function buildPreview() {
    let body: VariableContextRegistrationRequest;
    try {
      const id = profileId.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9:._-]{0,199}$/.test(id)) throw new Error("请填写有效的新执行配置 ID（字母、数字、点、冒号、下划线或短横线）");
      if (id === source.profile_id) throw new Error("请使用新的执行配置 ID，来源配置保留原版本");
      if (!selected.length) throw new Error("请至少选择一个需要管理的变量");
      body = {
        profile_id: id, source_release_id: source.current_release_id,
        managed_variables: names.filter(name => selected.includes(name)),
        ...(inputMode === "variable_samples" ? { variable_samples: parseSamples(samples, names, selected) } : {
          variable_values: Object.fromEntries(selected.map(name => {
            const values = groupedValues[name] || [""];
            if (!values.length || values.length > 200) throw new Error(`${name} 需要 1–200 个原始取值`);
            return [name, values];
          })),
        }), evolution_scope: scope,
      };
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "请检查变量样本");
      return;
    }
    const current = ++sequence.current;
    setPreviewing(true);
    setError("");
    setPreview(null);
    setRequest(null);
    try {
      const result = await api<VariableContextRegistrationPreview>(`${endpoint}/preview`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (sequence.current !== current) return;
      if (result.profile_id !== body.profile_id || result.source_profile_id !== source.profile_id
        || result.source_release_id !== body.source_release_id || !result.preview_hash) {
        throw new Error("预览与当前来源配置不一致，请重新预览");
      }
      if (result.summary.component_count > 32) throw new Error("新组合超过 32 个组件，请减少管理的变量或不同变量值");
      setPreview(result);
      setRequest(body);
    } catch (cause) {
      if (sequence.current === current) setError(cause instanceof Error ? cause.message : "变量上下文预览失败");
    } finally {
      if (sequence.current === current) setPreviewing(false);
    }
  }

  async function register() {
    if (!preview || !request || busy) return;
    const current = ++sequence.current;
    setRegistering(true);
    setError("");
    try {
      const result = await api<ExecutionProfile>(endpoint, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request),
      });
      if (sequence.current !== current) return;
      if (result.profile_id !== request.profile_id) throw new Error("登记返回的配置 ID 不一致，请刷新配置列表核对");
      toastOk("变量上下文基线已登记", "可在版本与指标中查看各项上下文资产");
      await onRegistered(result);
    } catch (cause) {
      if (sequence.current === current) {
        const status = (cause as { status?: number })?.status;
        setError(status === 409 ? "来源发布或目标配置已发生变化，请刷新配置后重新预览"
          : cause instanceof Error ? cause.message : "登记变量上下文失败");
        setPreview(null);
        setRequest(null);
      }
    } finally {
      if (sequence.current === current) setRegistering(false);
    }
  }

  async function importFile(file?: File) {
    if (!file) return;
    invalidate();
    try {
      if (file.size > 1_048_576) throw new Error("变量文件不能超过 1 MiB");
      const content = await file.text();
      const value: unknown = JSON.parse(content);
      if (Array.isArray(value)) {
        setSamples(content); setInputMode("variable_samples");
      } else if (value && typeof value === "object") {
        const entries = Object.entries(value);
        if (!entries.length || entries.some(([name, values]) => !names.includes(name) || !Array.isArray(values)
          || !values.length || values.length > 200 || values.some(item => typeof item !== "string"))) {
          throw new Error("变量组文件应为 {变量名: [原始文本取值]}，仅包含此配置声明的变量");
        }
        setGroupedValues(Object.fromEntries(entries) as Record<string, string[]>);
        setSelected(entries.map(([name]) => name)); setInputMode("variable_values");
      } else throw new Error("请导入变量组 JSON 对象或样本 JSON 数组");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "读取变量文件失败"); }
  }

  const assets = preview?.components.filter(component => component.binding?.inline_variable) || [];
  return <section aria-label="登记变量上下文基线" className="space-y-3 rounded-lg border border-border p-3">
    <div className="space-y-1 text-xs text-muted-foreground">
      <p className="font-semibold text-foreground">来源配置：{source.profile_id} · 发布：{source.current_release_id}</p>
      <p>Agent：{source.agent_id} · Runtime：{source.runtime_type}</p>
      <p>新基线沿用此配置的执行环境与 Prompt 模板。变量资产会在原来的模板变量位置注入。</p>
      <p>样本用于登记变量值；进化仍使用接入的真实会话及反馈。</p>
    </div>
    <label className="block text-xs font-semibold">新执行配置 ID
      <input className={inputClass} value={profileId} disabled={busy} maxLength={200}
        onChange={event => { invalidate(); setProfileId(event.target.value); }} />
    </label>
    <label className="block text-xs font-semibold">变量值提供方式
      <select className={inputClass} value={inputMode} disabled={busy} onChange={event => {
        invalidate(); setInputMode(event.target.value as typeof inputMode);
      }}>
        <option value="variable_values">按变量分别填写取值（推荐）</option>
        <option value="variable_samples">已有变量样本 JSON 数组</option>
      </select>
    </label>
    <label className="block text-xs font-semibold">导入变量 JSON 文件（可选）
      <input className={inputClass} type="file" accept=".json,application/json" disabled={busy} onChange={event => {
        void importFile(event.target.files?.[0]); event.target.value = "";
      }} />
    </label>
    {inputMode === "variable_samples" && <>
      <label className="block text-xs font-semibold">变量样本 JSON 数组
        <textarea className={`${inputClass} min-h-36 font-mono`} value={samples} disabled={busy}
          placeholder={JSON.stringify([{ [names[0] || "variableName"]: "原始变量内容" }], null, 2)}
          onChange={event => { invalidate(); setSamples(event.target.value); }} />
      </label>
      <p className="text-[11px] text-muted-foreground">每条样本须包含所有已选变量，可带此配置声明的其他变量。相同取值会自动去重，空值继续使用原输入。</p>
    </>}
    <fieldset className="space-y-2" disabled={busy}>
      <legend className="text-xs font-semibold">需要管理的变量（可多选）</legend>
      {!names.length && <p role="alert" className="text-xs text-muted-foreground">此配置尚未声明模板变量，请选择已配置变量的执行基线。</p>}
      <div className="flex flex-wrap gap-3">{names.map(name => <label key={name} className="flex items-center gap-1.5 text-xs">
        <input type="checkbox" checked={selected.includes(name)} onChange={event => {
          invalidate(); setSelected(current => event.target.checked ? [...current, name] : current.filter(item => item !== name));
        }} />{name}
      </label>)}</div>
    </fieldset>
    {inputMode === "variable_values" && <div className="space-y-3">
      <p className="text-[11px] text-muted-foreground">每个变量独立登记它可以使用的规则、示例或参考内容，不需要列出所有变量组合。不要选择每次变化的用户输入或对话历史。</p>
      {names.filter(name => selected.includes(name)).map(name => <fieldset className="space-y-2 rounded-md border border-border p-3" key={name} disabled={busy}>
        <legend className="px-1 text-xs font-semibold">变量组 {name}</legend>
        {(groupedValues[name] || [""]).map((value, index) => <div key={index} className="space-y-1">
          <label className="block text-xs font-semibold">{name} 取值 {index + 1}
            <textarea className={`${inputClass} min-h-20`} value={value} onChange={event => {
              invalidate(); const values = [...(groupedValues[name] || [""])]; values[index] = event.target.value;
              setGroupedValues(current => ({ ...current, [name]: values }));
            }} />
          </label>
          {(groupedValues[name] || [""]).length > 1 && <Button size="sm" variant="outline" onClick={() => {
            invalidate(); setGroupedValues(current => ({ ...current, [name]: current[name].filter((_, item) => item !== index) }));
          }}>删除 {name} 取值 {index + 1}</Button>}
        </div>)}
        <Button size="sm" variant="outline" disabled={(groupedValues[name] || [""]).length >= 200} onClick={() => {
          invalidate(); setGroupedValues(current => ({ ...current, [name]: [...(current[name] || [""]), ""] }));
        }}>为 {name} 添加取值</Button>
      </fieldset>)}
    </div>}
    <fieldset className="space-y-2 text-xs" disabled={busy}>
      <legend className="font-semibold">参与进化的内容</legend>
      {([
        ["context", "仅上下文资产"], ["prompt_and_context", "Prompt 与上下文资产"],
      ] as const).map(([value, label]) => <label key={value} className="mr-4 inline-flex items-center gap-1.5">
        <input type="radio" name={`variable-context-scope-${source.profile_id}`} value={value} checked={scope === value}
          onChange={() => { invalidate(); setScope(value); }} />{label}
      </label>)}
    </fieldset>
    {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" disabled={busy || !names.length} onClick={buildPreview}>
        {previewing ? "预览中…" : "预览变量上下文"}
      </Button>
      <Button size="sm" variant="outline" disabled={registering} onClick={onCancel}>取消</Button>
    </div>
    {preview && <section aria-label="变量上下文预览" className="space-y-3 border-t border-border pt-3 text-xs">
      <p>{preview.summary.sample_count} {preview.summary.input_mode === "variable_values" ? "个变量取值" : "条样本"} · 新组合共 {preview.summary.component_count} 个组件</p>
      <p className="text-muted-foreground">下方统计对应本次提交的变量取值；来源配置已有的上下文成员继续保留，展开后的新组合也包含这些已有成员。</p>
      <div className="space-y-1 text-muted-foreground">{preview.summary.managed_variables.map(item => <p key={item.name}>
        {item.name}：1 个变量组，{item.unique_values} 个不同值，{item.blank_samples} 条空值样本，登记 {item.component_ids.length} 个组内注入成员
      </p>)}</div>
      <ExecutionObjectList plan={{ binding: { profile_id: preview.profile_id }, components: assets.map(component => ({
        ...component, content_hash: component.content_hash || "",
      })) }}>{component => {
        const inline = component.binding!.inline_variable!;
        return <details key={component.component_id} className="rounded-md bg-muted p-2">
          <summary className="cursor-pointer break-all font-semibold">
            {inline.name} · {component.component_id} · v{component.revision}
          </summary>
          <div className="mt-2 break-all text-muted-foreground">注入位置：{inline.component_id} 中的 {`{{${inline.name}}}`}</div>
          <div className="mt-1 break-all text-muted-foreground">原值哈希：{inline.source_value_hash}</div>
          <pre className="mt-2 max-h-60 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2 font-mono">{component.content}</pre>
        </details>;
      }}</ExecutionObjectList>
      {!!preview.summary.managed_variables.some(item => item.blank_samples > 0) && <p className="text-muted-foreground">空值与仅含空白的样本保留原输入，不生成上下文资产。</p>}
      <Button size="sm" disabled={busy} onClick={register}>{registering ? "登记中…" : "确认登记新基线"}</Button>
    </section>}
  </section>;
}
