import { Empty,Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { type UserProfile } from "@/features/admin/api/client";
import {
AlertTriangle,CalendarClock,CheckCheck,Clock3,Code2,DownloadCloud,
FilePlus2,List,Play,PlugZap,RefreshCw,Save,Upload,X,
} from "lucide-react";
import { formatScheduleTime,LABELS,SCHEDULE_TIMEZONES } from "./DataSourcesViewShared";
import { useDataSourcesView } from "./useDataSourcesView";

export default function DataSourcesView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
const { uploadRef, source, file, filters, setFilters, previewLimit, setPreviewLimit, health, rows, counts, code, setCode, codeFile, setCodeFile, revision, dirty, setDirty, setDraftMeta, draftResult, setDraftResult, schedule, setSchedule, busy, error, pullSummary, canEditSource, mayDiscard, closeDraft, chooseFile, loadCode, newDraft, uploadDraft, saveBinding, saveSchedule, runScheduleNow, saveCode, runDraft, runSaved, bindingDirty, savedReady, activeMeta, filterMeta, missing, warning, savedSchedule, scheduleStatus, scheduleDirty, scheduleAdapterReady, scheduleLabel, scheduleTone } = useDataSourcesView({ active, user });
if (user?.role !== "admin") return <Empty>仅管理员可管理数据源。</Empty>;
return (
    <div className="mx-auto w-full max-w-[1200px] space-y-6 p-5">
      <div className="flex gap-3 border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
        <div>
          <div className="font-semibold">适配器版本已持久化</div>
          <div className="mt-1 break-words text-xs leading-relaxed">{warning}</div>
          <div className="mt-1 text-xs">适配器以服务进程权限执行，仅上传可信代码。</div>
        </div>
      </div>

      <section className="space-y-4 border-b border-border pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-base font-semibold">租户数据源</h2>
          <Button
            variant="ghost"
            size="sm"
            title="刷新适配器"
            aria-label="刷新适配器"
            disabled={!!busy}
            onClick={() => runSaved("refresh")}
          >
            <RefreshCw className="size-4" />
          </Button>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-0 flex-1 text-xs font-medium">
            适配器文件
            <select
              aria-label="适配器文件"
              value={file}
              disabled={!!busy}
              onChange={(event) => chooseFile(event.target.value)}
              className="mt-2 h-10 w-full min-w-0 rounded-md border border-border bg-background px-2 text-sm"
            >
              <option value="">未绑定</option>
              {file && !source?.available.some((item) => item.file === file) && (
                <option value={file}>{file}（文件缺失）</option>
              )}
              {(source?.available || []).map((item) => (
                <option
                  key={item.file}
                  value={item.file}
                  disabled={
                    !!item.error
                    || !!item.bound_tenant_id
                      && item.bound_tenant_id !== source?.tenant_id
                  }
                >
                  {item.file}
                  {item.bound_tenant_id && item.bound_tenant_id !== source?.tenant_id
                    ? "（已绑定其他租户）"
                    : ""}
                  {item.error ? "（文件无效）" : ""}
                </option>
              ))}
            </select>
          </label>
          <Button disabled={!bindingDirty || !!busy} onClick={saveBinding}>
            <Save className="size-4" />
            {busy === "binding" ? "保存中" : "保存绑定"}
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <Pill tone={source?.configured && source.enabled ? "green" : "gray"}>
            {source?.configured ? source.enabled ? "已启用" : "已停用" : "未配置"}
          </Pill>
          {source?.provider && <span>来源：{source.provider}</span>}
          {source?.host && (
            <span className="break-all">数据源地址：{source.host}</span>
          )}
          {source?.project_id && (
            <span className="break-all">上游项目：{source.project_id}</span>
          )}
          {source?.revision && (
            <span className="font-mono text-xs text-muted-foreground">
              版本 {source.revision}
            </span>
          )}
        </div>
        {source?.error && (
          <p className="break-words text-sm text-muted-foreground">{source.error}</p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={!savedReady}
            onClick={() => runSaved("test")}
          >
            <PlugZap className="size-4" />
            {busy === "test" ? "测试中" : "测试已保存连接"}
          </Button>
          {canEditSource && <><Button variant="outline" disabled={!file || !!busy} onClick={loadCode}>
            <Code2 className="size-4" />
            编辑源码
          </Button>
          <input
            ref={uploadRef}
            aria-label="上传适配器文件"
            type="file"
            accept=".py,text/x-python"
            className="hidden"
            onChange={(event) => uploadDraft(event.target.files?.[0])}
          />
          <Button
            variant="outline"
            disabled={!!busy}
            onClick={() => uploadRef.current?.click()}
          >
            <Upload className="size-4" />
            上传 .py
          </Button>
          <Button variant="outline" disabled={!!busy} onClick={newDraft}>
            <FilePlus2 className="size-4" />
            新建
          </Button>
          </>}
          {health && (
            <span role="status" className="self-center text-sm">
              {health.ok ? "连接正常" : `连接失败：${health.error || "上游不可用"}`}
            </span>
          )}
        </div>
      </section>

      <section className="space-y-4 border-b border-border pb-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="min-w-0">
            <h2 className="flex items-center gap-2 text-base font-semibold">
              <CalendarClock className="size-4" />
              定时拉取
            </h2>
            <p className="mt-1 text-xs text-muted-foreground">
              每天按所选时区拉取上一自然日的 Trace，转换后进入 Session 队列等待分析。
            </p>
          </div>
          <Pill tone={scheduleTone}>{scheduleLabel}</Pill>
        </div>

        <label className="flex w-fit items-center gap-2 text-sm font-medium">
          <input
            aria-label="启用每日定时拉取"
            type="checkbox"
            checked={schedule.enabled}
            disabled={!!busy}
            onChange={(event) => setSchedule((current) => ({
              ...current,
              enabled: event.target.checked,
            }))}
            className="size-4 accent-primary"
          />
          启用每日定时拉取
        </label>

        <div className="grid min-w-0 gap-4 sm:grid-cols-2">
          <label className="min-w-0 text-xs font-medium">
            每日触发时间
            <Input
              aria-label="每日触发时间"
              type="time"
              value={schedule.time}
              disabled={!!busy}
              className="mt-2"
              onChange={(event) => setSchedule((current) => ({
                ...current,
                time: event.target.value,
              }))}
            />
          </label>
          <label className="min-w-0 text-xs font-medium">
            时区
            <select
              aria-label="定时拉取时区"
              value={schedule.timezone}
              disabled={!!busy}
              onChange={(event) => setSchedule((current) => ({
                ...current,
                timezone: event.target.value,
              }))}
              className="mt-2 h-10 w-full min-w-0 rounded-md border border-border bg-background px-2 text-sm"
            >
              {SCHEDULE_TIMEZONES.map((timezone) => (
                <option key={timezone} value={timezone}>{timezone}</option>
              ))}
            </select>
          </label>
        </div>

        {schedule.enabled && !scheduleAdapterReady && (
          <p className="text-xs text-amber-800">
            启用前需保存一个可用且支持起止时间过滤的适配器绑定。
          </p>
        )}

        <div className="flex flex-wrap gap-2">
          <Button
            disabled={
              !scheduleDirty
              || !!busy
              || schedule.enabled && !scheduleAdapterReady
            }
            onClick={saveSchedule}
          >
            <Save className="size-4" />
            {busy === "schedule-save" ? "保存中" : "保存定时配置"}
          </Button>
          <Button
            variant="outline"
            disabled={
              !!busy
              || scheduleDirty
              || !savedSchedule.enabled
              || !scheduleAdapterReady
              || scheduleStatus?.running
            }
            onClick={runScheduleNow}
          >
            <Play className="size-4" />
            {busy === "schedule-run" ? "启动中" : "立即拉取昨日 Trace"}
          </Button>
        </div>

        <div className="grid gap-x-8 gap-y-2 border-t border-border pt-4 text-xs sm:grid-cols-2">
          <div className="flex min-w-0 items-start gap-2">
            <Clock3 className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            <span className="text-muted-foreground">下次触发</span>
            <span className="break-words">
              {formatScheduleTime(scheduleStatus?.next_run_at, savedSchedule.timezone)}
            </span>
          </div>
          <div>
            <span className="text-muted-foreground">业务时间窗口：</span>
            {scheduleStatus?.last_target_date
              ? `${scheduleStatus.last_target_date} 全天（${savedSchedule.timezone}）`
              : "-"}
          </div>
          <div>
            <span className="text-muted-foreground">系统开始时间：</span>
            {formatScheduleTime(scheduleStatus?.last_started_at, savedSchedule.timezone)}
          </div>
          <div>
            <span className="text-muted-foreground">系统完成时间：</span>
            {formatScheduleTime(scheduleStatus?.last_finished_at, savedSchedule.timezone)}
          </div>
          {scheduleStatus?.last_status && (
            <div>
              <span className="text-muted-foreground">上次结果：</span>
              {scheduleStatus.last_total ?? 0} 个 Session
              {scheduleStatus.last_counts
                ? `，${Object.entries(scheduleStatus.last_counts)
                    .filter(([, value]) => value > 0)
                    .map(([key, value]) => `${key} ${value}`)
                    .join("，") || "无新增"}`
                : ""}
            </div>
          )}
          {scheduleStatus?.last_error && (
            <div className="break-words text-destructive sm:col-span-2">
              上次错误：{scheduleStatus.last_error}
            </div>
          )}
        </div>
      </section>

      {canEditSource && code !== null && (
        <section className="space-y-4 border-b border-border pb-5">
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
            <div className="min-w-0">
            <h2 className="text-base font-semibold">适配器版本编辑</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                校验和测试直接使用编辑器草稿，不写磁盘。
              </p>
            </div>
            <div className="flex items-center gap-2">
              {dirty && <Pill tone="amber">未保存</Pill>}
              <Button
                variant="ghost"
                size="sm"
                aria-label="关闭源码编辑"
                title="关闭源码编辑"
                onClick={() => mayDiscard() && closeDraft()}
              >
                <X className="size-4" />
              </Button>
            </div>
          </div>
          <label className="block text-xs font-medium">
            文件名
            <Input
              aria-label="适配器文件名"
              className="mt-2 font-mono"
              value={codeFile}
              disabled={!!busy}
              onChange={(event) => {
                setCodeFile(event.target.value);
                setDirty(true);
                setDraftResult(null);
              }}
            />
          </label>
          <Textarea
            aria-label="适配器源码"
            value={code}
            disabled={!!busy}
            spellCheck={false}
            className="h-[480px] resize-y whitespace-pre font-mono text-xs leading-relaxed"
            onChange={(event) => {
              setCode(event.target.value);
              setDirty(true);
              setDraftMeta(null);
              setDraftResult(null);
            }}
          />
          <div className="flex flex-wrap gap-2">
            <Button disabled={!dirty || !!busy} onClick={saveCode}>
              <Save className="size-4" />
              {busy === "saveCode" ? "保存中" : "保存新版本"}
            </Button>
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={() => runDraft("validate")}
            >
              <CheckCheck className="size-4" />
              校验草稿
            </Button>
            <Button
              variant="outline"
              disabled={!!busy}
              onClick={() => runDraft("health")}
            >
              <PlugZap className="size-4" />
              测试草稿连接
            </Button>
            <Button
              variant="outline"
              disabled={!!busy || missing}
              onClick={() => runDraft("preview")}
            >
              <Play className="size-4" />
              预览草稿
            </Button>
            {(activeMeta?.supported_filters || []).includes("session_id") && (
              <Button
                variant="outline"
                disabled={!!busy || !filters.session_id?.trim()}
                onClick={() => runDraft("session")}
              >
                <Code2 className="size-4" />
                转换单条
              </Button>
            )}
          </div>
          <div className="text-xs text-amber-800">
            保存后生成固定版本；重新保存绑定后，后续拉取使用该版本。
          </div>
          {draftResult && (
            <pre
              aria-label="草稿测试结果"
              className="max-h-80 overflow-auto whitespace-pre-wrap break-words border border-border bg-surface-subtle p-4 text-xs"
            >
              {JSON.stringify(draftResult, null, 2)}
            </pre>
          )}
        </section>
      )}

      {error && (
        <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      )}

      <section className="space-y-4">
        <h2 className="text-base font-semibold">Session 拉取</h2>
        <div className="grid min-w-0 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {(filterMeta?.supported_filters || []).map((key) => (
            <label className="min-w-0 text-xs font-medium" key={key}>
              {LABELS[key] || key}
              {filterMeta?.required_filters?.includes(key) ? " *" : ""}
              <Input
                aria-label={LABELS[key] || key}
                type={key.endsWith("timestamp") ? "datetime-local" : "text"}
                value={filters[key] || ""}
                disabled={!!busy}
                className="mt-2"
                onChange={(event) => setFilters((current) => ({
                  ...current,
                  [key]: event.target.value,
                }))}
              />
            </label>
          ))}
          <label className="text-xs font-medium">
            预览条数
            <Input
              aria-label="预览条数"
              type="number"
              min={1}
              max={1000}
              value={previewLimit}
              disabled={!!busy}
              onChange={(event) => setPreviewLimit(event.target.value)}
              className="mt-2"
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={!savedReady || missing}
            onClick={() => runSaved("sessions")}
          >
            <List className="size-4" />
            {busy === "sessions" ? "查询中" : "预览已保存"}
          </Button>
          <Button
            disabled={!savedReady || missing || scheduleStatus?.running}
            onClick={() => runSaved("pull")}
          >
            <DownloadCloud className="size-4" />
            {busy === "pull" || (
              scheduleStatus?.running && scheduleStatus.job_type === "manual"
            ) ? "拉取中" : "拉取入库"}
          </Button>
        </div>
        {counts && (
          <div className="flex flex-wrap gap-4 text-xs">
            {Object.entries(counts).map(([key, value]) => (
              <span key={key}>{key}: {value}</span>
            ))}
          </div>
        )}
        {pullSummary && (
          <p className="text-xs text-muted-foreground">
            {scheduleStatus?.running && scheduleStatus.job_type === "manual"
              ? `正在拉取，已处理 ${pullSummary.total} 个 Session`
              : `已处理完整时间窗口内的 ${pullSummary.total} 个 Session${
                pullSummary.truncated
                  ? `，下方仅展示前 ${pullSummary.shown} 条明细`
                  : ""
              }`}
          </p>
        )}
        {rows && (rows.length ? (
          <div className="overflow-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr>
                  {["Session", "标题", "用户", "时间", "状态"].map((label) => (
                    <th key={label} className="border-b border-border p-3">{label}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.session_id}>
                    <td className="max-w-64 break-all border-b border-border p-3">
                      {row.session_id}
                    </td>
                    <td className="max-w-64 break-words border-b border-border p-3">
                      {row.title || "-"}
                    </td>
                    <td className="border-b border-border p-3">{row.user_id || "-"}</td>
                    <td className="border-b border-border p-3">{row.timestamp || "-"}</td>
                    <td className="max-w-64 break-words border-b border-border p-3">
                      {row.reason || row.status || `${row.trace_count ?? "-"} traces`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>没有匹配的 Session。</Empty>
        ))}
      </section>
    </div>
  );
}
