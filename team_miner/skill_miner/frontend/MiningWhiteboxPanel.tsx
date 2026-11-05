import { miningApi as api } from "./miningApi";
import { Panel,Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { UserProfile } from "@/features/admin/api/client";
import { toastErr,toastOk } from "@/lib/toast";
import { useCallback,useEffect,useState } from "react";

interface MiningPromptSetting {
  id: "ov_compile";
  label: string;
  description: string;
  default_prompt: string;
  effective_prompt: string;
  overridden: boolean;
  char_count: number;
}

interface MiningWhiteboxSettings {
  revision: number;
  engine: "ov_compile";
  model: { configuration_source: "openviking_server"; editable_here: false };
  pipeline: { max_rounds: number; runtime_timeout_seconds: number };
  prompts: MiningPromptSetting[];
}

export default function MiningWhiteboxPanel({ active, user }: { active: boolean; user?: UserProfile | null }) {
  const [settings, setSettings] = useState<MiningWhiteboxSettings | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const isAdmin = user?.tenant_role === "tenant_admin" || user?.platform_role === "platform_admin";
  const prompt = settings?.prompts.find((item) => item.id === "ov_compile");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setSettings(await api<MiningWhiteboxSettings>("/api/mining/settings"));
    } catch (error: any) {
      toastErr("加载挖掘配置失败", error.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (active) void refresh();
  }, [active, refresh]);

  function updatePipeline(key: keyof MiningWhiteboxSettings["pipeline"], value: number) {
    setSettings((current) => current && { ...current, pipeline: { ...current.pipeline, [key]: value } });
  }

  async function save() {
    if (!settings || !prompt || !isAdmin) return;
    setSaving(true);
    try {
      setSettings(await api<MiningWhiteboxSettings>("/api/mining/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: settings.revision,
          pipeline: {
            max_rounds: settings.pipeline.max_rounds,
            runtime_timeout_seconds: settings.pipeline.runtime_timeout_seconds,
          },
          prompts: [{ id: "ov_compile", prompt: prompt.effective_prompt }],
        }),
      }));
      toastOk("挖掘配置已保存", "新任务将使用更新后的默认参数与编译指令。");
    } catch (error: any) {
      toastErr("保存挖掘配置失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  async function resetPrompt() {
    if (!isAdmin || !settings) return;
    setSaving(true);
    try {
      setSettings(await api<MiningWhiteboxSettings>("/api/mining/prompts/ov_compile/reset", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: settings.revision }),
      }));
      toastOk("已恢复默认编译指令");
    } catch (error: any) {
      toastErr("恢复编译指令失败", error.message);
    } finally {
      setSaving(false);
    }
  }

  const disabled = !isAdmin || !settings || saving;
  return (
    <div className="mt-5 space-y-5">
      <Panel title="OpenViking 挖掘配置" extra={
        <div className="flex items-center gap-2">
          <Pill tone={isAdmin ? "green" : "gray"}>{isAdmin ? "管理员可编辑" : "只读"}</Pill>
          <Button variant="outline" size="sm" onClick={() => void refresh()} disabled={loading || saving}>
            {loading ? "刷新中…" : "刷新"}
          </Button>
          <Button size="sm" onClick={save} disabled={disabled || !prompt}>
            {saving ? "保存中…" : "保存参数与编译指令"}
          </Button>
        </div>
      }>
        <div className="space-y-4 p-4">
          <p className="text-xs leading-relaxed text-muted-foreground">
            编译使用 OpenViking 服务端的模型配置。以下默认参数用于新任务，可在创建任务时调整；人工补证等待不计入单轮期限。
          </p>
          <div className="grid gap-3.5 md:grid-cols-2">
            <NumberField label="默认挖掘轮数上限" value={settings?.pipeline.max_rounds ?? 3} min={1} max={5}
              disabled={disabled} onChange={(value) => updatePipeline("max_rounds", value)} />
            <NumberField label="默认单轮运行期限（秒）" value={settings?.pipeline.runtime_timeout_seconds ?? 3600} min={60} max={7200}
              disabled={disabled} onChange={(value) => updatePipeline("runtime_timeout_seconds", value)} />
          </div>
        </div>
      </Panel>
      <Panel title="Skill 挖掘编译指令">
        {prompt ? (
          <div className="space-y-3 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="font-bold">{prompt.label}</div>
                <p className="mt-1 text-xs text-muted-foreground">{prompt.description}</p>
                <p className="mt-1 text-xs text-muted-foreground">每轮受理前冻结编译指令；已受理的轮次沿用冻结版本。</p>
                <p className="mt-1 text-xs text-muted-foreground">产物语言按首轮原始来源冻结，中文主导用简体中文，后续轮不随旧产物或补证语言改变。自定义业务指令原样保存；语言规则加入新的 Compile 请求，不改写已受理请求或已完成产物。</p>
              </div>
              {prompt.overridden && <Button variant="outline" size="sm" disabled={disabled} onClick={resetPrompt}>恢复默认</Button>}
            </div>
            <textarea aria-label="Skill 挖掘编译指令" value={prompt.effective_prompt} disabled={disabled} spellCheck={false}
              onChange={(event) => setSettings((current) => current && {
                ...current,
                prompts: current.prompts.map((item) => item.id === "ov_compile" ? { ...item, effective_prompt: event.target.value } : item),
              })}
              className="mono h-[420px] w-full resize-y overflow-y-auto rounded-lg border border-border bg-background p-3 text-xs leading-relaxed outline-none focus:border-accent" />
            <div className="text-[11px] text-muted-foreground">{prompt.effective_prompt.length} 字符</div>
            <details className="rounded-lg border border-border bg-background/60">
              <summary className="cursor-pointer px-3 py-2 text-xs font-semibold">查看默认编译指令</summary>
              <pre className="mono max-h-[360px] overflow-auto whitespace-pre-wrap border-t border-line p-3 text-[11px] leading-relaxed text-muted-foreground">{prompt.default_prompt}</pre>
            </details>
          </div>
        ) : <div className="p-8 text-center text-sm text-muted-foreground">{loading ? "正在加载编译指令…" : "暂无编译指令配置"}</div>}
      </Panel>
    </div>
  );
}

function NumberField({ label, value, min, max, disabled, onChange }: {
  label: string; value: number; min: number; max: number; disabled: boolean; onChange: (value: number) => void;
}) {
  return <label className="block">
    <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
    <Input type="number" min={min} max={max} step={1} disabled={disabled} value={value}
      onChange={(event) => onChange(Number(event.target.value))} />
  </label>;
}
