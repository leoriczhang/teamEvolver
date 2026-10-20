import { api } from "@/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { Dot,Panel,Pill,StatCard } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { type UserProfile } from "@/features/admin/api/client";
import { type EvolveModelSettings,type EvolveModelTestResp,type EvolveProcessSettings,type PromptSummary } from "@/features/skills/api/client";
import { toastErr,toastOk } from "@/lib/toast";
import { useCallback,useEffect,useRef,useState,type ReactNode } from "react";

const PRESET_MODELS = [
  { id: "gpt-4o-mini", label: "GPT-4o mini" },
  { id: "gpt-4o", label: "GPT-4o" },
  { id: "gpt-4.1-mini", label: "GPT-4.1 mini" },
];

const emptySettings = (): EvolveModelSettings => ({
  base_url: "",
  model: "",
  max_tokens: 0,
  temperature: 0.4,
  api_key: "",
});

const emptyProcessSettings = (): EvolveProcessSettings => ({ evolve: {}, validation: {} });

interface StageDraft {
  model: string;
  base_url: string;
  api_key: string;
  clear_api_key: boolean;
  temperature: number;
  max_tokens: number;
}

function stageDraftFromSummary(s: PromptSummary): StageDraft {
  return {
    model: s.model || "",
    base_url: s.base_url || "",
    api_key: "",
    clear_api_key: false,
    temperature: s.temperature ?? 0.4,
    max_tokens: s.max_tokens ?? 16384,
  };
}

export default function ModelSettingsView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [settings, setSettings] = useState<EvolveModelSettings>(() => emptySettings());
  const [processSettings, setProcessSettings] = useState<EvolveProcessSettings>(
    () => emptyProcessSettings()
  );
  const [stages, setStages] = useState<PromptSummary[]>([]);
  const [stageDrafts, setStageDrafts] = useState<Record<string, StageDraft>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<EvolveModelTestResp | null>(null);
  const [clearKey, setClearKey] = useState(false);
  const loaded = useRef(false);
  const isAdmin = user?.role === "admin";
  const isTenantScope = settings.scope === "tenant";
  const scopeLabel = isTenantScope ? `租户 ${settings.tenant_id || ""}` : "系统默认";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [data, process, promptList] = await Promise.all([
        api<EvolveModelSettings>("/api/model-settings"),
        api<EvolveProcessSettings>("/api/skill-evolution/settings"),
        api<{ prompts: PromptSummary[] }>("/api/skill-evolution/prompts"),
      ]);
      setSettings({ ...data, api_key: "" });
      setProcessSettings(process);
      setStages(promptList.prompts || []);
      const drafts: Record<string, StageDraft> = {};
      for (const s of promptList.prompts || []) {
        drafts[s.id] = stageDraftFromSummary(s);
      }
      setStageDrafts(drafts);
      setClearKey(false);
      setTestResult(null);
    } catch (e: any) {
      toastErr("加载模型配置失败", e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  async function saveDefaultModel() {
    const payload: EvolveModelSettings = {
      ...settings,
      model: settings.model.trim(),
      base_url: settings.base_url.trim(),
      max_tokens: Number(settings.max_tokens),
      temperature: Number(settings.temperature ?? 0.4),
      clear_api_key: clearKey,
    };
    const saved = await api<EvolveModelSettings>("/api/model-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setSettings({ ...saved, api_key: "" });
    setClearKey(false);
  }

  async function saveStageModel(stageId: string) {
    const draft = stageDrafts[stageId];
    if (!draft) return;
    const payload = {
        model: draft.model || "",
        base_url: draft.base_url || "",
        api_key: draft.api_key || "",
        clear_api_key: draft.clear_api_key,
        temperature: draft.temperature,
        max_tokens: draft.max_tokens,
    };
    const result = await api<PromptSummary>(
      `/api/skill-evolution/prompts/${stageId}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    setStageDrafts((prev) => ({
      ...prev,
      [stageId]: {
        ...stageDraftFromSummary(result),
        api_key: "",
        clear_api_key: false,
      },
    }));
    setStages((prev) =>
      prev.map((s) => (s.id === stageId ? { ...s, ...result } : s))
    );
  }

  async function save() {
    if (!isAdmin) return;
    setSaving(true);
    try {
      await saveDefaultModel();
      // Save all stage overrides that have been modified
      const stageIds = Object.keys(stageDrafts);
      for (const stageId of stageIds) {
        await saveStageModel(stageId);
      }
      toastOk(
        isTenantScope ? "租户模型配置已保存" : "模型配置已保存",
        `默认模型 + ${stageIds.length} 个阶段均已持久化`,
      );
    } catch (e: any) {
      toastErr("保存模型配置失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  async function testModel() {
    if (!isAdmin) return;
    setTesting(true);
    setTestResult(null);
    try {
      const payload: EvolveModelSettings = {
        ...settings,
        model: settings.model.trim(),
        base_url: settings.base_url.trim(),
        max_tokens: Number(settings.max_tokens),
        temperature: Number(settings.temperature ?? 0.4),
        clear_api_key: clearKey,
      };
      const result = await runSkillCommand<EvolveModelTestResp>("/api/model-settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setTestResult(result);
      toastOk("模型连通性正常", `${result.latency_ms ?? "-"} ms · ${result.response || ""}`);
    } catch (e: any) {
      toastErr("模型测试失败", e.message);
    } finally {
      setTesting(false);
    }
  }

  function updateStageDraft(stageId: string, patch: Partial<StageDraft>) {
    setStageDrafts((prev) => ({
      ...prev,
      [stageId]: { ...prev[stageId], ...patch },
    }));
  }

  return (
    <div className="mx-auto max-w-[1080px] px-[22px] py-[22px]">
      <div className="content-toolbar">
        <div>
          <div className="flex items-center gap-2 text-[12px] font-[700] text-[#464c5e]">
            <Dot state={settings.api_key_present ? "on" : "off"} />
            {settings.api_key_present ? "默认模型凭据已配置" : "默认模型凭据尚未配置"}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            当前作用域：{scopeLabel}。页面包含所有 LLM 环节的模型配置，阶段留空继承默认模型，统一持久化保存。
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>刷新</Button>
          <Button variant="outline" size="sm" onClick={testModel} disabled={!isAdmin || testing}>
            {testing ? "测试中…" : "测试默认模型"}
          </Button>
          <Button size="sm" onClick={save} disabled={!isAdmin || saving}>
            {saving ? "保存中…" : "保存全部"}
          </Button>
        </div>
      </div>

      <div className="mb-[18px] grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5">
        <StatCard label="配置作用域" value={scopeLabel} mono={isTenantScope} />
        <StatCard label="默认模型" value={settings.model || "未配置"} />
        <StatCard label="Base URL" value={settings.base_url || "未配置"} mono />
        <StatCard label="默认 Key" value={settings.api_key_present ? "已配置" : "未配置"} />
        <StatCard label="编辑权限" value={isAdmin ? "管理员" : "只读"} />
      </div>

      {/* ── Section 1: 默认模型 ── */}
      <Panel
        title={`${isTenantScope ? "租户默认模型" : "系统默认模型"}（所有 LLM 环节的基线配置）`}
        extra={
          <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
            <Dot state={settings.api_key_present ? "on" : "off"} />
            <Pill tone={settings.api_key_present ? "green" : "gray"}>
              {settings.api_key_present ? "Key 已配置" : "Key 未配置"}
            </Pill>
          </span>
        }
      >
        <div className="space-y-5 p-4">
          {!isAdmin && (
            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
              当前账号不是管理员，只能查看模型配置。
            </div>
          )}

          <Field label="模型选择 *">
            <Input
              disabled={!isAdmin}
              list="evolve-model-presets"
              value={settings.model || ""}
              placeholder="请输入模型 ID"
              onChange={(e) => setSettings({ ...settings, model: e.target.value })}
            />
            <datalist id="evolve-model-presets">
              {PRESET_MODELS.map((m) => (
                <option key={m.id} value={m.id}>{m.label}</option>
              ))}
            </datalist>
          </Field>

          <Field label="Base URL *">
            <Input
              disabled={!isAdmin}
              value={settings.base_url || ""}
              placeholder="https://example.com/v1"
              onChange={(e) => setSettings({ ...settings, base_url: e.target.value })}
            />
          </Field>
          <div className="text-xs text-muted-foreground -mt-2">
            填写 OpenAI-compatible API 的 Base URL。
          </div>

          <Field label={`API Key${settings.api_key_present ? "（已配置，留空保留）" : ""}`}>
            <Input
              disabled={!isAdmin || clearKey}
              type="password"
              value={settings.api_key || ""}
              placeholder={settings.api_key_present ? "输入新值可替换现有 key" : "请输入模型 API Key（Bearer Token）"}
              onChange={(e) => setSettings({ ...settings, api_key: e.target.value })}
            />
          </Field>
          <div className="text-xs text-muted-foreground -mt-2">
            即 curl 命令中 <code className="text-foreground">Authorization: Bearer &lt;token&gt;</code> 的 <code className="text-foreground">&lt;token&gt;</code> 部分。
          </div>

          <div className="grid gap-3.5 md:grid-cols-2">
            <Field label="最大输出 Token">
              <Input
                disabled={!isAdmin}
                type="number"
                min="1"
                value={settings.max_tokens || ""}
                onChange={(e) => setSettings({ ...settings, max_tokens: Number(e.target.value) })}
              />
            </Field>
            <Field label="Temperature">
              <Input
                disabled={!isAdmin}
                type="number"
                min="0"
                max="2"
                step="0.1"
                value={settings.temperature ?? 0.4}
                onChange={(e) => setSettings({ ...settings, temperature: Number(e.target.value) })}
              />
            </Field>
          </div>

          {isAdmin && (
            <label className="flex items-center gap-2 text-xs text-muted-foreground">
              <input
                type="checkbox"
                checked={clearKey}
                onChange={(e) => {
                  setClearKey(e.target.checked);
                  if (e.target.checked) setSettings({ ...settings, api_key: "" });
                }}
              />
              清空已保存的 API Key
            </label>
          )}

          {testResult && (
            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed">
              <div className="mb-1 font-semibold text-success">模型测试通过</div>
              <div className="text-muted-foreground">
                {testResult.model} · {testResult.latency_ms ?? "-"} ms · 返回：{testResult.response || "（空）"}
              </div>
            </div>
          )}

          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
            此模型用于所有 LLM 环节的基线配置。下方每个阶段可独立覆盖模型、Base URL、API Key、Temperature、Max Tokens；留空的字段继承此默认模型。
          </div>
        </div>
      </Panel>

      {/* ── Section 2: 进化流水线各阶段模型配置 ── */}
      <Panel
        title="进化流水线 — 各阶段模型配置（7 个 LLM 阶段）"
        extra={
          <span className="text-xs text-muted-foreground">
            留空继承默认模型
          </span>
        }
      >
        <div className="space-y-3 p-4">
          <div className="rounded-lg border border-blue-200 bg-blue-50/50 p-3 text-xs leading-relaxed text-blue-900">
            每个阶段可独立配置模型、Base URL、API Key、Temperature 和 Max Tokens。留空的字段继承上方默认模型配置。点击「保存全部」一次性持久化所有阶段配置（包括 API Key）。
          </div>

          {stages.map((stage) => {
            const draft = stageDrafts[stage.id];
            if (!draft) return null;
            const hasOverride = stage.settings_overridden;
            const hasKey = stage.api_key_present;
            return (
              <div
                key={stage.id}
                className="rounded-lg border border-border bg-background/60 p-4"
              >
                <div className="mb-3 flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <Dot state={hasOverride ? "on" : "off"} />
                    <span className="text-sm font-semibold text-foreground">{stage.label}</span>
                    {hasOverride && (
                      <Pill tone="green">已覆盖</Pill>
                    )}
                    {hasKey && (
                      <Pill tone="blue">独立 Key</Pill>
                    )}
                  </div>
                  <span className="text-xs text-muted-foreground">
                    默认 T={stage.temperature ?? 0.4} · {stage.max_tokens ?? 16384} tokens
                  </span>
                </div>

                {stage.description && (
                  <div className="mb-3 text-xs text-muted-foreground">{stage.description}</div>
                )}

                <div className="grid gap-3 md:grid-cols-2">
                  <Field label="模型（留空继承默认）">
                    <Input
                      disabled={!isAdmin}
                      list="evolve-model-presets"
                      value={draft.model}
                      placeholder={settings.model || "继承默认模型"}
                      onChange={(e) => updateStageDraft(stage.id, { model: e.target.value })}
                    />
                  </Field>
                  <Field label="Base URL（留空继承默认）">
                    <Input
                      disabled={!isAdmin}
                      value={draft.base_url}
                      placeholder={settings.base_url || "继承默认 Base URL"}
                      onChange={(e) => updateStageDraft(stage.id, { base_url: e.target.value })}
                    />
                  </Field>
                </div>

                <Field label={`API Key${hasKey ? "（已配置，留空保留）" : "（留空继承默认）"}`}>
                  <Input
                    disabled={!isAdmin || draft.clear_api_key}
                    type="password"
                    value={draft.api_key}
                    placeholder={hasKey ? "输入新值可替换" : "留空则继承默认模型 Key"}
                    onChange={(e) => updateStageDraft(stage.id, { api_key: e.target.value })}
                  />
                </Field>

                <div className="grid gap-3 md:grid-cols-2">
                  <Field label="Temperature">
                    <Input
                      disabled={!isAdmin}
                      type="number"
                      min="0"
                      max="2"
                      step="0.1"
                      value={draft.temperature}
                      onChange={(e) => updateStageDraft(stage.id, { temperature: Number(e.target.value) })}
                    />
                  </Field>
                  <Field label="Max Tokens">
                    <Input
                      disabled={!isAdmin}
                      type="number"
                      min="1"
                      max="131072"
                      value={draft.max_tokens}
                      onChange={(e) => updateStageDraft(stage.id, { max_tokens: Number(e.target.value) })}
                    />
                  </Field>
                </div>

                {isAdmin && (
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    <input
                      type="checkbox"
                      checked={draft.clear_api_key}
                      onChange={(e) => {
                        updateStageDraft(stage.id, {
                          clear_api_key: e.target.checked,
                          api_key: e.target.checked ? "" : draft.api_key,
                        });
                      }}
                    />
                    清空此阶段的独立 API Key（恢复继承默认）
                  </label>
                )}
              </div>
            );
          })}
        </div>
      </Panel>

      <div className="mt-5 rounded-xl border border-border bg-surface p-4 text-xs leading-6 text-muted-foreground">
        本页面统一管理所有 LLM 环节的模型配置。进化流水线的 7 个阶段（会话分析、Trace 分析、改进技能、新建技能、冲突合并、测试集生成、真回放裁判）均可独立覆盖模型和凭据。点击「保存全部」一次性持久化所有配置（包括 API Key）到 PostgreSQL。
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}
