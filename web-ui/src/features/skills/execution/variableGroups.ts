import type { ExecutionComponent, ExecutionPlan } from "@/features/skills/api/client";

export interface ExecutionObject {
  id: string;
  variableName?: string;
  valueCount: number;
  members: ExecutionComponent[];
}

/** A variable is one object; original value/host components remain its members. */
export function executionObjects(plan: ExecutionPlan): ExecutionObject[] {
  const objects: ExecutionObject[] = [];
  const groups = new Map<string, ExecutionObject>();
  for (const component of plan.components || []) {
    const inline = component.binding?.inline_variable;
    if (!inline) {
      objects.push({ id: component.component_id, members: [component], valueCount: 0 });
      continue;
    }
    const id = `variable-group:${plan.binding?.profile_id || ""}:${inline.name}`;
    let group = groups.get(id);
    if (!group) {
      group = { id, variableName: inline.name, valueCount: 0, members: [] };
      groups.set(id, group); objects.push(group);
    }
    group.members.push(component);
    group.valueCount = new Set(group.members.map(member => member.binding!.inline_variable!.source_value_hash)).size;
  }
  return objects;
}
