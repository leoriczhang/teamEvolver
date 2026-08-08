import { useCallback, useEffect, useRef, useState } from "react";
import { Panel, StatCard, Pill, Dot } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/api/client";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import DropZone from "@/views/skills/DropZone";
import {
  Boxes,
  ScanSearch,
  FileCode2,
  RotateCcw,
  Play,
  Square,
  Upload,
  CheckCircle2,
  ArrowRight,
  FileText,
} from "lucide-react";

/**
 * MiningView — SkillMiner「文档 → Skill」挖掘流水线控制台。
 *
 * 集成后 SkillGene 统一控制台「挖掘」分组的落地页。挖掘能力在左侧边栏拆成
 * 5 个独立菜单项（总览 / 知识源 / 挖掘流水线 / 挖掘任务 / 模型配置），本组件
 * 按 `page` 渲染对应页面。
 *
 * 后端已接入：SkillGene 服务把内嵌的 SkillMiner 控制台（子进程）反向代理到
 * ``/api/mining/*``。本视图通过 ``/api/mining/config`` 拉配置、``/api/mining/events``
 * (SSE) 接收实时进度、``/api/mining/run`` 与 ``/api/mining/stop`` 驱动流水线。
 */

export type MinePage = "overview" | "sources" | "pipeline" | "jobs" | "model";

const PAGE_META: Record<MinePage, { title: string; desc: string }> = {
  overview: {
    title: "挖掘总览",
    desc: "SkillMiner 文档挖掘的整体进度、近期动态与流水线运行状态。",
  },
  sources: {
    title: "知识源",
    desc: "上传并管理用于挖掘的领域文档，然后选择要进入流水线的知识源。",
  },
  pipeline: {
    title: "挖掘流水线",
    desc: "配置并运行「样本包构建 → 语义发现 → Skill 编译」流水线。",
  },
  jobs: {
    title: "挖掘任务",
    desc: "检查编译产物与内部 Benchmark，并提交到 SkillGene 候选评审与人工发布流程。",
  },
  model: {
    title: "挖掘模型",
    desc: "查看 SkillMiner 当前实际生效的 Hermes 模型配置。",
  },
};

// ---- Backend types (subset of SkillMiner /api/config & SSE events) -------- //

interface InputSource {
  path: string;
  document_count: number;
  total_bytes: number;
  ready: boolean;
}

interface CompiledSkillDetail {
  name: string;
  has_skill: boolean;
  has_evaluation: boolean;
  has_benchmark: boolean;
  question_count: number;
}

interface MinedSkillLifecycle {
  name: string;
  status: "incomplete" | "ready" | "candidate" | "published" | "rejected";
  job_id: string;
  question_count: number;
  dataset_format: string;
  registered: boolean;
  error?: string;
}

interface MiningConfig {
  input_dirs: string[];
  input_sources: InputSource[];
  default_input_dir: string;
  max_rounds_default: number;
  max_rounds_range: [number, number];
  model: { id: string; base_url: string };
  compiled_skills: string[];
  compiled_skill_details: CompiledSkillDetail[];
  benchmark: {
    default_dist: string;
    default_total: number;
  };
  checkpoints: { key: string; label: string; desc: string }[];
}

interface KnowledgeUploadResult {
  ok: boolean;
  written: Array<{
    name: string;
    path: string;
    size_bytes: number;
    renamed: boolean;
    source_encoding: string;
  }>;
  source: InputSource;
}

type RunState = "idle" | "running" | "waiting" | "done" | "error";

interface PhaseState {
  step1: "idle" | "active" | "done";
  step2: "idle" | "active" | "done";
  step3: "idle" | "active" | "done";
}

interface RoundEval {
  round: number;
  confidence?: string | number;
  gap_count?: number;
  gaps?: string[];
  question_count?: number;
}

interface MiningQuestionItem {
  qid: string;
  dimension?: string;
  severity?: string;
  question: string;
}

interface MiningQuestion {
  id: string;
  checkpoint?: string;
  round?: number;
  title?: string;
  intro?: string;
  allow_stop?: boolean;
  questions: MiningQuestionItem[];
}

const STEP_META = [
  { key: "step1" as const, n: 1, title: "样本包构建", sub: "输入文档 → sample_packages", icon: Boxes },
  { key: "step2" as const, n: 2, title: "语义发现", sub: "决策单元 + 知识缺口", icon: ScanSearch },
  { key: "step3" as const, n: 3, title: "Skill 编译", sub: "SKILL.md + EVALUATION.md", icon: FileCode2 },
];

/**
 * Shared hook: owns the SkillMiner config + live SSE state. Instantiated once
 * per MiningView mount and kept alive across page switches (the outer <main>
 * hides views with CSS rather than unmounting).
 */
function useMining(active: boolean) {
  const [config, setConfig] = useState<MiningConfig | null>(null);
  const [lifecycle, setLifecycle] = useState<MinedSkillLifecycle[]>([]);
  const [state, setState] = useState<RunState>("idle");
  const [logs, setLogs] = useState<string[]>([]);
  const [phase, setPhase] = useState<PhaseState>({ step1: "idle", step2: "idle", step3: "idle" });
  const [round, setRound] = useState(0);
  const [evals, setEvals] = useState<RoundEval[]>([]);
  const [question, setQuestion] = useState<MiningQuestion | null>(null);
  const [benchmarkState, setBenchmarkState] = useState<RunState>("idle");
  const esRef = useRef<EventSource | null>(null);
  // 已处理的最大事件 seq：SkillMiner 事件流带单调 seq，用于去重
  // （浏览器自动重连虽有 Last-Event-ID 续传，这里再兜一层防重放翻倍）。
  const lastSeq = useRef(0);
  const streamId = useRef("");

  const refreshConfig = useCallback(async () => {
    try {
      const next = await api<MiningConfig>("/api/mining/config");
      setConfig(next);
      try {
        const handoff = await api<{ skills: MinedSkillLifecycle[] }>("/api/mined-skills");
        setLifecycle(handoff.skills || []);
      } catch (e: any) {
        toastErr("加载候选交接状态失败", e.message);
      }
      return next;
    } catch (e: any) {
      toastErr("加载挖掘配置失败", e.message);
      return null;
    }
  }, []);

  // 每次重新进入挖掘页面都重连 SSE；离开页面时连接会关闭。lastSeq 会保留，
  // 因此服务端历史重放只补齐离开期间的新事件，不会把旧日志重复追加。
  useEffect(() => {
    if (!active) return;

    refreshConfig();

    const es = new EventSource("/api/mining/events");
    esRef.current = es;
    es.onmessage = (ev) => {
      let data: any;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      // SkillMiner 子进程重启后 seq 会从 1 重新开始；stream_id 用于识别新进程，
      // 避免沿用旧 lastSeq 而把新进程的全部事件误判为重放。
      if (typeof data.stream_id === "string" && data.stream_id !== streamId.current) {
        streamId.current = data.stream_id;
        lastSeq.current = 0;
      }
      if (typeof data.seq === "number") {
        if (data.seq <= lastSeq.current) return; // 重放/重连的旧事件，跳过
        lastSeq.current = data.seq;
      }
      switch (data.type) {
        case "reset":
          // 新任务开始（可能由其他客户端触发）：清空对应面板
          setLogs([]);
          if (data.scope !== "benchmark") {
            setEvals([]);
            setPhase({ step1: "idle", step2: "idle", step3: "idle" });
            setRound(0);
            setQuestion(null);
          }
          break;
        case "status":
          if (data.state) setState(data.state as RunState);
          break;
        case "log":
          // SkillMiner 的 log 事件字段是 msg（兼容旧的 line 命名）
          {
            const line = typeof data.msg === "string" ? data.msg : data.line;
            if (typeof line === "string") {
              setLogs((prev) => [...prev.slice(-400), line]);
            }
          }
          break;
        case "round_start":
          setRound(Number(data.round || 0));
          setPhase({ step1: "idle", step2: "idle", step3: "idle" });
          break;
        case "phase": {
          // SkillMiner 的 phase 事件字段是 phase/state（兼容旧的 step/status 命名）
          const step = (data.phase ?? data.step) as keyof PhaseState;
          const st = (data.state ?? data.status) === "done" ? "done" : "active";
          if (step === "step1" || step === "step2" || step === "step3") {
            setPhase((prev) => ({ ...prev, [step]: st }));
          }
          break;
        }
        case "round_eval":
          setEvals((prev) => [
            ...prev,
            {
              round: Number(data.round || 0),
              confidence: data.confidence,
              gap_count: data.gap_count,
              gaps: data.gaps,
              question_count: data.question_count,
            },
          ]);
          break;
        case "question":
          // 检查点提问：阻塞流水线直到 /api/mining/answer 回填
          if (data.id && Array.isArray(data.questions)) {
            setQuestion(data as MiningQuestion);
          }
          break;
        case "answer_ack":
          // 与 question 成对：正常应答与中止路径都会补发，弹出的问答卡片在此关闭
          setQuestion(null);
          break;
        case "done":
          setState("done");
          setPhase({ step1: "done", step2: "done", step3: "done" });
          setQuestion(null);
          // 编译完成后刷新 compiled_skills，任务页无需手动刷新浏览器。
          refreshConfig();
          break;
        case "bench_status":
          if (data.state) setBenchmarkState(data.state as RunState);
          break;
        case "bench_done":
          setBenchmarkState("done");
          refreshConfig();
          break;
        case "bench_error":
          setBenchmarkState("error");
          {
            const msg = data.msg ?? data.message;
            if (msg) setLogs((prev) => [...prev.slice(-400), `[错误] ${msg}`]);
          }
          break;
        case "error":
          setState("error");
          {
            const msg = data.msg ?? data.message;
            if (msg) setLogs((prev) => [...prev.slice(-400), `[错误] ${msg}`]);
          }
          break;
        case "warning":
          // 非致命告警（如本轮部分步骤失败）：显式标注但不切 error 状态，
          // 流水线仍在继续。
          {
            const msg = data.msg ?? data.message;
            if (msg) setLogs((prev) => [...prev.slice(-400), `[告警] ${msg}`]);
          }
          break;
      }
    };
    es.onerror = () => {
      // Browser auto-reconnects (with Last-Event-ID); nothing to do.
    };
    return () => {
      es.close();
      esRef.current = null;
    };
  }, [active, refreshConfig]);

  return {
    config, lifecycle, state, logs, phase, round, evals, question, benchmarkState,
    setLogs, setState, setPhase, setRound, setEvals, setQuestion, setBenchmarkState, refreshConfig,
  };
}

export default function MiningView({
  active,
  page,
  preferredInputDir,
  onInputDirChange,
  onNavigate,
}: {
  active: boolean;
  page: MinePage;
  preferredInputDir?: string;
  onInputDirChange?: (path: string) => void;
  onNavigate?: (destination: MinePage | "candidates" | "skills") => void;
}) {
  const mining = useMining(active);
  const { config, state, logs, phase, round, evals, question } = mining;

  const [rounds, setRounds] = useState(3);
  const [askEnabled, setAskEnabled] = useState(true);
  const [starting, setStarting] = useState(false);
  const [benchmarkStarting, setBenchmarkStarting] = useState(false);
  const [benchmarkingSkill, setBenchmarkingSkill] = useState("");
  const [submittingSkill, setSubmittingSkill] = useState("");
  const [uploading, setUploading] = useState(false);
  const [selectedInputDir, setSelectedInputDir] = useState("");
  const logRef = useRef<HTMLDivElement | null>(null);

  // Sync default rounds from config once loaded.
  useEffect(() => {
    if (!config) return;
    if (config.max_rounds_default) setRounds(config.max_rounds_default);
    const next = preferredInputDir && config.input_dirs.includes(preferredInputDir)
      ? preferredInputDir
      : selectedInputDir && config.input_dirs.includes(selectedInputDir)
        ? selectedInputDir
        : config.default_input_dir || config.input_dirs[0] || "data/input";
    setSelectedInputDir(next);
    onInputDirChange?.(next);
  }, [config, preferredInputDir]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-scroll the log console.
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  if (!active) return null;

  const meta = PAGE_META[page];
  const running = state === "running" || state === "waiting";
  const inputDir = selectedInputDir || config?.default_input_dir || "data/input";
  const maxRounds = config?.max_rounds_range?.[1] ?? 5;
  const minRounds = config?.max_rounds_range?.[0] ?? 1;
  const skills = config?.compiled_skills ?? [];
  const inputSources = config?.input_sources ?? [];
  const selectedSource = inputSources.find((source) => source.path === inputDir);
  const compiledSkills = config?.compiled_skill_details
    ?? skills.map((name) => ({ name, has_skill: true, has_evaluation: false, has_benchmark: false, question_count: 0 }));
  const lifecycleByName = new Map(mining.lifecycle.map((item) => [item.name, item]));
  const benchmarkRunning = mining.benchmarkState === "running" || mining.benchmarkState === "waiting";

  function selectInputDir(path: string) {
    setSelectedInputDir(path);
    onInputDirChange?.(path);
  }

  async function uploadKnowledgeFiles(list: FileList) {
    if (!list.length) return;
    setUploading(true);
    try {
      const files = await Promise.all(Array.from(list).map(async (file) => ({
        name: file.name,
        content_b64: await fileToB64(file),
      })));
      const result = await api<KnowledgeUploadResult>("/api/mining/sources/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_path: inputDir, files }),
      });
      const renamed = result.written.filter((file) => file.renamed).length;
      toastOk(
        `已上传 ${result.written.length} 个文档`,
        renamed ? `${renamed} 个重名文件已自动保留为新副本` : `已写入 ${result.source.path}`
      );
      await mining.refreshConfig();
    } catch (e: any) {
      toastErr("上传知识文档失败", e.message);
    } finally {
      setUploading(false);
    }
  }

  async function startRun() {
    setStarting(true);
    mining.setLogs([]);
    mining.setEvals([]);
    mining.setPhase({ step1: "idle", step2: "idle", step3: "idle" });
    try {
      await api("/api/mining/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          input_dir: inputDir,
          max_rounds: rounds,
          ask_enabled: askEnabled,
          checkpoints: {
            after_semantic: askEnabled,
            after_compile: askEnabled,
            on_gap_low_confidence: askEnabled,
            before_reflection: false,
          },
        }),
      });
      mining.setState("running");
      toastOk("挖掘已启动", `输入 ${inputDir} · 最多 ${rounds} 轮`);
    } catch (e: any) {
      toastErr("启动挖掘失败", e.message);
    } finally {
      setStarting(false);
    }
  }

  async function stopRun() {
    try {
      await api("/api/mining/stop", { method: "POST" });
      toastOk("已发送中止信号", "");
    } catch (e: any) {
      toastErr("中止失败", e.message);
    }
  }

  async function buildBenchmark(skillName: string) {
    if (!config) return;
    setBenchmarkStarting(true);
    setBenchmarkingSkill(skillName);
    mining.setBenchmarkState("running");
    mining.setLogs([]);
    try {
      await api("/api/mining/benchmark", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skill_name: skillName,
          difficulty_dist: config.benchmark.default_dist,
          target_total: config.benchmark.default_total,
          skip_build: false,
          build_only: true,
        }),
      });
      toastOk("Benchmark 生成已启动", `${skillName} · ${config.benchmark.default_total} 题`);
    } catch (e: any) {
      mining.setBenchmarkState("error");
      toastErr("启动 Benchmark 生成失败", e.message);
    } finally {
      setBenchmarkStarting(false);
    }
  }

  async function submitCandidate(skillName: string) {
    setSubmittingSkill(skillName);
    try {
      const result = await api<{ created: boolean; job_id: string; question_count: number }>(
        `/api/mined-skills/${encodeURIComponent(skillName)}/submit`,
        { method: "POST" }
      );
      toastOk(
        result.created ? "已提交候选评审" : "候选已在评审流程中",
        `${skillName} · ${result.question_count || 0} 道内部 Benchmark`
      );
      await mining.refreshConfig();
      onNavigate?.("candidates");
    } catch (e: any) {
      toastErr("提交候选评审失败", e.message);
    } finally {
      setSubmittingSkill("");
    }
  }

  /** 回答检查点提问：必须带 question_id，后端会拒绝过期/不匹配的提交。 */
  async function submitAnswers(answers: Record<string, string>) {
    if (!question) return;
    const questionId = question.id;
    try {
      const resp = await api<{ ok: boolean; msg?: string }>("/api/mining/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question_id: questionId, answers, stop: false }),
      });
      if (!resp.ok) {
        toastErr("提交答案未被接受", resp.msg || "");
        return;
      }
      // 正常情况下由 answer_ack 关闭；这里兜底处理事件流短暂断线。
      mining.setQuestion((current) => current?.id === questionId ? null : current);
    } catch (e: any) {
      toastErr("提交答案失败", e.message);
    }
  }

  const stateTone = state === "running" ? "blue" : state === "waiting" ? "amber" : state === "done" ? "green" : state === "error" ? "red" : "gray";
  const stateLabel = { idle: "空闲", running: "运行中", waiting: "等待人工", done: "已完成", error: "出错" }[state];

  return (
    <div>
      {/* Page header */}
      <div className="border-b border-line bg-surface px-7 py-5">
        <h1 className="flex items-center gap-2.5 text-[22px] font-bold tracking-tight">
          {meta.title}
          <Pill tone="purple">SkillMiner</Pill>
          {running && <Pill tone="blue">运行中{round ? ` · 第 ${round} 轮` : ""}</Pill>}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">{meta.desc}</p>
      </div>

      {/* ---- 总览 ---- */}
      {page === "overview" && (
        <div className="px-7 py-6">
          <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
            <StatCard label="已编译技能" value={String(skills.length)} />
            <StatCard label="输入文档" value={config ? String(inputSources.reduce((sum, source) => sum + source.document_count, 0)) : "—"} />
            <StatCard label="运行状态" value={stateLabel} />
            <StatCard label="当前轮次" value={round ? String(round) : "—"} />
          </div>

          <div className="grid gap-6 lg:grid-cols-2">
            <Panel title="流水线状态" extra={<Pill tone={stateTone as any}>{stateLabel}</Pill>}>
              <div className="space-y-3 p-4">
                {STEP_META.map((s) => {
                  const st = phase[s.key];
                  return (
                    <div key={s.key} className="flex items-center gap-3">
                      <span
                        className={cn(
                          "grid size-7 shrink-0 place-items-center rounded-lg text-[13px] font-extrabold text-white",
                          st === "idle" ? "bg-muted-soft" : "bg-accent"
                        )}
                      >
                        {s.n}
                      </span>
                      <div className="min-w-0 flex-1">
                        <div className="text-[13px] font-semibold">{s.title}</div>
                        <div className="mono text-[11px] text-muted-soft">{s.sub}</div>
                      </div>
                      {st === "active" && <Pill tone="blue">进行中</Pill>}
                      {st === "done" && <CheckCircle2 className="size-4 text-success" />}
                    </div>
                  );
                })}
              </div>
            </Panel>

            <Panel title="逐轮评估" count={evals.length ? `${evals.length} 轮` : undefined}>
              {evals.length === 0 ? (
                <div className="p-6 text-center text-sm text-muted-soft">
                  暂无评估数据，运行一次挖掘后展示每轮的置信度与缺口数。
                </div>
              ) : (
                <ul className="divide-y divide-line">
                  {evals.map((e, i) => (
                    <li key={i} className="flex items-center justify-between px-4 py-3 text-[13px]">
                      <span className="font-semibold">第 {e.round} 轮</span>
                      <span className="flex items-center gap-3 text-muted-foreground">
                        <span>置信 {e.confidence ?? "—"}</span>
                        <span>缺口 {e.gap_count ?? "—"}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Panel>
          </div>
        </div>
      )}

      {/* ---- 知识源 ---- */}
      {page === "sources" && (
        <div className="px-7 py-6">
          <Panel
            title="上传本地文档"
            extra={<Pill tone={running ? "amber" : "green"}>{running ? "任务运行中" : "可上传"}</Pill>}
          >
            <div className="grid gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_260px]">
              <div>
                <DropZone
                  multiple
                  accept=".md,.markdown,.txt,.rst,.csv,.tsv,.json,.jsonl,.yaml,.yml,.xml,.html,.htm"
                  disabled={running || uploading || !config}
                  onFiles={uploadKnowledgeFiles}
                  label={
                    running
                      ? "挖掘运行中，暂不能修改输入文档"
                      : uploading
                        ? "正在上传并校验文档…"
                        : "点击选择或将多个知识文档拖拽到这里"
                  }
                />
                <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
                  支持 Markdown、TXT、RST、CSV/TSV、JSON/JSONL、YAML、XML 和 HTML；
                  单文件不超过 10 MB，单次最多 50 个、合计不超过 40 MB。
                </p>
              </div>

              <div className="rounded-lg border border-border bg-background/60 p-3.5">
                <Label htmlFor="knowledge-source-target" className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  上传到知识源
                </Label>
                <select
                  id="knowledge-source-target"
                  value={inputDir}
                  disabled={running || uploading || !config}
                  onChange={(e) => selectInputDir(e.target.value)}
                  className="mono w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                >
                  {(config?.input_dirs ?? [inputDir]).map((dir) => (
                    <option key={dir} value={dir}>{dir}</option>
                  ))}
                </select>
                <div className="mt-3 flex items-start gap-2 text-[11px] leading-relaxed text-muted-foreground">
                  <CheckCircle2 className="mt-0.5 size-3.5 shrink-0 text-success" />
                  <span>不会覆盖同名文件；系统会自动添加序号，并统一保存为 UTF-8 文本。</span>
                </div>
              </div>
            </div>
          </Panel>

          <Panel
            title="知识源 · 输入目录"
            count={config ? `${inputSources.length} 个目录` : undefined}
          >
            {!config ? (
              <div className="p-6 text-center text-sm text-muted-soft">加载中…</div>
            ) : inputSources.length === 0 ? (
              <div className="p-6 text-center text-sm text-muted-soft">
                尚无输入目录。请在 SkillMiner 的 data/ 下放置待挖掘文档。
              </div>
            ) : (
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-line text-[11px] uppercase tracking-wide text-muted-foreground">
                    <th className="px-4 py-2.5 text-left font-semibold">目录</th>
                    <th className="px-4 py-2.5 text-left font-semibold">文档</th>
                    <th className="px-4 py-2.5 text-left font-semibold">状态</th>
                    <th className="px-4 py-2.5 text-right font-semibold">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {inputSources.map((source) => (
                    <tr key={source.path} className="border-b border-line last:border-none">
                      <td className="px-4 py-3">
                        <div className="mono font-semibold">{source.path}</div>
                        <div className="mt-0.5 text-[11px] text-muted-foreground">
                          {source.path === config.default_input_dir ? "默认挖掘输入" : "候选输入"}
                        </div>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {source.document_count} 个 · {formatBytes(source.total_bytes)}
                      </td>
                      <td className="px-4 py-3">
                        <Pill tone={source.ready ? "green" : "amber"}>{source.ready ? "可挖掘" : "空目录"}</Pill>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!source.ready}
                          onClick={() => {
                            selectInputDir(source.path);
                            onNavigate?.("pipeline");
                          }}
                        >
                          用于挖掘 <ArrowRight className="size-3.5" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      )}

      {/* ---- 挖掘流水线 ---- */}
      {page === "pipeline" && (
        <div className="grid gap-6 px-7 py-6 lg:grid-cols-[300px_1fr]">
          {/* config panel */}
          <Panel title="配置">
            <div className="space-y-4 p-4">
              <label className="block">
                <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  输入文档目录
                </span>
                {config && config.input_dirs.length > 1 ? (
                  <select
                    value={inputDir}
                    disabled={running}
                    onChange={(e) => selectInputDir(e.target.value)}
                    className="mono w-full rounded-lg border border-border bg-background px-3 py-2 text-[13px] outline-none focus:border-accent disabled:opacity-60"
                  >
                    {config.input_dirs.map((dir) => (
                      <option key={dir} value={dir}>{dir}</option>
                    ))}
                  </select>
                ) : (
                  <div className="mono rounded-lg border border-border bg-background px-3 py-2 text-[13px]">
                    {inputDir}
                  </div>
                )}
                <div className={cn("mt-1.5 text-[11px]", selectedSource?.ready ? "text-muted-foreground" : "text-amber-700")}>
                  {!config
                    ? "正在检查目录…"
                    : selectedSource?.ready
                      ? `已检测到 ${selectedSource.document_count} 个文档 · ${formatBytes(selectedSource.total_bytes)}`
                      : "该目录没有非隐藏文档，请先放入素材后再启动。"}
                </div>
              </label>

              <label className="block">
                <span className="mb-1.5 flex items-center justify-between text-xs font-semibold text-muted-foreground">
                  反思环最大轮数
                  <span className="text-accent">{rounds}</span>
                </span>
                <input
                  type="range"
                  min={minRounds}
                  max={maxRounds}
                  value={rounds}
                  disabled={running}
                  onChange={(e) => setRounds(Number(e.target.value))}
                  className="w-full accent-[var(--accent)]"
                />
              </label>

              <label className="block">
                <span className="mb-1.5 block text-xs font-semibold text-muted-foreground">
                  模型
                </span>
                <div className="mono rounded-lg border border-border bg-background px-3 py-2 text-[11px] text-muted-foreground">
                  {config?.model?.id || "由本机 Hermes 配置"}
                </div>
              </label>

              <div className="border-t border-line pt-4">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[13px] font-semibold">人工检查点</div>
                    <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                      在关键节点暂停，补充知识 / 校验产物
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-label="人工检查点"
                    aria-checked={askEnabled}
                    onClick={() => setAskEnabled((v) => !v)}
                    disabled={running}
                    className={cn(
                      "relative h-6 w-11 shrink-0 rounded-full transition-colors disabled:opacity-50",
                      askEnabled ? "bg-accent" : "bg-muted"
                    )}
                  >
                    <span
                      className={cn(
                        "absolute top-0.5 size-5 rounded-full bg-white shadow transition-all",
                        askEnabled ? "left-[22px]" : "left-0.5"
                      )}
                    />
                  </button>
                </div>
              </div>

              <div className="flex gap-2 pt-1">
                <Button className="flex-1" onClick={startRun} disabled={running || starting || !selectedSource?.ready}>
                  <Play className="size-4" /> {starting ? "启动中…" : "开始挖掘"}
                </Button>
                <Button variant="outline" onClick={stopRun} disabled={!running}>
                  <Square className="size-4" /> 中止
                </Button>
              </div>
            </div>
          </Panel>

          {/* flow diagram + live log */}
          <div className="space-y-6">
            <Panel
              title="流程"
              extra={<Pill tone={stateTone as any}>{stateLabel}{round ? ` · 第 ${round} 轮` : ""}</Pill>}
            >
              <div className="p-5">
                <div className="grid gap-3 md:grid-cols-3">
                  {STEP_META.map((s) => {
                    const Icon = s.icon;
                    const st = phase[s.key];
                    return (
                      <div key={s.key} className="relative flex min-w-0 items-stretch">
                        <div
                          className={cn(
                            "w-full rounded-xl border p-3.5 transition-colors",
                            st === "active"
                              ? "border-accent bg-accent-soft"
                              : st === "done"
                                ? "border-border bg-surface-subtle"
                                : "border-border bg-background"
                          )}
                        >
                          <div className="mb-2 flex items-center gap-2">
                            <span
                              className={cn(
                                "grid size-7 place-items-center rounded-lg text-[13px] font-extrabold text-white",
                                st === "idle" ? "bg-muted-soft" : "bg-accent"
                              )}
                            >
                              {s.n}
                            </span>
                            <Icon className="size-4 text-muted-foreground" />
                            {st === "active" && <Dot state="run" className="ml-auto" />}
                            {st === "done" && <CheckCircle2 className="ml-auto size-4 text-success" />}
                          </div>
                          <div className="text-[13px] font-bold">{s.title}</div>
                          <div className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                            {s.sub}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>

                {/* Reflection loop */}
                <div className="mt-4 flex items-center gap-2 rounded-lg border border-dashed border-border bg-surface-subtle px-3.5 py-2.5 text-[12px] font-semibold text-muted-foreground">
                  <RotateCcw className="size-4 text-accent" />
                  反思环 · 携带缺口回跳补证（置信未收敛且有补充素材时）
                </div>

                {/* Downstream hand-off */}
                <div className="mt-4 flex items-center gap-3 rounded-lg border border-accent/30 bg-accent-soft px-4 py-3">
                  <Upload className="size-4 text-accent" />
                  <div className="text-[12.5px]">
                    <span className="font-bold text-accent">编译产物交付</span>
                    <span className="text-muted-foreground">
                      {" "}
                      — 完成后生成内部 Benchmark，进入 SkillGene A/B 验证与人工发布
                    </span>
                  </div>
                  <Button size="sm" variant="outline" className="ml-auto" onClick={() => onNavigate?.("jobs")}>
                    查看产物 <ArrowRight className="size-3.5" />
                  </Button>
                </div>
              </div>
            </Panel>

            {/* Checkpoint Q&A: pipeline blocks in "waiting" until answered */}
            {question && (
              <CheckpointCard key={question.id} q={question} onSubmit={submitAnswers} />
            )}

            {/* Live log console */}
            <Panel title="实时日志" count={logs.length ? `${logs.length} 行` : undefined}>
              <div
                ref={logRef}
                className="mono max-h-80 overflow-auto whitespace-pre-wrap break-words bg-[#0f172a] p-4 text-[11.5px] leading-relaxed text-[#cbd5e1]"
              >
                {logs.length === 0 ? (
                  <span className="text-muted-soft">等待运行… 点击「开始挖掘」后实时输出流水线日志。</span>
                ) : (
                  logs.map((l, i) => <div key={i}>{l}</div>)
                )}
              </div>
            </Panel>
          </div>
        </div>
      )}

      {/* ---- 挖掘任务 ---- */}
      {page === "jobs" && (
        <div className="px-7 py-6">
          <Panel title="已编译技能" count={compiledSkills.length ? `${compiledSkills.length} 个` : undefined}>
            {compiledSkills.length === 0 ? (
              <div className="flex flex-col items-center gap-3 p-8 text-center text-sm text-muted-soft">
                <FileText className="size-7" />
                <span>暂无已编译技能。先运行挖掘流水线，产物会自动出现在这里。</span>
                <Button size="sm" onClick={() => onNavigate?.("pipeline")}>去运行挖掘 <ArrowRight className="size-3.5" /></Button>
              </div>
            ) : (
              <table className="w-full border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-line text-[11px] uppercase tracking-wide text-muted-foreground">
                    <th className="px-4 py-2.5 text-left font-semibold">技能</th>
                    <th className="px-4 py-2.5 text-left font-semibold">编译产物</th>
                    <th className="px-4 py-2.5 text-left font-semibold">Benchmark</th>
                    <th className="px-4 py-2.5 text-left font-semibold">生命周期</th>
                    <th className="px-4 py-2.5 text-right font-semibold">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {compiledSkills.map((skill) => {
                    const handoff = lifecycleByName.get(skill.name);
                    const statusLabel = {
                      incomplete: "产物不完整",
                      ready: "待提交",
                      candidate: "待人工评审",
                      published: "已发布",
                      rejected: "未通过",
                    }[handoff?.status || "incomplete"];
                    const statusTone = handoff?.status === "published"
                      ? "green"
                      : handoff?.status === "candidate"
                        ? "blue"
                        : handoff?.status === "rejected" || handoff?.status === "incomplete"
                          ? "amber"
                          : "gray";
                    return (
                    <tr key={skill.name} className="border-b border-line last:border-none">
                      <td className="px-4 py-3 font-semibold">{skill.name}</td>
                      <td className="px-4 py-3">
                        <div className="flex flex-wrap gap-1.5">
                          <Pill tone="green">SKILL.md</Pill>
                          <Pill tone={skill.has_evaluation ? "green" : "amber"}>
                            {skill.has_evaluation ? "EVALUATION.md" : "缺少 EVALUATION.md"}
                          </Pill>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <Pill tone={skill.has_benchmark ? "green" : "gray"}>
                          {skill.has_benchmark ? `${skill.question_count} 题` : "未生成"}
                        </Pill>
                      </td>
                      <td className="px-4 py-3">
                        <Pill tone={statusTone as any}>{statusLabel}</Pill>
                        {handoff?.error && (
                          <div className="mt-1 max-w-[220px] truncate text-[11px] text-amber-700" title={handoff.error}>
                            {handoff.error}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        {handoff?.status === "candidate" || handoff?.status === "published" ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => onNavigate?.(handoff.status === "published" ? "skills" : "candidates")}
                          >
                            {handoff.status === "published" ? "查看技能" : "进入候选评审"} <ArrowRight className="size-3.5" />
                          </Button>
                        ) : handoff?.status === "rejected" ? (
                          <Button size="sm" variant="outline" disabled title="修改挖掘产物后会生成新的候选版本">
                            修改产物后重提
                          </Button>
                        ) : skill.has_benchmark ? (
                          <Button
                            size="sm"
                            disabled={submittingSkill === skill.name || handoff?.status === "incomplete"}
                            onClick={() => submitCandidate(skill.name)}
                          >
                            {submittingSkill === skill.name ? "提交中…" : "提交候选评审"} <ArrowRight className="size-3.5" />
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            disabled={!skill.has_evaluation || benchmarkRunning || benchmarkStarting}
                            onClick={() => buildBenchmark(skill.name)}
                            title={!skill.has_evaluation ? "需要 EVALUATION.md 才能生成 Benchmark" : undefined}
                          >
                            {benchmarkingSkill === skill.name && (benchmarkRunning || benchmarkStarting) ? "正在生成…" : "生成 Benchmark"}
                          </Button>
                        )}
                      </td>
                    </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </Panel>
          {benchmarkingSkill && (
            <div className="mt-6">
              <Panel
                title={`Benchmark 生成 · ${benchmarkingSkill}`}
                extra={<Pill tone={mining.benchmarkState === "done" ? "green" : mining.benchmarkState === "error" ? "red" : "blue"}>
                  {mining.benchmarkState === "done" ? "已完成" : mining.benchmarkState === "error" ? "失败" : "生成中"}
                </Pill>}
              >
                <div ref={logRef} className="mono max-h-64 min-h-28 overflow-auto whitespace-pre-wrap break-words bg-[#0f172a] p-4 text-[11.5px] leading-relaxed text-[#cbd5e1]">
                  {logs.length ? logs.map((line, index) => <div key={index}>{line}</div>) : <span className="text-muted-soft">正在准备 Benchmark 生成任务…</span>}
                </div>
                {benchmarkRunning && (
                  <div className="flex justify-end border-t border-line p-3">
                    <Button size="sm" variant="outline" onClick={stopRun}><Square className="size-3.5" />停止生成</Button>
                  </div>
                )}
              </Panel>
            </div>
          )}
        </div>
      )}

      {/* ---- 模型配置 ---- */}
      {page === "model" && (
        <div className="mx-auto max-w-[1080px] px-7 py-6">
          <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(170px,1fr))] gap-3.5">
            <StatCard label="当前模型" value={config?.model?.id || "未配置"} />
            <StatCard label="Base URL" value={config?.model?.base_url || "未配置"} mono />
            <StatCard label="凭据" value="由 Hermes 管理" />
            <StatCard label="配置模式" value="只读" />
          </div>

          <Panel
            title="挖掘模型 · 当前生效配置"
            extra={
              <span className="inline-flex items-center gap-2 text-xs text-muted-foreground">
                <Dot state={config?.model?.id ? "on" : "off"} />
                <Pill tone={config?.model?.id ? "green" : "gray"}>
                  {config?.model?.id ? "已由 Hermes 配置" : "待配置"}
                </Pill>
              </span>
            }
          >
            <div className="space-y-5 p-4">
              <div className="grid gap-3.5 md:grid-cols-2">
                <MField label="模型标识">
                  <Input value={config?.model?.id || ""} placeholder="未从 Hermes 读取到模型" readOnly disabled />
                </MField>
                <MField label="Base URL">
                  <Input value={config?.model?.base_url || ""} placeholder="未从 Hermes 读取到地址" readOnly disabled />
                </MField>
              </div>

              <MField label="API Key">
                <Input value="" placeholder="由服务端环境变量 ARK_API_KEY 管理，不在浏览器中读取或保存" readOnly disabled />
              </MField>

              <div className="rounded-lg border border-border bg-background/60 p-3 text-xs leading-relaxed text-muted-foreground">
                SkillMiner 的挖掘模型实际由本机 Hermes 安装（<span className="mono">~/.hermes/config.yaml</span> +
                <span className="mono"> ARK_API_KEY</span>）管理，与「进化」分组下的进化模型相互独立。此处展示当前生效配置，
                供核对之用。修改模型或凭据后，重启 SkillGene 服务使配置重新加载。
              </div>
            </div>
          </Panel>
        </div>
      )}
    </div>
  );
}

function MField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <Label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</Label>
      {children}
    </div>
  );
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/**
 * CheckpointCard — 渲染流水线检查点提问（缺口补充 / 语义审核 / 编译后校验），
 * 逐条作答后回传 SkillMiner（留空 = 认可采纳/跳过该条）。流水线在收到答案前
 * 保持 "waiting" 状态。
 */
function CheckpointCard({
  q,
  onSubmit,
}: {
  q: MiningQuestion;
  onSubmit: (answers: Record<string, string>) => Promise<void>;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  function collect(): Record<string, string> {
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(values)) {
      const t = v.trim();
      if (t) out[k] = t;
    }
    return out;
  }

  const sevTone = (s?: string) => (s === "高" ? "red" : s === "中" ? "amber" : "gray");

  async function submit(answers: Record<string, string>) {
    if (submitting) return;
    setSubmitting(true);
    try {
      await onSubmit(answers);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Panel
      title={q.title || "检查点 · 请补充以下内容"}
      extra={<Pill tone="amber">等待人工 · 第 {q.round ?? "—"} 轮</Pill>}
    >
      <div className="space-y-4 p-4">
        {q.intro && <p className="text-[12.5px] leading-relaxed text-muted-foreground">{q.intro}</p>}
        <div className="space-y-3">
          {q.questions.map((item, i) => (
            <div key={item.qid} className="rounded-lg border border-border bg-background p-3">
              <div className="mb-1.5 flex items-center gap-2 text-[12px]">
                <span className="grid size-5 place-items-center rounded bg-muted font-bold">{i + 1}</span>
                {item.severity && <Pill tone={sevTone(item.severity) as any}>{item.severity}严重度</Pill>}
                {item.dimension && <span className="text-muted-foreground">{item.dimension}</span>}
              </div>
              <div className="mb-2 text-[13px] leading-relaxed">{item.question}</div>
              <textarea
                rows={2}
                value={values[item.qid] || ""}
                onChange={(e) => setValues((prev) => ({ ...prev, [item.qid]: e.target.value }))}
                placeholder="填入你掌握的准确规则 / 数值 / 来源……（留空跳过此条）"
                className="w-full resize-y rounded-lg border border-border bg-surface px-3 py-2 text-[13px] outline-none focus:border-accent"
              />
            </div>
          ))}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="outline" size="sm" disabled={submitting} onClick={() => submit({})}>
            全部留空继续
          </Button>
          <Button size="sm" disabled={submitting} onClick={() => submit(collect())}>
            {submitting ? "提交中…" : "提交答案并继续"}
          </Button>
        </div>
      </div>
    </Panel>
  );
}
