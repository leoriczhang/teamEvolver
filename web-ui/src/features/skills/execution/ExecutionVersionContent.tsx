import { useEffect, useState } from "react";
import { executionObjects } from "./variableGroups";

import { InlineDiffView } from "@/components/UnifiedDiffView";
import type { ExecutionPlan } from "@/features/skills/api/client";

function ComponentDiff({ before, after }: { before: string; after: string }) {
  // Long source documents must remain readable without a quadratic LCS allocation.
  if (before.split("\n").length * after.split("\n").length > 1_000_000) {
    return <div className="grid min-w-0 gap-3 md:grid-cols-2">
      {[{ title: "对照版本正文", value: before }, { title: "所选版本正文", value: after }].map(item => <div key={item.title} className="min-w-0">
        <p className="mb-2 text-xs font-medium">{item.title}</p>
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border p-3 text-xs [overflow-wrap:anywhere]">{item.value}</pre>
      </div>)}
    </div>;
  }
  return <InlineDiffView current={before} candidate={after} className="max-h-[48vh]" />;
}

export default function ExecutionVersionContent({ plan, baseline, showDiff }: {
  plan: ExecutionPlan;
  baseline?: ExecutionPlan | null;
  showDiff: boolean;
}) {
  const components = plan.components || [];
  const objects = executionObjects(plan);
  const [selectedId, setSelectedId] = useState(components[0]?.component_id || "");
  useEffect(() => {
    if (!components.some(component => component.component_id === selectedId)) setSelectedId(components[0]?.component_id || "");
  }, [components, selectedId]);
  const component = components.find(item => item.component_id === selectedId);
  const selectedObject = objects.find(object => object.members.some(member => member.component_id === selectedId));
  const before = baseline?.components?.find(item => item.component_id === selectedId);
  if (!component) return <p className="text-sm text-muted-foreground">此发布没有可展示的业务组件。</p>;
  return <section className="min-w-0 space-y-3" aria-label={showDiff ? "版本正文差异" : "版本完整正文"}>
    <label className="block text-xs font-medium">业务组件
      <select value={selectedObject?.id || ""} onChange={event => {
        const object = objects.find(item => item.id === event.target.value);
        if (object) setSelectedId(object.members[0].component_id);
      }}
        className="mt-1 w-full min-w-0 rounded-md border border-border bg-background px-2 py-2 text-xs">
        {objects.map(item => <option key={item.id} value={item.id}>
          {item.variableName ? `变量组 ${item.variableName} · ${item.valueCount} 个取值`
            : `${item.members[0].kind === "agent_prompt" ? "业务 Prompt" : "上下文注入"} · ${item.id} · v${item.members[0].revision}`}
        </option>)}
      </select>
    </label>
    {selectedObject?.variableName && <><label className="block text-xs font-medium">组内取值成员
      <select value={selectedId} onChange={event => setSelectedId(event.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-background px-2 py-2 text-xs">
        {selectedObject.members.map(member => <option key={member.component_id} value={member.component_id}>
          {member.component_id} · v{member.revision} · 原值 {member.binding!.inline_variable!.source_value_hash.slice(0, 12)}
        </option>)}
      </select>
    </label>
      <p className="text-xs text-muted-foreground">变量组随此发布整体冻结；成员正文和版本逐项保留。</p>
    </>}
    <div className="break-all text-xs text-muted-foreground">
      <div>{component.kind === "agent_prompt" ? "业务 Prompt" : "上下文注入"} · {component.slot} · 所选版本 v{component.revision}</div>
      <div className="mt-1">内容哈希：{component.content_hash || "未记录"}</div>
      {component.binding?.inline_variable && <>
        <div className="mt-1">注入位置：{component.binding.inline_variable.component_id} 中的 {`{{${component.binding.inline_variable.name}}}`}</div>
        <div className="mt-1">原值哈希：{component.binding.inline_variable.source_value_hash}</div>
      </>}
      {showDiff && before && <div className="mt-1">对照组件：v{before.revision}{before.content_hash === component.content_hash ? " · 正文未变化" : " · 正文已变化"}</div>}
    </div>
    {!showDiff && <pre className="max-h-[48vh] overflow-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-surface-subtle p-4 text-xs leading-6 [overflow-wrap:anywhere]">{component.content}</pre>}
    {showDiff && (baseline
      ? <ComponentDiff before={before?.content || ""} after={component.content} />
      : <p className="rounded-md bg-muted p-4 text-xs text-muted-foreground">此版本没有冻结的进化基线。可选择另一个历史发布进行正文对比。</p>)}
  </section>;
}
