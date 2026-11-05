import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useState } from "react";
import { saveConnection, type MAConnection, type MAJob } from "./api/client";

export default function ConnectionForm({ connection, onClose, onSaved }: {
  connection?: MAConnection; onClose: () => void; onSaved: (job?: MAJob) => void;
}) {
  const [draft, setDraft] = useState({
    display_name: connection?.config.display_name || "", entry_agent_id: connection?.config.entry_agent_id || "",
    environment_id: connection?.config.environment_id || "", default_user_id: connection?.config.default_user_id || "",
    ma_base_url: connection?.config.ma_base_url || "https://ark.cn-beijing.volces.com/api/v3",
    team_evolver_url: connection?.config.team_evolver_url || "http://127.0.0.1:52010",
    ma_api_key: "", tenant_token: "", vault_ids: (connection?.config.vault_ids || []).join(","),
    replay_enabled: connection?.config.replay_enabled || false,
    replay_environment_id: connection?.config.replay_environment_id || "",
    replay_vault_ids: (connection?.config.replay_vault_ids || []).join(","),
    poll_interval_seconds: connection?.config.poll_interval_seconds || 60,
    sync_interval_seconds: connection?.config.sync_interval_seconds || 30,
    user_tag_prefix: connection?.config.user_tag_prefix || "te-user:",
    runtime_binding: JSON.stringify(connection?.config.runtime_binding || {}, null, 2),
    replay_isolation: JSON.stringify(connection?.config.replay_isolation || {}, null, 2),
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const list = (text: string) => text.split(",").map(v => v.trim()).filter(Boolean);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(""); setBusy(true);
    try {
      if (!connection && (!draft.ma_api_key.trim() || !draft.tenant_token.trim())) {
        throw new Error("请填写 MA Key 和当前租户 Key");
      }
      if (draft.replay_enabled && !draft.replay_environment_id.trim()) throw new Error("请指定回放环境");
      const result = await saveConnection({
        ...draft, expected_revision: connection?.revision || 0,
        vault_ids: list(draft.vault_ids), replay_vault_ids: list(draft.replay_vault_ids),
        runtime_binding: JSON.parse(draft.runtime_binding), replay_isolation: JSON.parse(draft.replay_isolation),
      }, connection?.connection_id);
      setDraft(value => ({ ...value, ma_api_key: "", tenant_token: "" }));
      onSaved("job_id" in result ? result : undefined);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  const field = (key: keyof typeof draft, label: string, type = "text", required = true) => (
    <label className="block text-xs font-medium" key={key}>{label}
      <Input className="mt-1" type={type} required={required} value={String(draft[key])}
        autoComplete={type === "password" ? "new-password" : "off"}
        disabled={!!connection && ["entry_agent_id", "ma_base_url"].includes(key)}
        onChange={e => setDraft(v => ({ ...v, [key]: e.target.value }))} />
    </label>
  );
  return <Dialog open onOpenChange={open => !open && onClose()}>
    <DialogContent className="max-h-[90vh] overflow-auto sm:max-w-[760px]">
      <DialogHeader><DialogTitle>{connection ? "编辑 MA 连接" : "接入 Managed Agent"}</DialogTitle></DialogHeader>
      <form onSubmit={submit} className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          {field("display_name", "连接名称")}{field("default_user_id", "默认用户 ID")}
          {field("entry_agent_id", "入口 Agent ID")}{field("environment_id", "业务 Environment ID")}
          {field("ma_api_key", connection ? "MA Key（留空保留）" : "MA Key", "password", !connection)}
          {field("tenant_token", connection ? "租户 Key（留空保留）" : "当前租户 Key（tevt_）", "password", !connection)}
        </div>
        <p className="text-xs text-muted-foreground">凭证保存后仅显示配置状态。已有会话保持其固定版本，新会话使用完整应用的发布版本。</p>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={draft.replay_enabled}
            onChange={e => setDraft(v => ({ ...v, replay_enabled: e.target.checked }))} />启用隔离 MA True Replay
        </label>
        {draft.replay_enabled && <div className="grid gap-3 sm:grid-cols-2">
          {field("replay_environment_id", "回放 Environment ID")}
          {field("replay_vault_ids", "回放 Vault ID（逗号分隔）", "text", false)}
        </div>}
        <details className="rounded-lg border p-3">
          <summary className="cursor-pointer text-sm font-semibold">连接、归属与运行时配置</summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {field("ma_base_url", "MA API 地址")}{field("team_evolver_url", "本系统 pull 地址")}
            {field("vault_ids", "业务 Vault ID（逗号分隔）", "text", false)}
            {field("user_tag_prefix", "用户标签前缀")}
            {(["poll_interval_seconds", "sync_interval_seconds"] as const).map((key, index) =>
              <label key={key} className="text-xs">{index ? "同步间隔（秒）" : "采集间隔（秒）"}
                <Input type="number" min={15} max={86400} value={draft[key]} className="mt-1"
                  onChange={e => setDraft(v => ({ ...v, [key]: Number(e.target.value) }))} /></label>)}
          </div>
          {(["runtime_binding", "replay_isolation"] as const).map(key =>
            <label key={key} className="mt-3 block text-xs">{key === "runtime_binding" ? "运行时绑定 JSON" : "回放隔离能力 JSON"}
              <Textarea className="mt-1 font-mono text-xs" rows={4} value={draft[key]}
                onChange={e => setDraft(v => ({ ...v, [key]: e.target.value }))} /></label>)}
        </details>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
        <div className="flex justify-end gap-2"><Button type="button" variant="outline" onClick={onClose}>取消</Button>
          <Button disabled={busy} type="submit">{busy ? "保存中…" : connection ? "保存配置" : "保存并发现资源"}</Button></div>
      </form>
    </DialogContent>
  </Dialog>;
}
