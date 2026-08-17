import { useCallback, useEffect, useState } from "react";
import { api, type UserProfile } from "@/api/client";
import { Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toastErr, toastOk } from "@/lib/toast";

interface MiningPromptSetting {
  id: string;
  label: string;
  description: string;
  symbol: string;
  default_prompt: string;
  effective_prompt: string;
  overridden: boolean;
  char_count: number;
}

interface MiningWhiteboxSettings {
  model: {
    provider: string;
    model: string;
    base_url: string;
    max_tokens: number;
    context_length: number;
    api_key_present?: boolean;
    inherits_global?: boolean;
  };
  pipeline: {
    max_rounds: number;
    max_retries: number;
    retry_backoff_seconds: number;
    oneshot_timeout_seconds: number;
    step1_validation_retries: number;
    strict_step1: boolean;
    benchmark_target_total: number;
    benchmark_difficulty_dist: string;
    benchmark_max_turns: number;
  };
  prompts: MiningPromptSetting[];
}

export default function MiningWhiteboxPanel({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [settings, setSettings] = useState<MiningWhiteboxSettings | null>(null);
  const [selectedPromptId, setSelectedPromptId] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api<MiningWhiteboxSettings>("/api/mining/settings");
      setSettings(next);
      setSelectedPromptId((current) => (
        next.prompts.some((prompt) => prompt.id === current)
          ? current
          : next.prompts[0]?.id || ""
      ));
    } catch (error: any) {
      toastErr("加载挖掘白盒配置失败", error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) refresh();
  }, [active, refresh]);

  const selectedPrompt = settings?.prompts.find(
    (prompt) => prompt.id === selectedPromptId
  ) || settings?.prompts[0];

  function updatePipeline(
    key: keyof MiningWhiteboxSettings["pipeline"],
    value: number | string | boolean
  ) {
    if (!settings) return;
    setSettings({
      ...settings,
      pipeline: { ...settings.pipeline, [key]: value },
    });
  }

  async function save() {
    if (!settings || !isAdmin) return;
    setSaving(true);
    try {
      const saved = await api<MiningWhiteboxSettings>("/api/mining/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...settings,
          prompts: settings.prompts.map((prompt) => ({
            id: prompt.id,
            prompt: prompt.effective_prompt,
          })),
        }),
      });
      setSettings(saved);
      toastOk("挖掘白盒配置已保存", "新任务将使用更新后的参数与 Prompt");
    } catch (error: any) {
      toastErr("保存挖掘白盒配置失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function resetPrompt(stageId: string) {
    if (!isAdmin) return;
    try {
      const saved = await api<MiningWhiteboxSettings>(
        `/api/mining/prompts/${encodeURIComponent(stageId)}/reset`,
        { method: "POST" }
      );
      setSettings(saved);
      toastOk("已恢复默认 Prompt");
    } catch (error: any) {
      toastErr("恢复 Prompt 失败", error.message);
    }
  }

  return (
    <div className="mt-5 space-y-5">
      <Panel
        title="挖掘与 Benchmark 参数"
        extra={
          <div className="flex items-center gap-2">
            <Pill tone={isAdmin ? "green" : "gray"}>
              {isAdmin ? "管理员可编辑" : "只读"}
            </Pill>
            <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
              {loading ? "刷新中…" : "刷新"}
            </Button>
            <Button size="sm" onClick={save} disabled={!isAdmin || !settings || saving}>
              {saving ? "保存中…" : "保存参数与 Prompt"}
            </Button>
          </div>
        }
      >
        <div className="grid gap-3.5 p-4 md:grid-cols-3">
          <NumberField
            label="反思环最大轮数"
            value={settings?.pipeline.max_rounds ?? 3}
            min={1}
            max={20}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("max_rounds", value)}
          />
          <NumberField
            label="模型瞬时错误重试次数"
            value={settings?.pipeline.max_retries ?? 2}
            min={0}
            max={20}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("max_retries", value)}
          />
          <NumberField
            label="重试退避基数（秒）"
            value={settings?.pipeline.retry_backoff_seconds ?? 0.8}
            min={0}
            step={0.1}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("retry_backoff_seconds", value)}
          />
          <NumberField
            label="单次模型调用超时（秒）"
            value={settings?.pipeline.oneshot_timeout_seconds ?? 1800}
            min={30}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("oneshot_timeout_seconds", value)}
          />
          <NumberField
            label="Step1 校验重跑次数"
            value={settings?.pipeline.step1_validation_retries ?? 1}
            min={0}
            max={10}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("step1_validation_retries", value)}
          />
          <NumberField
            label="Benchmark 目标题量"
            value={settings?.pipeline.benchmark_target_total ?? 16}
            min={1}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("benchmark_target_total", value)}
          />
          <Field label="Benchmark 难度分布">
            <Input
              disabled={!isAdmin || !settings}
              value={settings?.pipeline.benchmark_difficulty_dist || ""}
              onChange={(event) => updatePipeline(
                "benchmark_difficulty_dist",
                event.target.value
              )}
            />
          </Field>
          <NumberField
            label="Benchmark 最大对话轮次"
            value={settings?.pipeline.benchmark_max_turns ?? 5}
            min={1}
            disabled={!isAdmin || !settings}
            onChange={(value) => updatePipeline("benchmark_max_turns", value)}
          />
          <label className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2 text-xs font-semibold">
            Step1 硬伤严格阻断
            <input
              type="checkbox"
              disabled={!isAdmin || !settings}
              checked={settings?.pipeline.strict_step1 ?? true}
              onChange={(event) => updatePipeline("strict_step1", event.target.checked)}
            />
          </label>
        </div>
      </Panel>

      <Panel title="挖掘 Prompt">
        <div className="grid min-h-[520px] lg:grid-cols-[250px_1fr]">
          <div className="border-r border-line p-2">
            {(settings?.prompts || []).map((prompt) => (
              <button
                key={prompt.id}
                type="button"
                onClick={() => setSelectedPromptId(prompt.id)}
                className={`mb-1 flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2.5 text-left text-sm ${
                  selectedPrompt?.id === prompt.id
                    ? "bg-sidebar-accent font-semibold"
                    : "hover:bg-muted"
                }`}
              >
                <span>{prompt.label}</span>
                {prompt.overridden && <Pill tone="amber">已改</Pill>}
              </button>
            ))}
          </div>
          {selectedPrompt ? (
            <div className="space-y-3 p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="font-bold">{selectedPrompt.label}</div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {selectedPrompt.description}
                  </div>
                </div>
                {selectedPrompt.overridden && (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!isAdmin}
                    onClick={() => resetPrompt(selectedPrompt.id)}
                  >
                    恢复默认
                  </Button>
                )}
              </div>
              <textarea
                value={selectedPrompt.effective_prompt}
                disabled={!isAdmin}
                spellCheck={false}
                onChange={(event) => settings && setSettings({
                  ...settings,
                  prompts: settings.prompts.map((prompt) => (
                    prompt.id === selectedPrompt.id
                      ? { ...prompt, effective_prompt: event.target.value }
                      : prompt
                  )),
                })}
                className="mono h-[420px] w-full resize-none overflow-y-auto rounded-lg border border-border bg-background p-3 text-xs leading-relaxed outline-none focus:border-accent"
              />
              <details className="rounded-lg border border-border bg-background/60">
                <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">
                  查看模块默认 Prompt
                </summary>
                <pre className="mono max-h-[360px] overflow-auto whitespace-pre-wrap border-t border-line p-3 text-[11px] leading-relaxed text-muted-foreground">
                  {selectedPrompt.default_prompt}
                </pre>
              </details>
            </div>
          ) : (
            <div className="grid place-items-center text-sm text-muted-foreground">
              {loading ? "正在加载 Prompt…" : "暂无 Prompt 配置"}
            </div>
          )}
        </div>
      </Panel>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">
        {label}
      </Label>
      {children}
    </div>
  );
}

function NumberField({
  label,
  value,
  min,
  max,
  step,
  disabled,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max?: number;
  step?: number;
  disabled: boolean;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label}>
      <Input
        type="number"
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </Field>
  );
}
