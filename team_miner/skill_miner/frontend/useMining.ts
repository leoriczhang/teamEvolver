import { miningApi as api } from "./miningApi";
import { toastErr } from "@/lib/toast";
import { useCallback,useEffect,useRef,useState } from "react";
import type { InputSource,MiningConfig,MiningDefaults,MiningJob,MiningJobSummary } from "./types";

export function useMining(active: boolean) {
  const [config, setConfig] = useState<MiningConfig | null>(null);
  const [jobs, setJobs] = useState<MiningJob[]>([]);
  const [jobSummary, setJobSummary] = useState<MiningJobSummary>({
    total: 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3,
  });
  const jobsRefreshInFlight = useRef<Promise<MiningJob[]> | null>(null);
  const configRefreshInFlight = useRef<Promise<MiningConfig | null> | null>(null);

  const refreshJobs = useCallback((notifyFailure = false) => {
    if (jobsRefreshInFlight.current) return jobsRefreshInFlight.current;
    const request = (async () => {
      try {
        const response = await api<{ jobs: MiningJob[]; summary: MiningJobSummary }>("/api/mining/jobs");
        setJobs(response.jobs || []);
        setJobSummary(response.summary || {
          total: 0, running: 0, queued: 0, completed: 0, failed: 0, max_parallel: 3,
        });
        return response.jobs || [];
      } catch (e: any) {
        if (notifyFailure) toastErr("加载挖掘任务失败", e.message);
        return [];
      }
    })().finally(() => {
      jobsRefreshInFlight.current = null;
    });
    jobsRefreshInFlight.current = request;
    return request;
  }, []);

  const refreshConfig = useCallback((notifyFailure = true) => {
    if (configRefreshInFlight.current) return configRefreshInFlight.current;
    const request = (async () => {
      try {
        const [base, ovSources] = await Promise.all([
          api<MiningDefaults>("/api/mining/config"),
          api<{ sources: InputSource[] }>("/api/mining/input-sources"),
        ]);
        const sources = ovSources.sources || [];
        const skillSources = sources.filter((source) => source.tags?.includes("skill"));
        const next: MiningConfig = {
          ...base,
          input_sources: sources,
          input_dirs: skillSources.map((source) => source.path),
          default_input_dir: skillSources.find((source) => source.ready)?.path || skillSources[0]?.path || "",
        };
        setConfig(next);
        await refreshJobs(false);
        return next;
      } catch (e: any) {
        if (notifyFailure) toastErr("加载挖掘配置失败", e.message);
        return null;
      }
    })().finally(() => {
      configRefreshInFlight.current = null;
    });
    configRefreshInFlight.current = request;
    return request;
  }, [refreshJobs]);

  const refreshInputSource = useCallback(async (sourceId: string) => {
    const response = await api<{ source: InputSource }>(
      `/api/mining/input-sources/${encodeURIComponent(sourceId)}`
    );
    setConfig((current) => current ? {
      ...current,
      input_sources: current.input_sources.map((source) =>
        source.source_id === sourceId ? response.source : source
      ),
    } : current);
    return response.source;
  }, []);

  useEffect(() => {
    if (!active) return;
    // Job summaries are local control-plane state and do not wait for OV tree scans.
    refreshJobs(false);
    refreshConfig(true);
    const jobsTimer = window.setInterval(() => refreshJobs(false), 5000);
    return () => {
      window.clearInterval(jobsTimer);
    };
  }, [active, refreshConfig, refreshJobs]);

  return { config, jobs, jobSummary, refreshConfig, refreshJobs, refreshInputSource };
}
