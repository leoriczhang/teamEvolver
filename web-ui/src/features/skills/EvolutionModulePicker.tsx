import type { EvolutionModule } from "@/features/skills/api/client";

const MODULES: Array<{ id: EvolutionModule; label: string }> = [
  { id: "skill", label: "Skill" },
  { id: "prompt", label: "业务 Agent Prompt／上下文注入" },
];

export function evolutionModules(value: unknown): EvolutionModule[] {
  if (value === undefined) return ["skill"];
  if (!Array.isArray(value)) return [];
  return MODULES.map(module => module.id).filter(module => value.includes(module));
}

export function moduleNames(value: string[] | undefined): string {
  if (!value) return "未记录";
  if (!value.length) return "无修改";
  return value.map(id => MODULES.find(module => module.id === id)?.label || id).join("、");
}

export default function EvolutionModulePicker({
  legend, value, onChange, disabled = false,
}: {
  legend: string;
  value: EvolutionModule[];
  onChange: (value: EvolutionModule[]) => void;
  disabled?: boolean;
}) {
  return (
    <fieldset className="space-y-2" disabled={disabled}>
      <legend className="mb-2 text-xs font-semibold">{legend}</legend>
      <div className="flex flex-wrap gap-4">
        {MODULES.map(module => (
          <label key={module.id} className="flex items-center gap-2 text-xs">
            <input
              type="checkbox"
              checked={value.includes(module.id)}
              onChange={event => onChange(MODULES.map(item => item.id).filter(id => (
                id === module.id ? event.target.checked : value.includes(id)
              )))}
            />
            {module.label}
          </label>
        ))}
      </div>
      {!value.length && <p role="alert" className="text-xs text-destructive">请至少选择一个可修改模块。</p>}
    </fieldset>
  );
}
