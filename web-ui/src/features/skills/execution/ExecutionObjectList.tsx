import type { ReactNode } from "react";
import type { ExecutionComponent, ExecutionPlan } from "@/features/skills/api/client";
import { executionObjects } from "./variableGroups";

export default function ExecutionObjectList({ plan, children }: {
  plan: ExecutionPlan;
  children: (component: ExecutionComponent) => ReactNode;
}) {
  return <>{executionObjects(plan).map(object => object.variableName
    ? <details key={object.id} className="min-w-0 space-y-2 rounded-lg border border-border p-3"
      aria-label={`变量组 ${object.variableName}`}>
      <summary className="cursor-pointer break-all text-xs font-semibold">
        变量组 {object.variableName} · {object.valueCount} 个取值 · {object.members.length} 个注入成员
      </summary>
      <p className="text-[11px] text-muted-foreground">同一变量共同演化，按原始取值选择成员；展开审阅或修改具体取值。组版本随发布冻结，成员保留各自版本。</p>
      <div className="space-y-2">{object.members.map(children)}</div>
    </details>
    : children(object.members[0]))}</>;
}
