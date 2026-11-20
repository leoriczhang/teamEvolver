import type { MAConnection } from "./api/client";

export default function ReplayResources({ connection }: { connection?: MAConnection }) {
  const resources = connection?.detail.replay_resources;
  const environment = resources?.environment_id || connection?.config.replay_environment_id;
  const vaults = resources?.vault_ids || connection?.config.replay_vault_ids || [];
  const labels: Record<string, string> = {
    ready: "隔离资源已就绪", credentials_missing: "隔离资源已准备，凭证待补充",
    preparing: "正在准备隔离资源", unsupported: "需要处理运行环境",
  };
  return <div className="space-y-2 rounded-lg border bg-muted/30 p-3 text-xs">
    <p className="font-medium">{labels[resources?.status || ""] || "保存后自动准备隔离资源"}</p>
    <p className="text-muted-foreground">系统自动获取或创建独立的回放环境和 Vault，并复用已有资源，无需填写 ID。</p>
    {environment && <p className="break-all text-muted-foreground">回放环境：<span className="font-mono">{environment}</span></p>}
    {!!vaults.length && <p className="break-all text-muted-foreground">回放 Vault：<span className="font-mono">{vaults.join(", ")}</span></p>}
    {!!resources?.missing_credentials?.length && <p className="text-amber-700">
      需要向回放 Vault 补充：{resources.missing_credentials.join("、")}。已有密钥无法从 MA 读出，补充后点击“发现／重新检查”。
    </p>}
    {resources?.reason && <p className="text-amber-700">{resources.reason}</p>}
  </div>;
}
