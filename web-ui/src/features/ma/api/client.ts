import { api, tenantHeaders } from "@/api/client";

export type MAConfig = {
  display_name: string; entry_agent_id: string; environment_id: string; default_user_id: string;
  ma_base_url: string; team_evolver_url: string; user_tag_prefix: string;
  poll_interval_seconds: number; sync_interval_seconds: number;
  vault_ids: string[]; replay_enabled: boolean; replay_environment_id: string; replay_vault_ids: string[];
  replay_isolation?: Record<string, boolean>; runtime_binding: Record<string, unknown>;
  ma_credential_configured: boolean; tenant_credential_configured: boolean; enabled?: boolean;
};
export type MAConnection = {
  connection_id: string; revision: number; enabled: boolean; status: string; config: MAConfig;
  detail: { error?: string; agent_count?: number; managed_skill_count?: number;
    replay_capability?: { supported: boolean; reason?: string }; waiting?: string[] };
  last_collected_at?: string; last_synced_at?: string;
  sessions?: { session_id: string; record: { business_time?: string; last_pulled_at?: string;
    user_id?: string; status?: string; job_id?: string } }[];
};
export type MAResource = {
  kind: "agent" | "skill"; logical_name: string; revision: number;
  record: { id?: string; name?: string; version?: string; source_version?: string; team_version?: number;
    tree_sha256?: string; agents?: string[]; status?: string; drift?: string };
};
export type MARelease = {
  release_id: string; status: string; created_at: string; applied_at?: string;
  record: { entry_agent_version?: string; environment_id?: string; manifest_hash?: string;
    runtime_tree_sha256?: string; error?: string; drift?: string };
};
export type MATarget = { status: string; accepted: boolean; connection_revision?: number; binding_hash?: string };
export type MAJob = { job_id: string; status_url: string; connection?: MAConnection };

const root = "/api/ma/connections";
const path = (id: string) => `${root}/${encodeURIComponent(id)}`;
export const maConnections = () => api<{ connections: MAConnection[]; next_after?: string }>(root);
export const maConnection = (id: string) => api<MAConnection>(path(id));
export async function maResources(id: string) {
  const resources: MAResource[] = [];
  for (const kind of ["agent", "skill"]) {
    let after = "";
    for (let page = 0; page < 20; page++) {
      const result = await api<{ resources: MAResource[]; next_after?: string }>(
        `${path(id)}/resources?kind=${kind}&after=${encodeURIComponent(after)}`);
      resources.push(...result.resources);
      if (!result.next_after) break;
      after = result.next_after;
      if (page === 19) throw new Error("资源列表超出分页限制");
    }
  }
  return { resources };
}
export const maReleases = (id: string) => api<{ releases: MARelease[] }>(`${path(id)}/releases`);
export const saveConnection = (body: Record<string, unknown>, id?: string) =>
  api<MAJob | MAConnection>(id ? path(id) : root, {
    method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
export const saveTargets = (id: string, name: string, revision: number, agents: string[]) =>
  api(`${path(id)}/resources/${encodeURIComponent(name)}/bindings`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: revision, agent_ids: agents }),
  });

export async function startMAJob(id: string, kind: "discover" | "collect" | "reconcile",
  body: Record<string, unknown> = {}): Promise<MAJob> {
  return api<MAJob>(`${path(id)}/${kind}`, {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify(body),
  });
}

export async function followMAJob(job: MAJob, signal: AbortSignal, onStatus: (status: string) => void) {
  if (job.status_url !== `/api/jobs/${job.job_id}`) throw new Error("后台任务地址无效");
  const headers = tenantHeaders(job.status_url);
  for (let poll = 0; poll < 900 && !signal.aborted; poll++) {
    const current = await api<{ status: string; error_code?: string }>(job.status_url, { headers, signal });
    onStatus(current.status);
    if (["failed", "cancelled"].includes(current.status)) throw new Error(current.error_code || current.status);
    if (current.status === "succeeded") return;
    if (current.status === "reconciling") throw new Error("远端结果正在核对，请在同步记录查看");
    await new Promise<void>(resolve => {
      const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
      const timer = setTimeout(finish, 2000);
      signal.addEventListener("abort", finish, { once: true });
    });
  }
  if (!signal.aborted) throw new Error("任务仍在后台执行，请稍后刷新");
}
