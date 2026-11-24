import { Input } from "@/components/ui/input";
import { type TenantRuntimeConfig } from "./api/client";

const fields: [keyof TenantRuntimeConfig, string, string, number][] = [
  ["collection_concurrency", "采集与 MA 同步", "数据源拉取和 MA 连接操作", 32],
  ["ingestion_concurrency", "入库各阶段并发", "标准化、拆分、归档各自保留此数量", 32],
  ["analysis_concurrency", "Session 分析", "长分析任务独立运行", 32],
  ["evolution_concurrency", "Skill Evolution", "技能进化和迁移任务", 32],
  ["replay_concurrency", "验证与回放", "候选验证、Dataset 和 Replay", 32],
  ["background_concurrency", "其他后台任务", "Memory、资料编译和索引维护", 32],
  ["llm_max_concurrency", "模型并发", "本租户同时进行的模型请求", 8],
  ["llm_queue_capacity", "模型队列容量", "含正在执行的请求，队列满时返回重试", 10000],
];

export function validRuntimeCapacity(value: TenantRuntimeConfig) {
  return fields.every(([key, , , max]) => Number.isInteger(value[key]) && value[key] >= 1 && value[key] <= max)
    && value.llm_queue_capacity >= value.llm_max_concurrency;
}

export function RuntimeCapacityFields({ value, onChange }: {
  value: TenantRuntimeConfig;
  onChange: (value: TenantRuntimeConfig) => void;
}) {
  return (
    <section>
      <div className="mb-1 text-[13px] font-bold">独立执行容量</div>
      <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
        每个租户独立使用以下容量。采集、入库、分析和进化各有执行槽位，配置约 5 秒内生效。
        降低并发时，正在执行的任务完成后释放槽位。
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        {fields.map(([key, label, hint, max]) => (
          <label key={key} className="block rounded-lg border border-border bg-surface-subtle p-3">
            <span className="mb-1.5 block text-xs font-semibold">{label}</span>
            <Input type="number" min="1" max={max} step="1" value={Number.isNaN(value[key]) ? "" : value[key]}
              onChange={(event) => onChange({ ...value, [key]: event.target.value === "" ? NaN : Number(event.target.value) })} />
            <span className="mt-1.5 block text-[11px] leading-relaxed text-muted-foreground">{hint}</span>
          </label>
        ))}
      </div>
    </section>
  );
}
