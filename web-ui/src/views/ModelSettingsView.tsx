import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Panel, StatCard, Pill, Dot } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  api,
  type EvolveModelSettings,
  type EvolveModelTestResp,
  type EvolveProcessSettings,
  type UserProfile,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";

const emptySettings = (): EvolveModelSettings => ({
  provider: "custom",
  base_url: "",
  model: "",
  max_tokens: 100000,
  temperature: 0.4,
  api_key: "",
});

const emptyProcessSettings = (): EvolveProcessSettings => ({
  evolve: {
    use_session_judge: true,
    publish_mode: "validated",
    validation_max_rejections: 1,
    human_review_enabled: true,
    human_review_timeout_seconds: 86400,
    interval_seconds: 600,
    evidence_enabled: true,
    evidence_max_entries: 400,
    evidence_recent_limit: 20,
    evidence_historical_limit: 20,
    evidence_replay_cases_per_window: 1,
    evidence_change_debt_threshold: 3,
    dataset_synthesis_enabled: true,
    dataset_test_cases: 2,
    dataset_min_requirements: 12,
    dataset_max_requirements: 24,
    dataset_disclosure_batch_size: 4,
    candidate_coalesce_enabled: true,
    bundle_text_extensions: [".py", ".sh"],
    bundle_max_file_bytes: 262144,
    bundle_max_prompt_bytes: 786432,
    bundle_allow_delete: true,
    bundle_static_checks_enabled: true,
  },
  validation: {
    enabled: true,
    mode: "true_replay",
    idle_after_seconds: 300,
    poll_interval_seconds: 60,
    max_jobs_per_day: 5,
    max_concurrency: 1,
    required_results: 3,
    required_approvals: 2,
  },
  memory_maintenance: {
    enabled: false,
    auto_start: false,
    model: "",
    base_url: "",
    api_key: "",
    engine: "teamEvolver-native",
    full_capabilities: true,
    llm_max_tokens: 4096,
    temperature: 0.3,
    scheduler: {
      active_start_hour: 0,
      active_end_hour: 6,
      rounds_per_window: 3,
      round_interval_minutes: 90,
      max_turns_per_job: 25,
      max_consecutive_errors: 3,
      retry_delay_seconds: 300,
    },
    jobs: [],
    interval_seconds: 86400,
    max_source_items: 100,
    max_source_chars: 120000,
    prompts: {
      extract: "",
      consolidate: "",
    },
  },
});

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
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<EvolveModelTestResp | null>(null);
  const [clearKey, setClearKey] = useState(false);
  const loaded = useRef(false);
  const isAdmin = user?.role === "admin";

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [data, process] = await Promise.all([
        api<EvolveModelSettings>("/api/evolve-model"),
        api<EvolveProcessSettings>("/api/evolve-settings"),
      ]);
      setSettings({ ...data, api_key: "" });
      setProcessSettings({
        ...process,
        memory_maintenance: {
          ...process.memory_maintenance,
          api_key: "",
        },
      });
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

  async function save() {
    if (!isAdmin) return;
    setSaving(true);
    try {
      const payload: EvolveModelSettings = {
        ...settings,
        max_tokens: Number(settings.max_tokens || 100000),
        temperature: Number(settings.temperature ?? 0.4),
        clear_api_key: clearKey,
      };
      const saved = await api<EvolveModelSettings>("/api/evolve-model", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setSettings({ ...saved, api_key: "" });
      setClearKey(false);
      toastOk("全局模型已保存", saved.model);
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
        max_tokens: Number(settings.max_tokens || 100000),
        temperature: Number(settings.temperature ?? 0.4),
        clear_api_key: clearKey,
      };
      const result = await api<EvolveModelTestResp>("/api/evolve-model/test", {
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

  return (
    <div className="mx-auto max-w-[1080px] px-[22px] py-[22px]">
      <div className="content-toolbar">
        <div>
          <div className="flex items-center gap-2 text-[12px] font-[700] text-[#464c5e]">
            <Dot state={settings.api_key_present ? "on" : "off"} />
            {settings.api_key_present ? "模型凭据已配置" : "模型凭据尚未配置"}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            配置全局模型默认值。阶段 Prompt、模型采样和过程参数在“进化链路”中对应维护。
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>刷新</Button>
          <Button variant="outline" size="sm" onClick={testModel} disabled={!isAdmin || testing}>
            {testing ? "测试中…" : "测试模型"}
          </Button>
          <Button size="sm" onClick={save} disabled={!isAdmin || saving}>
            保存全部
          </Button>
        </div>
      </div>

      <div className="mb-[18px] grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5">
        <StatCard label="当前模型" value={settings.model || "未配置"} />
        <StatCard label="Base URL" value={settings.base_url || "未配置"} mono />
        <StatCard label="API Key" value={settings.api_key_present ? "已配置" : "未配置"} />
        <StatCard label="编辑权限" value={isAdmin ? "管理员" : "只读"} />
      </div>

      {!!Object.keys(processSettings.environment_overrides || {}).length && (
        <div className="mb-5 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs leading-relaxed text-amber-900">
          <div className="font-semibold">检测到服务端环境变量覆盖</div>
          <div className="mt-1">
            {Object.entries(processSettings.environment_overrides || {})
              .map(([key, value]) => `${key}=${value}`)
              .join(" · ")}
          </div>
          <div className="mt-1">这些值优先于页面持久化配置；调整部署环境后重启服务才能解除覆盖。</div>
        </div>
      )}

      <Panel
        title="进化模型"
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

          <div className="grid gap-3.5 md:grid-cols-2">
            <Field label="Provider">
              <Input
                disabled={!isAdmin}
                value={settings.provider || "custom"}
                placeholder="custom"
                onChange={(e) => setSettings({ ...settings, provider: e.target.value })}
              />
            </Field>
            <Field label="模型名 *">
              <Input
                disabled={!isAdmin}
                value={settings.model || ""}
                placeholder="doubao-seed-evolving"
                onChange={(e) => setSettings({ ...settings, model: e.target.value })}
              />
            </Field>
          </div>

          <Field label="Base URL *">
            <Input
              disabled={!isAdmin}
              value={settings.base_url || ""}
              placeholder="https://ark.cn-beijing.volces.com/api/v3"
              onChange={(e) => setSettings({ ...settings, base_url: e.target.value })}
            />
          </Field>

          <Field label={`API Key${settings.api_key_present ? "（已配置，留空保留）" : ""}`}>
            <Input
              disabled={!isAdmin || clearKey}
              type="password"
              value={settings.api_key || ""}
              placeholder={settings.api_key_present ? "输入新值可替换现有 key" : "请输入模型 API Key"}
              onChange={(e) => setSettings({ ...settings, api_key: e.target.value })}
            />
          </Field>

          <div className="grid gap-3.5 md:grid-cols-2">
            <Field label="最大输出 Token">
              <Input
                disabled={!isAdmin}
                type="number"
                value={settings.max_tokens || 100000}
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
            这些参数只影响 teamEvolver 52010 服务自身的技能进化流程；不会配置 Hermes，也不会暴露本机路径或明文 Key。
          </div>
        </div>
      </Panel>

      <div className="mt-5 rounded-xl border border-border bg-surface p-4 text-xs leading-6 text-muted-foreground">
        阶段专属 Prompt、模型采样和过程参数已统一收敛到“进化链路”；此页只维护全局模型默认值。
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
