import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, RefreshCw, Search } from "lucide-react";
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
import {
  api,
  type SkillExperience,
  type SkillExperienceListResp,
} from "@/api/client";
import { fmtTime } from "@/lib/format";
import { toastErr } from "@/lib/toast";
import ExperienceSyncPanel from "./ExperienceSyncPanel";

const PAGE_SIZE = 50;

export default function ExperienceLibraryView({ active, canManageSync = false }: {
  active: boolean; canManageSync?: boolean;
}) {
  const [data, setData] = useState<SkillExperienceListResp | null>(null);
  const [kind, setKind] = useState("");
  const [skill, setSkill] = useState("");
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(0);

  const load = useCallback(async (refresh = false) => {
    if (!active) return;
    const id = ++requestId.current;
    setLoading(true);
    setError("");
    setData(null);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      });
      if (kind) params.set("kind", kind);
      if (skill) params.set("skill", skill);
      if (search) params.set("search", search);
      if (refresh) params.set("refresh", "true");
      const result = await api<SkillExperienceListResp>(`/api/skill-experiences?${params}`);
      if (id !== requestId.current) return;
      if (result.reason) throw new Error(result.reason);
      setData(result);
    } catch (error: any) {
      if (id !== requestId.current) return;
      setError(error.message || "请稍后重试");
      toastErr("加载经验库失败", error.message);
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }, [active, kind, page, search, skill]);

  useEffect(() => {
    void load();
    return () => { requestId.current += 1; };
  }, [load]);

  const applySearch = () => {
    setPage(1);
    setSearch(query.trim());
  };
  const stats = data?.stats || {};
  const total = data?.total || 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const items = data?.items || [];

  return (
    <div className="mx-auto max-w-[1200px] px-[22px] py-[22px]">
      {canManageSync && <ExperienceSyncPanel active={active} />}
      <p className="mb-4 text-xs leading-6 text-muted-foreground">
        展示 Session 分析中已生成的 Skill 错误经验和优秀实践，不要求对应 Skill 已上传到平台。
      </p>
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="经验条目" value={stats.total_experiences ?? "—"} />
        <StatCard label="错误发生" value={stats.defect_occurrences ?? "—"} />
        <StatCard label="优秀实践" value={stats.exemplary_occurrences ?? "—"} />
        <StatCard label="涉及 Skill" value={stats.skills ?? "—"} />
      </div>

      <Panel
        title="Skill 使用经验"
        count={`${total} 条`}
        extra={
          <Button variant="outline" size="sm" onClick={() => void load(true)} disabled={loading}>
            <RefreshCw className={loading ? "size-3.5 animate-spin" : "size-3.5"} />
            {loading ? "刷新中…" : "刷新"}
          </Button>
        }
      >
        <div className="flex flex-wrap items-end gap-2 border-b border-line bg-surface-subtle/50 p-3">
          <label className="block min-w-[150px]">
            <span className="mb-1 block text-[11px] font-semibold text-muted-foreground">类型</span>
            <select
              value={kind}
              onChange={(event) => {
                setKind(event.target.value);
                setPage(1);
              }}
              className="h-8 w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
            >
              <option value="">全部经验</option>
              <option value="defect">错误经验</option>
              <option value="exemplary">优秀实践</option>
            </select>
          </label>
          <label className="block min-w-[220px]">
            <span className="mb-1 block text-[11px] font-semibold text-muted-foreground">Skill</span>
            <select
              value={skill}
              onChange={(event) => {
                setSkill(event.target.value);
                setPage(1);
              }}
              className="h-8 w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
            >
              <option value="">全部 Skill</option>
              {Object.entries(data?.skill_counts || {}).map(([name, count]) => (
                <option key={name} value={name}>{name} ({count})</option>
              ))}
            </select>
          </label>
          <label className="min-w-[260px] flex-1">
            <span className="mb-1 block text-[11px] font-semibold text-muted-foreground">搜索</span>
            <Input
              value={query}
              placeholder="搜索经验或 Skill"
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
            加载经验库失败：{error}。请点击刷新重试。
          </div>
        ) : !items.length ? (
          <Empty>
            {loading || !data
              ? "正在加载经验库…"
              : kind || skill || search
                ? "暂无匹配的经验，请调整筛选条件。"
                : "暂无 Skill 使用经验。接入包含 used_skills 的 Session 并完成分析后，即使 Skill 尚未上传，经验也会自动出现在这里。"}
          </Empty>
        ) : (
          <>
            <ListViewport maxHeight="680px">
              <div className="divide-y divide-line">
                {items.map((item) => (
                  <ExperienceRow key={item.id} item={item} />
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

function ExperienceRow({ item }: { item: SkillExperience }) {
  const isDefect = item.kind === "defect";
  return (
    <article className="px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {isDefect ? (
            <AlertTriangle className="size-4 shrink-0 text-red-600" />
          ) : (
            <CheckCircle2 className="size-4 shrink-0 text-green-600" />
          )}
          <span className="mono break-all text-xs font-semibold">{item.skill_name}</span>
          <Pill tone={isDefect ? "red" : "green"}>{isDefect ? "错误经验" : "优秀实践"}</Pill>
        </div>
        <Pill tone={isDefect ? "amber" : "blue"}>累计 {item.occurrence_count} 次</Pill>
      </div>
      <p className="mt-3 text-[13px] leading-7 text-foreground">{item.description}</p>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted-foreground">
        <span>首次 {fmtTime(item.first_observed_at)}</span>
        <span>最近 {fmtTime(item.last_observed_at)}</span>
        {item.last_ingested_at ? <span>最近入库 {fmtTime(item.last_ingested_at)}</span> : null}
        <span>涉及用户 {(item.user_aliases || []).length}</span>
        {item.latest_score != null ? <span>最近评分 {item.latest_score.toFixed(2)}</span> : null}
      </div>
      {(item.session_ids || []).length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {(item.session_ids || []).slice(-6).map((sessionId) => (
            <span
              key={sessionId}
              className="mono rounded-md border border-border bg-surface-subtle px-2 py-1 text-[10px] text-muted-foreground"
            >
              {sessionId}
            </span>
          ))}
          {(item.session_ids || []).length > 6 ? (
            <span className="px-1 py-1 text-[10px] text-muted-foreground">
              等 {(item.session_ids || []).length} 个会话
            </span>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
