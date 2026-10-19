
export interface AdapterFile {
  file: string;
  label?: string;
  provider?: string;
  host?: string;
  project_id?: string;
  bound_tenant_id?: string;
  error?: string;
  supported_filters?: string[];
  required_filters?: string[];
  max_sessions?: number;
}

export interface Persistence {
  durable: boolean;
  mode: "postgres";
  warning?: string;
}

export interface Datasource extends AdapterFile {
  tenant_id: string;
  configured: boolean;
  enabled: boolean;
  revision?: number;
  source_editable?: boolean;
  available: AdapterFile[];
  persistence?: Persistence;
  schedule?: DatasourceSchedule;
  schedule_status?: DatasourceScheduleStatus;
}

export interface DatasourceSchedule {
  enabled: boolean;
  time: string;
  timezone: string;
  window: "previous_day";
}

export interface DatasourceScheduleStatus {
  running: boolean;
  job_id?: string;
  job_type?: "manual" | "scheduled";
  next_run_at?: string;
  last_started_at?: string;
  last_finished_at?: string;
  last_status?: string;
  last_error?: string;
  last_window_from?: string;
  last_window_to?: string;
  last_target_date?: string;
  last_total?: number;
  last_counts?: Record<string, number>;
  current_total?: number;
  current_counts?: Record<string, number>;
  last_results?: SessionRow[];
  last_results_truncated?: boolean;
  last_result_limit?: number;
}

export interface SessionRow {
  session_id: string;
  title?: string;
  user_id?: string;
  timestamp?: string;
  trace_count?: number;
  status?: string;
  reason?: string;
}

export interface DraftResult {
  ok: boolean;
  metadata: AdapterFile;
  health?: { ok: boolean; error?: string };
  sessions?: SessionRow[];
  count?: number;
  [key: string]: unknown;
}

export const LABELS: Record<string, string> = {
  from_timestamp: "起始时间", to_timestamp: "结束时间",
  session_id: "Session ID", user_id: "用户", environment: "Environment",
  tags: "Tags", trace_name: "Trace 名称", release: "Release",
  version: "Version", metadata: "Metadata",
};

export const DEFAULT_SCHEDULE: DatasourceSchedule = {
  enabled: false,
  time: "00:00",
  timezone: "Asia/Shanghai",
  window: "previous_day",
};

export const SCHEDULE_TIMEZONES = [
  "Asia/Shanghai",
  "UTC",
  "Asia/Tokyo",
  "Europe/London",
  "America/Los_Angeles",
];

export function formatScheduleTime(value: string | undefined, timezone: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(parsed);
  } catch {
    return parsed.toLocaleString();
  }
}

export function toIsoUtc(localValue: string): string {
  const d = new Date(localValue);
  return isNaN(d.getTime()) ? localValue : d.toISOString();
}

export const WARNING =
  "保存生成不可变版本；绑定固定版本后，所有实例读取同一份适配器。";

export const TEMPLATE = `"""Tenant upstream datasource adapter."""

SOURCE = {
    "label": "New tenant adapter",
    "provider": "custom",
    "host": "",
    "enabled": True,
    "max_sessions": 100,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id"],
    "required_filters": [],
}

def build_adapter():
    return Adapter()

class Adapter:
    def health(self):
        return {"ok": False, "error": "Implement the upstream health probe"}

    def list_session_ids(self, filters, *, max_sessions):
        raise NotImplementedError

    def iter_session_id_batches(self, filters, *, batch_size):
        raise NotImplementedError

    def fetch_session(self, session_id):
        raise NotImplementedError

    def convert_session(self, raw_session, raw_traces):
        raise NotImplementedError

    def close(self):
        pass
`;
