import {
AlertTriangle,
CheckCircle2,
RefreshCw,
Search,
Sparkles,
} from "lucide-react";
import { useCallback,useEffect,useRef,useState } from "react";

import { api } from "@/api/client";
import {
Empty,
ListViewport,
PaginationControls,
Panel,
Pill,
StatCard,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type SkillExperiencePattern,type SkillExperiencePatternCompileResp,type SkillExperiencePatternListResp } from "@/features/skills/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { fmtTime } from "@/lib/format";
import { toastErr,toastOk } from "@/lib/toast";

const PAGE_SIZE = 50;

export default function ExperiencePatternPanel({
  active,
  canCompile,
}: {
  active: boolean;
  canCompile: boolean;
}) {
  const [data, setData] = useState<SkillExperiencePatternListResp | null>(null);
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [skill, setSkill] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [compiling, setCompiling] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(0);

  const load = useCallback(async () => {
    if (!active) return;
    const id = ++requestId.current;
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      });
      if (kind) params.set("kind", kind);
      if (status) params.set("status", status);
      if (skill) params.set("skill", skill);
      if (search) params.set("search", search);
      const result = await api<SkillExperiencePatternListResp>(
        `/api/skill-experience-patterns?${params}`,
      );
      if (id === requestId.current) setData(result);
    } catch (caught: any) {
      if (id !== requestId.current) return;
      setError(caught.message || "请稍后重试");
      toastErr("加载归纳模式失败", caught.message);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [active, kind, page, search, skill, status]);

  useEffect(() => {
    void load();
    return () => {
      requestId.current += 1;
    };
  }, [load]);

  const applySearch = () => {
    setPage(1);
    setSearch(query.trim());
  };

  const compile = async () => {
    if (!skill || compiling) return;
    setCompiling(true);
    try {
      const result = await runSkillCommand<SkillExperiencePatternCompileResp>(
        "/api/skill-experience-patterns/compile",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill }),
        },
        "/api/experience-sync",
      );
      toastOk(
        result.compiled ? "模式编译完成" : "模式无需更新",
        `${result.skill_name} · ${result.pattern_count} 条`,
      );
      setPage(1);
      await load();
    } catch (caught: any) {
      toastErr("编译归纳模式失败", caught.message);
    } finally {
      setCompiling(false);
    }
  };

  const stats = data?.stats || {};
  const total = data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const items = data?.items || [];

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[18px]">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
        <p>由 Session Experience 语义归纳，并作为 Skill Evolution 的可追溯输入。</p>
        {data?.location ? (
          <span className="mono max-w-full break-all">{data.location}</span>
        ) : null}
      </div>

      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="归纳模式" value={stats.total_patterns ?? "—"} />
        <StatCard label="团队可用" value={stats.ready_patterns ?? "—"} />
        <StatCard label="继续观察" value={stats.observing_patterns ?? "—"} />
        <StatCard label="来源经验" value={stats.source_experiences ?? "—"} />
      </div>

      <Panel
        title="Skill 经验模式"
        count={`${total} 条`}
        extra={
          <div className="flex flex-wrap gap-2">
            {canCompile ? (
              <Button
                size="sm"
                disabled={!skill || compiling}
                title={skill ? `重新编译 ${skill}` : "请先选择一个 Skill"}
                onClick={() => void compile()}
              >
                <Sparkles className="size-3.5" />
                {compiling ? "编译中…" : "编译当前 Skill"}
              </Button>
            ) : null}
            <Button
              variant="outline"
              size="sm"
              disabled={loading}
              title="刷新归纳模式"
              onClick={() => void load()}
            >
              <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
              刷新
            </Button>
          </div>
        }
      >
        <div className="flex flex-wrap items-end gap-2 border-b border-line bg-surface-subtle/50 p-3">
          <Filter label="类型" value={kind} onChange={(value) => {
            setKind(value);
            setPage(1);
          }}>
            <option value="">全部类型</option>
            <option value="defect">错误模式</option>
            <option value="exemplary">优秀模式</option>
          </Filter>
          <Filter label="状态" value={status} onChange={(value) => {
            setStatus(value);
            setPage(1);
          }}>
            <option value="">全部状态</option>
            <option value="ready">团队可用</option>
            <option value="observing">继续观察</option>
          </Filter>
          <Filter label="Skill" value={skill} wide onChange={(value) => {
            setSkill(value);
            setPage(1);
          }}>
            <option value="">全部 Skill</option>
            {(data?.available_skills || Object.keys(data?.skill_counts || {})).map((name) => (
              <option key={name} value={name}>
                {name} ({data?.skill_counts?.[name] || 0} 个模式)
              </option>
            ))}
          </Filter>
          <label className="min-w-[240px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-muted-foreground">搜索</span>
            <Input
              value={query}
              placeholder="搜索问题、根因或建议"
              className="h-8"
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") applySearch();
              }}
            />
          </label>
          <Button size="sm" onClick={applySearch}>
            <Search className="size-3.5" />
            查询
          </Button>
        </div>

        {error ? (
          <div role="alert" className="p-6 text-sm text-destructive">
            加载归纳模式失败：{error}。请稍后重试。
          </div>
        ) : !items.length ? (
          <Empty>
            {loading || !data
              ? "正在读取归纳模式…"
              : kind || status || skill || search
                ? "暂无匹配模式，请调整筛选条件。"
                : "暂无归纳模式。选择一个 Skill 并执行编译后，可在这里查看结果。"}
          </Empty>
        ) : (
          <>
            <ListViewport maxHeight="680px">
              <div className="divide-y divide-line">
                {items.map((item) => (
                  <PatternRow key={item.pattern_id} item={item} />
                ))}
              </div>
            </ListViewport>
            <PaginationControls
              page={currentPage}
              totalPages={totalPages}
              visiblePages={Math.min(10, totalPages)}
              total={total}
              start={(currentPage - 1) * PAGE_SIZE}
              end={(currentPage - 1) * PAGE_SIZE + items.length}
              onPageChange={setPage}
            />
          </>
        )}
      </Panel>
    </div>
  );
}

function Filter({
  label,
  value,
  onChange,
  children,
  wide = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <label className={wide ? "block min-w-[220px]" : "block min-w-[140px]"}>
      <span className="mb-1 block text-[11px] font-semibold text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-8 w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
      >
        {children}
      </select>
    </label>
  );
}

function PatternRow({ item }: { item: SkillExperiencePattern }) {
  const isDefect = item.kind === "defect";
  const outcomes = item.candidate_outcomes || [];
  const latestOutcome = outcomes[outcomes.length - 1];
  return (
    <article className="px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {isDefect ? (
              <AlertTriangle className="size-4 shrink-0 text-red-600" />
            ) : (
              <CheckCircle2 className="size-4 shrink-0 text-green-600" />
            )}
            <span className="text-sm font-semibold">{item.title}</span>
            <Pill tone={isDefect ? "red" : "green"}>
              {isDefect ? "错误模式" : "优秀模式"}
            </Pill>
            <Pill tone={item.team_ready ? "blue" : "gray"}>
              {item.team_ready ? "团队可用" : "继续观察"}
            </Pill>
          </div>
          <div className="mono mt-1 break-all text-[11px] text-muted-foreground">
            {item.skill_name} · {item.pattern_key}
          </div>
        </div>
        <Pill tone="amber">累计 {item.occurrence_count} 次</Pill>
      </div>

      <div className="mt-3 grid gap-3 md:grid-cols-3">
        <PatternField label="问题" value={item.problem} />
        <PatternField label="根因" value={item.root_cause} />
        <PatternField label="建议" value={item.guidance} />
      </div>

      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>经验 {item.evidence_count}</span>
        <span>会话 {item.session_count}</span>
        <span>用户 {item.user_count}</span>
        {item.updated_at ? <span>最近编译 {fmtTime(item.updated_at)}</span> : null}
        {latestOutcome ? (
          <span>
            最近候选 {latestOutcome.status}
            {latestOutcome.version ? ` · v${latestOutcome.version}` : ""}
          </span>
        ) : null}
      </div>
    </article>
  );
}

function PatternField({ label, value }: { label: string; value?: string }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[11px] font-semibold text-muted-foreground">{label}</div>
      <p className="break-words text-xs leading-6 text-foreground">{value || "—"}</p>
    </div>
  );
}
