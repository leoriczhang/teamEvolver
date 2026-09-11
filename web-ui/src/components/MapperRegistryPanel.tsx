import { useState, type ReactNode } from "react";
import { ListViewport, Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  api,
  type LangfuseMapperEntry,
  type LangfuseMapperFormatSpec,
  type LangfuseMapperMatch,
  type LangfuseMapperTemplateResp,
  type LangfuseMapperTestResp,
  type LangfuseRouteReport,
} from "@/api/client";
import { toastErr, toastOk } from "@/lib/toast";

// Shared per-agent mapper-registry editor (used by the global Langfuse view and
// the per-tenant config editor). An ordered list of named mapping entries, each
// with routing constraints (trace name patterns + tags + sessionId patterns)
// and operator-authored Python code. First matching enabled entry wins at pull
// time; editing/testing is admin-only (it is executable config).

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

// Client-side match summary, mirroring server-side MapperMatch.describe().
function describeMatch(m?: LangfuseMapperMatch): string {
  const parts: string[] = [];
  if (m?.trace_names?.length) parts.push("name~" + m.trace_names.join("|"));
  if (m?.tags?.length) parts.push("tag:" + m.tags.join("|"));
  if (m?.session_id_patterns?.length) parts.push("sid~" + m.session_id_patterns.join("|"));
  return parts.length ? parts.join(" · ") : "全部匹配";
}

function isCatchAll(m?: LangfuseMapperMatch): boolean {
  return !m?.trace_names?.length && !m?.tags?.length && !m?.session_id_patterns?.length;
}

function FormField({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-[11px] text-muted-soft">{hint}</span>}
    </label>
  );
}

export function MapperRegistryPanel({
  isAdmin,
  mappers,
  onChange,
  onSave,
  saving,
  title = "映射注册表（按 Agent 路由）",
}: {
  isAdmin: boolean;
  mappers: LangfuseMapperEntry[];
  onChange: (entries: LangfuseMapperEntry[]) => void;
  onSave: () => void | Promise<void>;
  saving: boolean;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState(0);
  const [traceJson, setTraceJson] = useState("");
  const [testing, setTesting] = useState(false);
  const [loadingTpl, setLoadingTpl] = useState(false);
  const [result, setResult] = useState<LangfuseMapperTestResp | null>(null);
  const [route, setRoute] = useState<LangfuseRouteReport | null>(null);
  const [routeTesting, setRouteTesting] = useState(false);
  const [spec, setSpec] = useState<LangfuseMapperFormatSpec | null>(null);
  const [specOpen, setSpecOpen] = useState(false);
  const [loadingSpec, setLoadingSpec] = useState(false);

  const entry: LangfuseMapperEntry | null = mappers[selected] ?? null;

  // The mapper endpoints were added to the evolve service; when the console
  // talks to an older running service the routes 404. Surface a clear hint to
  // restart rather than a bare "加载失败".
  function describeMapperError(e: any): string {
    const msg = String(e?.message || e || "");
    if (/404|not found|Method Not Allowed|405/i.test(msg)) {
      return "该接口不存在，通常是服务未重启。请重启 teamEvolver 服务后重试。";
    }
    return msg;
  }

  async function fetchTemplate(): Promise<LangfuseMapperTemplateResp> {
    const tpl = await api<LangfuseMapperTemplateResp>("/langfuse/mapper/template");
    if (tpl.spec) setSpec(tpl.spec);
    return tpl;
  }

  function patchEntry(patch: Partial<LangfuseMapperEntry>) {
    if (!entry) return;
    onChange(mappers.map((m, i) => (i === selected ? { ...m, ...patch } : m)));
  }

  function patchMatch(patch: Partial<LangfuseMapperMatch>) {
    if (!entry) return;
    onChange(
      mappers.map((m, i) =>
        i === selected ? { ...m, match: { ...m.match, ...patch } } : m
      )
    );
  }

  function addEntry() {
    const next = [
      ...mappers,
      { name: `agent-${mappers.length + 1}`, enabled: true, match: {}, code: "" },
    ];
    onChange(next);
    setSelected(next.length - 1);
  }

  function removeEntry(index: number) {
    const target = mappers[index];
    if (
      !window.confirm(
        `删除映射条目「${target?.name || index + 1}」？（保存后生效）`
      )
    )
      return;
    onChange(mappers.filter((_, i) => i !== index));
    setSelected((s) => Math.max(0, Math.min(s, mappers.length - 2)));
  }

  function moveEntry(index: number, dir: -1 | 1) {
    const j = index + dir;
    if (j < 0 || j >= mappers.length) return;
    const next = [...mappers];
    [next[index], next[j]] = [next[j], next[index]];
    onChange(next);
    setSelected(j);
  }

  async function insertTemplate() {
    setLoadingTpl(true);
    try {
      const tpl = await fetchTemplate();
      if (entry) {
        if (!entry.code.trim() || window.confirm("用参考模板覆盖当前代码？")) {
          patchEntry({ code: tpl.template });
        }
      }
      if (!traceJson.trim()) {
        setTraceJson(JSON.stringify(tpl.sample, null, 2));
      }
    } catch (e: any) {
      toastErr("加载模板失败", describeMapperError(e));
    } finally {
      setLoadingTpl(false);
    }
  }

  async function showSpec() {
    setSpecOpen(true);
    if (spec) return;
    setLoadingSpec(true);
    try {
      const tpl = await fetchTemplate();
      if (!tpl.spec) {
        toastErr("加载标准格式说明失败", "服务未返回格式说明，请重启服务后重试。");
      }
    } catch (e: any) {
      toastErr("加载标准格式说明失败", describeMapperError(e));
    } finally {
      setLoadingSpec(false);
    }
  }

  function parseTraceArg(): { arg?: unknown; error?: string } {
    const raw = traceJson.trim();
    if (!raw) return {};
    try {
      return { arg: JSON.parse(raw) };
    } catch (e: any) {
      return { error: e.message };
    }
  }

  async function runTest() {
    if (!entry?.code.trim()) {
      toastErr("无法测试", "请先填写该条目的映射代码");
      return;
    }
    const parsed = parseTraceArg();
    if (parsed.error) {
      toastErr("样例 JSON 无法解析", parsed.error);
      return;
    }
    setTesting(true);
    setResult(null);
    try {
      const data = await api<LangfuseMapperTestResp>("/langfuse/mapper/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          code: entry.code,
          trace: parsed.arg,
          match: entry.match || {},
        }),
      });
      setResult(data);
      if (data.ok) {
        toastOk("映射成功", data.used_sample ? "使用内置样例 trace" : "使用自定义 trace");
      } else {
        toastErr("映射失败", data.error || "未知错误");
      }
    } catch (e: any) {
      toastErr("测试请求失败", describeMapperError(e));
    } finally {
      setTesting(false);
    }
  }

  async function runRoutePreview() {
    const parsed = parseTraceArg();
    if (parsed.error) {
      toastErr("样例 JSON 无法解析", parsed.error);
      return;
    }
    setRouteTesting(true);
    setRoute(null);
    try {
      const data = await api<LangfuseRouteReport>("/langfuse/mapper/route-preview", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trace: parsed.arg, mappers }),
      });
      setRoute(data);
    } catch (e: any) {
      toastErr("路由预览失败", describeMapperError(e));
    } finally {
      setRouteTesting(false);
    }
  }

  const shadowedIndex = mappers.findIndex(
    (m, i) => i < mappers.length - 1 && isCatchAll(m.match)
  );

  return (
    <>
    <Panel
      title={title}
      extra={
        <div className="flex items-center gap-2">
          <Pill tone={mappers.some((m) => m.enabled) ? "green" : "gray"}>
            {mappers.filter((m) => m.enabled).length} / {mappers.length} 启用
          </Pill>
          <Button variant="outline" size="sm" onClick={showSpec} disabled={loadingSpec}>
            {loadingSpec ? "加载中…" : "标准格式说明"}
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? "收起" : "编辑"}
          </Button>
        </div>
      }
    >
      {!open && (
        <div className="px-4 py-3 text-xs text-muted-foreground">
          {mappers.length
            ? "按列表顺序对每个 trace 匹配：trace 名称通配 · tags（任一命中）· sessionId 模式；首个命中的启用条目负责该 trace 的映射，未命中任何条目时使用内置映射。"
            : "未配置映射条目。拉取会话时使用内置的 Langfuse → 进化格式映射。点击「编辑」可为不同 Agent 添加各自的映射。"}
        </div>
      )}
      {open && (
        <div className="space-y-4 p-4">
          {!isAdmin && (
            <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
              当前账号不是管理员，只能查看映射配置，无法保存或测试。
            </div>
          )}
          <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
            每个条目是一段 Python 代码，可定义{" "}
            <code className="mono">map_trace(trace, observations)</code>（返回部分 turn
            字典，深合并到内置映射；返回 <code className="mono">None</code> 表示用内置映射）和可选的{" "}
            <code className="mono">map_session(converted, session, traces)</code>{" "}
            会话钩子（返回部分会话字典，可覆盖 <code className="mono">user_alias</code> /{" "}
            <code className="mono">title</code> 等）。可用{" "}
            <code className="mono">json / re / math / datetime / collections / itertools / functools</code>
            ，出于安全考虑禁用了 <code className="mono">import</code> 与文件访问。
          </div>
          {shadowedIndex >= 0 && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              条目「{mappers[shadowedIndex].name}」没有配置任何匹配条件（兜底），会遮蔽其后的所有条目——兜底条目应放在列表最后。
            </div>
          )}
          <div className="grid gap-4 lg:grid-cols-[240px_1fr]">
            {/* Left: entry list */}
            <div className="space-y-2">
              {mappers.length === 0 && (
                <div className="rounded-lg border border-dashed border-border p-3 text-xs text-muted-foreground">
                  暂无条目。点击下方「新增映射」开始。
                </div>
              )}
              {mappers.map((m, i) => (
                <div
                  key={i}
                  className={`cursor-pointer rounded-lg border p-2.5 text-xs transition-colors ${
                    i === selected
                      ? "border-primary bg-primary/5"
                      : "border-border hover:bg-background/80"
                  }`}
                  onClick={() => setSelected(i)}
                >
                  <div className="flex items-center gap-1.5">
                    <span className="truncate font-semibold">{m.name || `entry-${i + 1}`}</span>
                    <Pill tone={m.enabled ? "green" : "gray"}>{m.enabled ? "启用" : "停用"}</Pill>
                  </div>
                  <div className="mt-1 truncate text-[11px] text-muted-foreground">
                    {describeMatch(m.match)}
                  </div>
                  {isAdmin && (
                    <div className="mt-1.5 flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-5 px-1.5"
                        disabled={i === 0}
                        onClick={(e) => {
                          e.stopPropagation();
                          moveEntry(i, -1);
                        }}
                      >
                        ↑
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-5 px-1.5"
                        disabled={i === mappers.length - 1}
                        onClick={(e) => {
                          e.stopPropagation();
                          moveEntry(i, 1);
                        }}
                      >
                        ↓
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="ml-auto h-5 px-1.5 text-red-600"
                        onClick={(e) => {
                          e.stopPropagation();
                          removeEntry(i);
                        }}
                      >
                        删除
                      </Button>
                    </div>
                  )}
                </div>
              ))}
              {isAdmin && (
                <Button variant="outline" size="sm" className="w-full" onClick={addEntry}>
                  + 新增映射
                </Button>
              )}
            </div>
            {/* Right: selected entry editor */}
            {entry ? (
              <div className="space-y-3">
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField label="条目名称">
                    <Input
                      disabled={!isAdmin}
                      value={entry.name}
                      onChange={(e) => patchEntry({ name: e.target.value })}
                      placeholder="agent-foo"
                    />
                  </FormField>
                  <FormField label="备注">
                    <Input
                      disabled={!isAdmin}
                      value={entry.note || ""}
                      onChange={(e) => patchEntry({ note: e.target.value })}
                      placeholder="该 Agent 的数据形态说明（可选）"
                    />
                  </FormField>
                </div>
                <label className="flex items-center gap-2 text-sm font-semibold">
                  <input
                    type="checkbox"
                    disabled={!isAdmin}
                    checked={entry.enabled}
                    onChange={(e) => patchEntry({ enabled: e.target.checked })}
                  />
                  启用该条目
                </label>
                <div className="grid gap-3 sm:grid-cols-2">
                  <FormField
                    label="Trace 名称模式（逗号分隔）"
                    hint="fnmatch 通配，如 openclaw-turn、agent-*"
                  >
                    <Input
                      disabled={!isAdmin}
                      value={(entry.match?.trace_names || []).join(", ")}
                      onChange={(e) => patchMatch({ trace_names: splitList(e.target.value) })}
                      placeholder="openclaw-turn"
                    />
                  </FormField>
                  <FormField label="Tags（逗号分隔，任一命中即可）">
                    <Input
                      disabled={!isAdmin}
                      value={(entry.match?.tags || []).join(", ")}
                      onChange={(e) => patchMatch({ tags: splitList(e.target.value) })}
                      placeholder="openclaw"
                    />
                  </FormField>
                </div>
                <FormField
                  label="SessionId 模式（每行一条）"
                  hint="fnmatch 通配，前缀匹配写 prefix*；模式本身可含逗号，故不按逗号拆分。"
                >
                  <Textarea
                    disabled={!isAdmin}
                    value={(entry.match?.session_id_patterns || []).join("\n")}
                    onChange={(e) =>
                      patchMatch({
                        session_id_patterns: e.target.value
                          .split("\n")
                          .map((s) => s.trim())
                          .filter(Boolean),
                      })
                    }
                    placeholder={"agent:main:openresponses-user:42749155_*"}
                    className="mono h-20 text-xs"
                  />
                </FormField>
                <FormField label="映射代码（Python）">
                  <Textarea
                    disabled={!isAdmin}
                    value={entry.code}
                    spellCheck={false}
                    onChange={(e) => patchEntry({ code: e.target.value })}
                    placeholder={"def map_trace(trace, observations):\n    return {\"prompt_text\": str(trace.get(\"input\") or \"\")}"}
                    className="mono h-64 text-xs"
                  />
                </FormField>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={runTest} disabled={!isAdmin || testing}>
                    {testing ? "映射中…" : "试运行该条目"}
                  </Button>
                  <Button variant="ghost" size="sm" onClick={insertTemplate} disabled={!isAdmin || loadingTpl}>
                    {loadingTpl ? "加载中…" : "插入参考模板"}
                  </Button>
                </div>
                {result && (
                  <div className="space-y-2">
                    {result.ok ? (
                      <>
                        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                          <span>
                            映射成功 · {result.used_sample ? "内置样例" : "自定义 trace"} · observations:{" "}
                            {result.observation_count ?? "—"}
                          </span>
                          {result.match && (
                            <Pill tone={result.match.matched ? "green" : "red"}>
                              {result.match.matched ? "路由命中" : "未命中"}
                            </Pill>
                          )}
                        </div>
                        {result.match && !result.match.matched && !!result.match.unmet?.length && (
                          <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                            {result.match.unmet.join("；")}
                          </div>
                        )}
                        {result.note && (
                          <div className="text-xs text-muted-foreground">{result.note}</div>
                        )}
                        <div className="grid gap-3 lg:grid-cols-2">
                          <MapperResultBlock title="映射结果（标准格式 turn）" value={result.turn} />
                          <MapperResultBlock title="内置映射（对照）" value={result.builtin} />
                        </div>
                      </>
                    ) : (
                      <div className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700 whitespace-pre-wrap">
                        {result.error || "映射失败"}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex items-center justify-center rounded-lg border border-dashed border-border p-6 text-xs text-muted-foreground">
                左侧选择或新增一个映射条目开始编辑。
              </div>
            )}
          </div>
          <FormField
            label="路由预览用 trace（JSON，可留空使用内置样例）"
            hint="支持 {trace, observations} 或直接是内嵌 observations 的 trace 对象。"
          >
            <Textarea
              disabled={!isAdmin}
              value={traceJson}
              spellCheck={false}
              onChange={(e) => setTraceJson(e.target.value)}
              placeholder='{"trace": {...}, "observations": [...]}'
              className="mono h-32 text-xs"
            />
          </FormField>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={onSave} disabled={!isAdmin || saving}>
              {saving ? "保存中…" : "保存注册表"}
            </Button>
            <Button variant="outline" size="sm" onClick={runRoutePreview} disabled={!isAdmin || routeTesting}>
              {routeTesting ? "路由中…" : "路由预览"}
            </Button>
            <span className="ml-auto text-xs text-muted-foreground">
              保存后立即生效，无需重启服务；启用条目前会校验代码可编译。
            </span>
          </div>
          {route && (
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">
                路由预览 · {route.used_sample ? "内置样例" : "自定义 trace"} ·{" "}
                {route.matched ? (
                  <>
                    胜出条目：<span className="font-semibold text-foreground">{route.matched.name}</span>
                  </>
                ) : (
                  "无条目命中（将使用内置映射）"
                )}
              </div>
              {!!route.broken?.length && (
                <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  {route.broken.map(([name, err], i) => (
                    <div key={i}>
                      条目「{name}」编译失败，已跳过：{err}
                    </div>
                  ))}
                </div>
              )}
              <div className="overflow-hidden rounded-lg border border-border">
                <table className="w-full text-left text-xs">
                  <thead className="bg-muted/50 text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 font-semibold">#</th>
                      <th className="px-3 py-2 font-semibold">条目</th>
                      <th className="px-3 py-2 font-semibold">状态</th>
                      <th className="px-3 py-2 font-semibold">匹配</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(route.per_entry || []).map((row) => (
                      <tr key={row.index} className={row.matched ? "bg-primary/5" : ""}>
                        <td className="border-t border-line px-3 py-1.5">{row.index}</td>
                        <td className="border-t border-line px-3 py-1.5 font-medium">{row.name}</td>
                        <td className="border-t border-line px-3 py-1.5">
                          <Pill tone={row.enabled ? "green" : "gray"}>{row.enabled ? "启用" : "停用"}</Pill>
                        </td>
                        <td className="border-t border-line px-3 py-1.5">
                          {row.matched ? "命中" : "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </Panel>
    <StandardFormatDialog open={specOpen} onOpenChange={setSpecOpen} spec={spec} loading={loadingSpec} />
    </>
  );
}

// Read-only dialog documenting the standard evolution turn format that a mapper
// must produce. Content comes from GET /langfuse/mapper/template's `spec`, so it
// stays in lockstep with the server-side ingest contract.
function StandardFormatDialog({
  open,
  onOpenChange,
  spec,
  loading,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  spec: LangfuseMapperFormatSpec | null;
  loading: boolean;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[860px]">
        <DialogHeader>
          <DialogTitle>{spec?.title || "进化标准格式（Evolution Turn）"}</DialogTitle>
          {spec?.summary && <DialogDescription>{spec.summary}</DialogDescription>}
        </DialogHeader>
        {loading && <div className="py-6 text-center text-sm text-muted-foreground">加载中…</div>}
        {!loading && !spec && (
          <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            无法加载格式说明。若服务为旧版本，请重启 teamEvolver 服务后重试。
          </div>
        )}
        {!loading && spec && (
          <div className="space-y-4">
            <ListViewport maxHeight="340px">
              <table className="w-full border-collapse">
                <thead>
                  <tr>
                    {["字段", "类型", "必填", "说明"].map((h) => (
                      <th
                        key={h}
                        className="sticky top-0 border-b border-line bg-surface-subtle px-3 py-2 text-left text-xs font-semibold text-muted-foreground"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {spec.fields.map((f) => (
                    <tr key={f.key}>
                      <td className="border-b border-line px-3 py-2 align-top">
                        <code className="mono text-xs">{f.key}</code>
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs text-muted-foreground">
                        {f.type}
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs">
                        {f.required === true ? (
                          <Pill tone="red">必填</Pill>
                        ) : f.required ? (
                          <Pill tone="amber">{String(f.required)}</Pill>
                        ) : (
                          <span className="text-muted-soft">可选</span>
                        )}
                      </td>
                      <td className="border-b border-line px-3 py-2 align-top text-xs text-muted-foreground">
                        {f.desc}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ListViewport>
            <div>
              <div className="mb-1.5 text-xs font-semibold text-muted-foreground">示例（一个 turn）</div>
              <ListViewport maxHeight="280px">
                <pre className="mono whitespace-pre-wrap break-all p-3 text-[11px] leading-relaxed">
                  {JSON.stringify(spec.example, null, 2)}
                </pre>
              </ListViewport>
            </div>
          </div>
        )}
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline" size="sm">
              关闭
            </Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function MapperResultBlock({
  title,
  value,
}: {
  title: string;
  value?: Record<string, unknown>;
}) {
  return (
    <div className="rounded-lg border border-line bg-surface-subtle">
      <div className="border-b border-line px-3 py-2 text-xs font-semibold text-muted-foreground">
        {title}
      </div>
      <ListViewport maxHeight="320px">
        <pre className="mono whitespace-pre-wrap break-all p-3 text-[11px] leading-relaxed">
          {JSON.stringify(value ?? {}, null, 2)}
        </pre>
      </ListViewport>
    </div>
  );
}
