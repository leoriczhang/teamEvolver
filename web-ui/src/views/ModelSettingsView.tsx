import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Panel, StatCard, Pill, Dot } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, type EvolveModelSettings, type EvolveModelTestResp, type UserProfile } from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";

const emptySettings = (): EvolveModelSettings => ({
  provider: "custom",
  base_url: "",
  model: "",
  max_tokens: 100000,
  temperature: 0.4,
  api_key: "",
});

export default function ModelSettingsView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [settings, setSettings] = useState<EvolveModelSettings>(() => emptySettings());
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
      const data = await api<EvolveModelSettings>("/api/evolve-model");
      setSettings({ ...data, api_key: "" });
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
      toastOk("模型配置已保存", saved.model);
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
    <div className="mx-auto max-w-[1080px] px-7 py-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-[22px] font-bold tracking-tight">模型配置</h1>
          <div className="mt-1 text-xs text-muted-foreground">
            配置 teamEvolver 在总结、判断、合并和生成技能时使用的模型。外部 Hermes 会话仍通过服务端接口上传，不在前端手工提交。
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>刷新</Button>
          <Button variant="outline" size="sm" onClick={testModel} disabled={!isAdmin || testing}>
            {testing ? "测试中…" : "测试模型"}
          </Button>
          <Button size="sm" onClick={save} disabled={!isAdmin || saving}>
            保存配置
          </Button>
        </div>
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5">
        <StatCard label="当前模型" value={settings.model || "未配置"} />
        <StatCard label="Base URL" value={settings.base_url || "未配置"} mono />
        <StatCard label="API Key" value={settings.api_key_present ? "已配置" : "未配置"} />
        <StatCard label="编辑权限" value={isAdmin ? "管理员" : "只读"} />
      </div>

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
