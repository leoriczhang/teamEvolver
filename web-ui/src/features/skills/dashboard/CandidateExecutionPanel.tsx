import { useEffect, useState } from "react";
import ExecutionObjectList from "@/features/skills/execution/ExecutionObjectList";

import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { InlineDiffView } from "@/components/UnifiedDiffView";
import { isExecutionCandidate,type Candidate,type ExecutionComponent,type ExecutionCostGate,type ExecutionGate,type ExecutionProbeGate } from "@/features/skills/api/client";
import { moduleNames } from "@/features/skills/EvolutionModulePicker";
import { toastErr, toastOk } from "@/lib/toast";
import IndependentValidationPanel from "./IndependentValidationPanel";
import { isStandardValidation } from "@/features/skills/api/client";
import CandidateValidationPanel from "./CandidateValidationPanel";

const control = "mt-1 w-full rounded-md border border-border bg-background px-2 py-1.5 text-xs";
const gateReasons: Record<string, string> = {
  execution_not_observed: "尚未核验实际执行内容与候选组合一致。",
  regression_coverage_missing: "共享资产影响的 Skill 尚未全部覆盖回归。",
  independent_validation_missing: "缺少当前候选版本的独立外部复核通过声明。",
};

type ExecutionEvaluation = {
  execution_gate?: ExecutionGate;
  publication_gate?: ExecutionGate;
  cost_gate?: ExecutionCostGate;
  replay?: {
    execution_gate?: ExecutionGate; publication_gate?: ExecutionGate;
    probe_gate?: ExecutionProbeGate; cost_gate?: ExecutionCostGate;
  };
};

export default function CandidateExecutionPanel({ candidate, readOnly, onUpdated, onEditingChange }: {
  candidate: Candidate;
  readOnly: boolean;
  onUpdated: (detail: Candidate) => void;
  onEditingChange?: (editing: boolean) => void;
}) {
  const [editingId, setEditingId] = useState("");
  const [content, setContent] = useState("");
  const [reason, setReason] = useState("");
  const [refs, setRefs] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const baseline = candidate.baseline_execution_plan;
  const proposed = candidate.candidate_execution_plan;
  const changes = candidate.component_changes || [];
  const canEdit = !readOnly && candidate.review_status === "open" && !!candidate.candidate_revision
    && !!candidate.allowed_modules?.includes("prompt");
  const editableIds = (baseline?.environment?.evolution as { editable_component_ids?: unknown } | undefined)?.editable_component_ids;
  const inScope = (id: string) => editableIds === undefined || (Array.isArray(editableIds) && editableIds.includes(id));
  const editingInScope = !editingId || inScope(editingId);
  const sourceRefs = [...new Set([...(candidate.session_ids || []), ...changes.flatMap(change => change.evidence_refs || [])])];
  const evaluation = candidate.evaluation as ExecutionEvaluation | undefined;
  const executionGate = evaluation?.execution_gate || evaluation?.replay?.execution_gate;
  const publicationGate = candidate.publication_gate || evaluation?.publication_gate || evaluation?.replay?.publication_gate;
  const caseIds = [...new Set(candidate.current_case_ids || candidate.evaluation?.replay?.datasets
    ?.filter(dataset => dataset.selection === "current").flatMap(dataset => dataset.inputs.map(item => item.case_id)) || [])];

  useEffect(() => {
    onEditingChange?.(Boolean(editingId));
    return () => onEditingChange?.(false);
  }, [editingId, onEditingChange]);
  useEffect(() => { setEditingId(""); }, [candidate.candidate_revision, candidate.job_id]);
  useEffect(() => { if (!canEdit || !editingInScope) setEditingId(""); }, [canEdit, editingInScope]);
  if (!baseline || !proposed) return null;
  const oldById = new Map(baseline.components.map(component => [component.component_id, component]));

  function startEdit(component: ExecutionComponent) {
    if (!canEdit || !inScope(component.component_id)) return;
    const change = changes.find(item => item.component_id === component.component_id);
    setEditingId(component.component_id);
    setContent(component.content);
    setReason(change?.reason || "");
    setRefs(change?.evidence_refs?.length ? change.evidence_refs : sourceRefs);
  }

  async function save() {
    if (!canEdit || saving || !editingId || !inScope(editingId) || !content.trim() || !reason.trim() || !refs.length) return;
    const edited = { component_id: editingId, content, reason: reason.trim(), evidence_refs: refs };
    // The endpoint rebuilds from the frozen baseline. Keep other components' edits.
    const componentChanges = [...changes.filter(change => change.component_id !== editingId), edited];
    setSaving(true);
    try {
      const detail = await api<Candidate>(
        `/api/skill-candidates/${encodeURIComponent(candidate.job_id)}/execution-content`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ candidate_revision: candidate.candidate_revision, component_changes: componentChanges }),
        },
      );
      setEditingId("");
      onUpdated(detail);
      toastOk("业务候选内容已保存", "候选版本已更新，原评估需重新确认，额外复核按验证策略要求");
    } catch (cause: any) {
      toastErr("保存业务候选失败", cause.message);
    } finally {
      setSaving(false);
    }
  }

  return <section aria-label="业务 Prompt 与上下文候选" className="min-w-0 w-full space-y-3 rounded-lg border border-border p-3">
    <h3 className="text-sm font-semibold">业务 Prompt／上下文变更与组合</h3>
    <p className="text-[11px] text-muted-foreground">此处展示业务 Agent 的实际候选文本，与 TE 生成器、裁判 Prompt 分开。</p>
    <div className="space-y-1 break-all text-xs text-muted-foreground">
      {isExecutionCandidate(candidate) && <div className="font-semibold text-foreground">Prompt-only · 纯业务 Prompt／上下文候选</div>}
      <div>允许修改：{moduleNames(candidate.allowed_modules || ["skill"])}</div>
      <div>实际修改：{moduleNames(candidate.actual_modules)}</div>
      <div>执行配置：{candidate.execution_profile_id || proposed.binding?.profile_id || "未记录"}</div>
      {candidate.base_release_id && <div>冻结基线发布：{candidate.base_release_id}</div>}
      <div>候选版本：{candidate.candidate_revision ?? "未记录"}</div>
      {isExecutionCandidate(candidate) ? <>
        <div>Skill 依赖：0（不适用 Skill 版本）</div>
        <div>此候选没有 Skill 包；仅为实际改变的业务组件及执行组合记录版本。</div>
      </> : candidate.skill_dependencies && <div>
        Skill 依赖：{candidate.skill_dependencies.length} · {candidate.skill_dependencies.map(item => `${item.name} v${item.version}`).join("、")}
      </div>}
      {candidate.decision?.release_id && <div>已发布组合：{String(candidate.decision.release_id)}</div>}
      {candidate.decision?.composition_hash && <div>已发布组合哈希：{String(candidate.decision.composition_hash)}</div>}
      <div>实际加载验证：{executionGate?.status || "未记录"}</div>
      <div>加载验证须核对最终模型请求；Agent 下载／激活自报不能代替该证据。</div>
      {isExecutionCandidate(candidate) && (candidate.evaluation?.static_validation || candidate.static_validation) && <div>
        业务组合静态检查：{(candidate.evaluation?.static_validation || candidate.static_validation)?.passed ? "通过" : "失败"}
      </div>}
      {executionGate?.reason && <div>{executionGate.reason}</div>}
      {(publicationGate?.reasons || []).map(item => <div key={item}>{gateReasons[item] || item}</div>)}
    </div>
    <ExecutionObjectList plan={proposed}>{component => {
      const old = oldById.get(component.component_id);
      const change = changes.find(item => item.component_id === component.component_id);
      const editing = editingId === component.component_id;
      const editable = inScope(component.component_id);
      return <div key={component.component_id} className="space-y-2 rounded-lg border border-border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs font-semibold">
          <span className="min-w-0 flex-1 break-all">{component.component_id} · {component.kind === "agent_prompt" ? "业务 Prompt" : "上下文注入"} · {component.slot}</span>
          {!editable && <span className="shrink-0 font-normal text-muted-foreground">本轮只读</span>}
          {canEdit && editable && !editingId && <Button size="sm" variant="outline" className="shrink-0"
            aria-label={`编辑 ${component.component_id}`} onClick={() => startEdit(component)}>
            编辑
          </Button>}
        </div>
        <div className="break-all text-[11px] text-muted-foreground">
          基线 v{old?.revision ?? "?"} · {old?.content_hash || "未记录"}<br />
          候选 v{component.revision} · {component.content_hash}
          {component.binding?.inline_variable && <>
            <div className="mt-1">注入位置：{component.binding.inline_variable.component_id} 中的 {`{{${component.binding.inline_variable.name}}}`}</div>
            <div className="mt-1">原值哈希：{component.binding.inline_variable.source_value_hash}</div>
          </>}
        </div>
        {change && <div className="text-xs text-muted-foreground">修改原因：{change.reason}</div>}
        {!editing ? <ComponentDiff baseline={old?.content || ""} candidate={component.content} /> : <div className="space-y-3">
          <label className="block text-xs font-semibold">候选业务文本
            <textarea className={`${control} min-h-48 font-mono`} value={content} disabled={saving}
              onChange={event => setContent(event.target.value)} />
          </label>
          <label className="block text-xs font-semibold">业务修改原因
            <textarea className={control} value={reason} disabled={saving}
              onChange={event => setReason(event.target.value)} />
          </label>
          <fieldset className="space-y-2" disabled={saving}>
            <legend className="text-xs font-semibold">原始 Session／Trace 证据引用</legend>
            {sourceRefs.map(ref => <label key={ref} className="flex items-center gap-2 break-all text-xs">
              <input type="checkbox" checked={refs.includes(ref)} onChange={event => setRefs(current => (
                event.target.checked ? [...current, ref] : current.filter(value => value !== ref)
              ))} />{ref}
            </label>)}
            {!sourceRefs.length && <p role="alert" className="text-xs text-destructive">详情没有来源引用，不能提交无证据修改。</p>}
          </fieldset>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!canEdit || !editable || saving || !content.trim() || !reason.trim() || !refs.length
              || content === component.content} onClick={save}>{saving ? "保存中…" : "保存业务候选"}</Button>
            <Button size="sm" variant="outline" disabled={saving} onClick={() => setEditingId("")}>取消业务编辑</Button>
          </div>
        </div>}
      </div>;
    }}</ExecutionObjectList>
    <CandidateValidationPanel plan={candidate.validation_plan} probe={evaluation?.replay?.probe_gate}
      cost={evaluation?.replay?.cost_gate || evaluation?.cost_gate} />
    {!isStandardValidation(candidate.validation_plan) && <IndependentValidationPanel
      key={`${candidate.job_id}:${candidate.candidate_revision}`} candidate={candidate}
      readOnly={readOnly || Boolean(editingId)} caseIds={caseIds} onUpdated={onUpdated} />}
  </section>;
}

function ComponentDiff({ baseline, candidate }: { baseline: string; candidate: string }) {
  // Avoid quadratic LCS allocation for long documents while retaining exact text.
  if (baseline.split("\n").length * candidate.split("\n").length > 1_000_000) {
    return <div className="grid gap-3 md:grid-cols-2">
      <div><div className="text-xs font-semibold">基线完整文本</div><pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs">{baseline}</pre></div>
      <div><div className="text-xs font-semibold">候选完整文本</div><pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs">{candidate}</pre></div>
    </div>;
  }
  return <InlineDiffView current={baseline} candidate={candidate} className="max-h-72" />;
}
