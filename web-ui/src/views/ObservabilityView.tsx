import { useEffect, useState } from "react";
import { PlugZap, Save } from "lucide-react";
import { api, type LangfuseTracingConfig, type UserProfile } from "@/api/client";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Pill } from "@/components/common";
import { toastErr, toastOk } from "@/lib/toast";

export default function ObservabilityView({ active, user }: { active: boolean; user?: UserProfile | null }) {
  const [form, setForm] = useState<LangfuseTracingConfig>({ enabled: false, host: "", sample_rate: 1, capture_content: true });
  const [busy, setBusy] = useState("");
  useEffect(() => {
    if (active) api<LangfuseTracingConfig>("/api/langfuse-tracing-config").then(setForm).catch(e => toastErr("读取观测配置失败", e.message));
  }, [active]);
  async function run(test: boolean) {
    setBusy(test ? "test" : "save");
    try {
      const payload = test ? { host: form.host, public_key: form.public_key, secret_key: form.secret_key } : {
        enabled: form.enabled, host: form.host, public_key: form.public_key, secret_key: form.secret_key,
        environment: form.environment, release: form.release, sample_rate: form.sample_rate,
        capture_content: form.capture_content,
      };
      const result = await api<any>(`/api/langfuse-tracing-config${test ? "/test" : ""}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      if (test && result.ok === false) throw new Error(result.error || "连接失败");
      if (!test) setForm(result);
      toastOk(test ? "观测连接正常" : "全局观测配置已保存");
    } catch (e: any) { toastErr("观测配置操作失败", e.message); } finally { setBusy(""); }
  }
  const disabled = user?.role !== "admin" || !!busy;
  return <div className="mx-auto max-w-[1000px] space-y-5 p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="text-base font-semibold">全局链路观测 Langfuse</h2>
      <Pill tone={form.status?.initialized ? "green" : "gray"}>{form.status?.initialized ? "已初始化" : "未初始化"}</Pill></div>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={disabled} checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} />上报进化与团队 Memory 链路</label>
    <div className="grid gap-4 sm:grid-cols-2">
      {(["host", "public_key", "secret_key", "environment", "release"] as const).map(key => <label key={key} className="min-w-0 text-xs">
        {{ host: "观测 Host", public_key: "Public Key", secret_key: "Secret Key", environment: "Environment", release: "Release" }[key]}
        {(key === "secret_key" && form.secret_key_present) || (key === "public_key" && form.public_key_present) ? "（已配置，留空保留）" : ""}
        <Input className="mt-2" type={key.includes("key") ? "password" : "text"} disabled={disabled} value={form[key] || ""} onChange={e => setForm({ ...form, [key]: e.target.value })} />
      </label>)}
      <label className="text-xs">采样率<Input className="mt-2" type="number" min={0} max={1} step={0.05} disabled={disabled} value={form.sample_rate ?? 1} onChange={e => setForm({ ...form, sample_rate: Number(e.target.value) })} /></label>
    </div>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" disabled={disabled} checked={!!form.capture_content} onChange={e => setForm({ ...form, capture_content: e.target.checked })} />采集模型输入与输出</label>
    <div className="flex flex-wrap gap-2"><Button disabled={disabled} onClick={() => run(false)}><Save className="size-4" />保存全局观测配置</Button>
      <Button variant="outline" disabled={disabled} onClick={() => run(true)}><PlugZap className="size-4" />测试观测连接</Button></div>
  </div>;
}
