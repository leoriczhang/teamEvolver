import { api,tenantHeaders } from "@/api/client";

/** Submit once, then follow the persisted job using the original tenant. */
export async function runSkillCommand<T>(
  path: string, init: RequestInit, resultRoot = "/api/skill-evolution",
): Promise<T> {
  const headers = tenantHeaders(path, init.headers);
  const accepted = await api<{ job_id: string; status_url: string }>(path, { ...init, headers });
  if (!accepted.job_id || accepted.status_url !== `/api/jobs/${accepted.job_id}`) {
    throw new Error("服务未返回有效的后台任务");
  }
  for (let poll = 0; poll < 600; poll++) {
    const job = await api<{ status: string; error_code?: string }>(accepted.status_url, { headers });
    if (job.status === "failed" || job.status === "cancelled") {
      throw new Error(`任务${job.status === "failed" ? "失败" : "已取消"}：${job.error_code || accepted.job_id}`);
    }
    if (job.status === "reconciling" && resultRoot === "/api/skills/transfer") {
      throw new Error(`远端结果正在核对，请勿重复提交，可在任务记录查看：${accepted.job_id}`);
    }
    if (job.status === "succeeded") {
      const response = await api<{ result: Record<string, unknown> }>(
        `${resultRoot}/jobs/${encodeURIComponent(accepted.job_id)}/result`, { headers },
      );
      const result = response.result;
      if (!result) throw new Error("任务完成但结果尚不可用");
      return (result.status === "evaluated" ? result.evaluation : result) as T;
    }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
  throw new Error(`任务仍在后台执行，可稍后查看结果：${accepted.job_id}`);
}
