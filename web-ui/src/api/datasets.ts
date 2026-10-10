import { api, tenantHeaders } from "./client";

export interface SessionDataset {
  dataset_id: string;
  name: string;
  description: string;
  case_count: number;
  created_at: string;
  updated_at: string;
  source?: { filters?: Record<string, string> };
}
export interface DatasetCase {
  case_id: string;
  query: string;
  skill_ids: string[];
  checks: Array<{ id: string; kind: "output" | "trajectory"; text: string; polarity?: "must_not" }>;
  materials: Array<Record<string, unknown>>;
  provenance: {
    session_id?: string;
    trace_id?: string;
    timestamp?: string;
    ingested_at?: string;
    session_snapshot?: Record<string, any>;
  };
  replay: { progressive_disclosure: { batch_size: number } };
  metadata: {
    title?: string;
    used_skills?: string[];
    user_alias?: string;
    judge?: { overall_score?: number };
  };
}
export interface DatasetDetail extends SessionDataset {
  cases: DatasetCase[];
  total: number;
}
interface BatchCase {
  case_id: string;
  session_id: string;
  trace_id: string;
  query: string;
  status: string;
  success: boolean | null;
  replay_trace_id?: string;
  error?: string;
}
export interface DatasetRun {
  run_id: string;
  dataset_id: string;
  status: string;
  total: number;
  completed: number;
  succeeded: number;
  failed: number;
  skipped: number;
  created_at: string;
  finished_at: string;
  error?: string;
  cases?: BatchCase[];
}
export const datasetPath = (id: string) => `/api/datasets/${encodeURIComponent(id)}`;
export const isActiveRun = (status: string) => ["queued", "running", "cancelling"].includes(status);
export const jsonBody = (body: unknown, method = "POST"): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
export async function downloadDataset(dataset: SessionDataset) {
  const path = `${datasetPath(dataset.dataset_id)}/export`;
  const response = await fetch(path, { headers: tenantHeaders(path), signal: AbortSignal.timeout(120000) });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || `导出失败（${response.status}）`);
  }
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `${dataset.name.replace(/[\\/:*?"<>|]/g, "_")}.zip`;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export { api };
