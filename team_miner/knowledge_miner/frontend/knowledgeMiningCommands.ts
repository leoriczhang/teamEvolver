import { api, tenantHeaders } from "@/api/client";

import type { MiningTask } from "./KnowledgeBaseMiningViewShared";

export interface KnowledgeMiningReceipt {
  job_id: string;
  status_url: string;
  account?: string;
}

type WorkerJob = Partial<MiningTask> & {
  job_id?: string;
  upstream_task_id?: string;
  cancel_requested?: boolean;
  error_code?: string;
};

const COMMAND_ROOT = "/api/knowledge-mining";
const WAITING = new Set(["queued", "running", "retry_wait", "reconciling", "waiting_external"]);
const TERMINAL = new Set(["succeeded", "completed", "failed", "cancelled"]);
const WAIT_MS = 20 * 60 * 1000;
const POLL_MS = 2000;

function validId(id: unknown): id is string {
  return typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9:._-]{0,239}$/.test(id);
}

function receipt(value: unknown): KnowledgeMiningReceipt | null {
  if (!value || typeof value !== "object" || !("status_url" in value)) return null;
  const candidate = value as KnowledgeMiningReceipt;
  if (!validId(candidate.job_id) || candidate.status_url !== `/api/jobs/${candidate.job_id}`) {
    throw new Error("服务未返回有效的知识库后台任务");
  }
  return candidate;
}

function pause(signal?: AbortSignal | null) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) return reject(signal.reason);
    const abort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, POLL_MS);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

/** Pass the submitting request's headers to keep accepted work on its original tenant. */
export async function waitForKnowledgeCommand<T>(
  response: T | KnowledgeMiningReceipt, init?: RequestInit, resultRequired = true,
): Promise<T> {
  const accepted = receipt(response);
  if (!accepted) return response as T;
  const headers = tenantHeaders(accepted.status_url, new Headers(init?.headers));
  const deadline = Date.now() + WAIT_MS;
  for (let poll = 0; poll < 600 && Date.now() < deadline; poll++) {
    if (init?.signal?.aborted) throw init.signal.reason;
    const job = await api<WorkerJob>(accepted.status_url, { headers, signal: init?.signal });
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(`任务${job.status === "failed" ? "失败" : "已取消"}：${job.error_code || accepted.job_id}`);
    }
    if (job.status === "succeeded") {
      if (!resultRequired) return { ok: true } as T;
      const result = await api<T>(`${COMMAND_ROOT}/jobs/${accepted.job_id}/result`, { headers, signal: init?.signal });
      if (result === undefined || result === null) throw new Error("任务完成但结果尚不可用");
      return result;
    }
    if (!WAITING.has(String(job.status))) throw new Error("后台任务状态无效，请刷新后重试");
    await pause(init?.signal);
  }
  throw new Error(`任务仍在后台执行，请稍后刷新查看结果：${accepted.job_id}`);
}

function taskView(task: WorkerJob, id: string): MiningTask {
  const status = String(task.status || "queued");
  const normalized = status === "succeeded" ? "completed"
    : status === "retry_wait" ? "queued"
      : ["reconciling", "waiting_external"].includes(status) ? "running" : status;
  return {
    ...task,
    task_id: task.task_id || id,
    status: task.cancel_requested && !TERMINAL.has(status) ? "cancelling" : normalized,
    error: task.error || task.error_code || null,
  };
}

export async function submitKnowledgeTask(body: Record<string, unknown>): Promise<MiningTask> {
  const path = `${COMMAND_ROOT}/tasks`;
  const headers = tenantHeaders(path, { "Content-Type": "application/json" });
  const response = await api<MiningTask | KnowledgeMiningReceipt>(path, {
    method: "POST", headers, body: JSON.stringify(body),
  });
  const accepted = receipt(response);
  if (accepted) return { task_id: accepted.job_id, status: "queued" };
  return taskView(response as MiningTask, "");
}

export async function loadKnowledgeTask(id: string, account: string): Promise<MiningTask> {
  if (!validId(id)) throw new Error("无效的知识库任务标识");
  const headers = tenantHeaders(COMMAND_ROOT);
  let upstream = id;
  let job: WorkerJob | null = null;
  if (id.startsWith("miner:")) {
    job = await api<WorkerJob>(`/api/jobs/${id}`, { headers });
    if (!job.upstream_task_id) return taskView({ ...job, task_id: id }, id);
    upstream = job.upstream_task_id;
    if (!validId(upstream)) throw new Error("无效的上游任务标识");
  }
  const response = await api<{ task: MiningTask }>(
    `${COMMAND_ROOT}/tasks/${upstream}?account=${encodeURIComponent(account)}`, { headers },
  );
  return taskView({ ...response.task, ...(job && { job_id: id, cancel_requested: job.cancel_requested }) }, upstream);
}

export async function cancelKnowledgeTask(id: string, account: string): Promise<MiningTask> {
  if (!validId(id)) throw new Error("无效的知识库任务标识");
  const headers = tenantHeaders(COMMAND_ROOT, { "Content-Type": "application/json" });
  if (id.startsWith("miner:")) {
    const job = await api<WorkerJob>(`/api/jobs/${id}/cancel`, { method: "POST", headers });
    return taskView({ ...job, task_id: id, status: TERMINAL.has(String(job.status)) ? job.status : "cancelling" }, id);
  }
  const response = await api<{ task: MiningTask }>(`${COMMAND_ROOT}/tasks/${id}/cancel`, {
    method: "POST", headers, body: JSON.stringify({ account }),
  });
  return taskView(response.task, id);
}
