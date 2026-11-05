import { api, tenantHeaders } from "@/api/client";

interface MiningReceipt {
  job_id?: string;
  status_url?: string;
}

interface MiningCommandStatus<T> {
  status: string;
  result?: T;
  error_code?: string;
}

const WAITING_STATES = new Set(["queued", "running", "retry_wait", "reconciling", "waiting_external"]);
const COMMAND_WAIT_MS = 20 * 60 * 1000;
const POLL_INTERVAL_MS = 2000;

function pause(signal?: AbortSignal | null) {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const abort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, POLL_INTERVAL_MS);
    signal?.addEventListener("abort", abort, { once: true });
  });
}

/** Keep ordinary mining responses intact; await explicitly accepted Worker commands. */
export async function miningApi<T = any>(path: string, init?: RequestInit): Promise<T> {
  const mutation = init?.method && !["GET", "HEAD"].includes(init.method.toUpperCase());
  // Pin accepted work to the tenant that submitted it, including subsequent polls.
  const headers = tenantHeaders(path, init?.headers);
  const response = init
    ? await api<T & MiningReceipt>(path, mutation ? { ...init, headers } : init)
    : await api<T & MiningReceipt>(path);
  if (!response || typeof response !== "object" || !response.status_url) return response;
  const { job_id: jobId, status_url: statusUrl } = response;
  if (!jobId || statusUrl !== `/api/mined-jobs/${jobId}`) {
    throw new Error("服务未返回有效的挖掘后台任务");
  }
  const deadline = Date.now() + COMMAND_WAIT_MS;
  for (let poll = 0; poll < 600 && Date.now() < deadline; poll++) {
    const job = await api<MiningCommandStatus<T>>(statusUrl, { headers, signal: init?.signal });
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(`任务${job.status === "failed" ? "失败" : "已取消"}：${job.error_code || jobId}`);
    }
    if (job.status === "succeeded") {
      if (job.result === undefined || job.result === null) throw new Error("任务完成但结果尚不可用");
      return job.result;
    }
    if (!WAITING_STATES.has(job.status)) throw new Error("后台任务状态无效，请刷新后重试");
    await pause(init?.signal);
  }
  throw new Error(`任务仍在后台执行，请稍后刷新查看结果：${jobId}`);
}
