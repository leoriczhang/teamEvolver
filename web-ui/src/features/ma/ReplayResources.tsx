import type { MAConnection } from "./api/client";

export default function ReplayResources({ connection }: { connection?: MAConnection }) {
  const resources = connection?.detail?.replay_resources;
  const environment = resources?.environment_id || connection?.config.environment_id;
  const vaults = resources?.vault_ids || connection?.config.vault_ids || [];
  const labels: Record<string, string> = {
    ready: "来源配置已就绪", credentials_missing: "请重新发现来源配置",
    preparing: "正在读取来源配置", unsupported: "需要处理运行环境",
  };
  return <div className="space-y-2 rounded-lg border bg-muted/30 p-3 text-xs">
    <p className="font-medium">{labels[resources?.status || ""] || "保存后自动读取来源配置"}</p>
    <p className="text-muted-foreground">回放固定来源配置和 Vault 引用，在两个独立 Session 中执行相同任务，仅替换目标 Skill。</p>
    {environment && <p className="break-all text-muted-foreground">回放环境：<span className="font-mono">{environment}</span></p>}
    {!!vaults.length && <p className="break-all text-muted-foreground">回放 Vault：<span className="font-mono">{vaults.join(", ")}</span></p>}
    {resources?.reason && <p className="text-amber-700">{resources.reason}</p>}
  </div>;
}
