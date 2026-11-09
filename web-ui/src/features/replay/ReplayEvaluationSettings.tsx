import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { toastOk } from "@/lib/toast";
import type { ReplayEvaluationConfig } from "./api/client";
import EvaluatorAdapterPicker from "./EvaluatorAdapterPicker";

interface Settings {
  revision: number;
  evaluation: ReplayEvaluationConfig;
}

const DEFAULT_EXTERNAL = {
  url: "", secret_ref: "", timeout_seconds: 60, rubric: "", min_score: 0, min_improvement: 0,
};

export default function ReplayEvaluationSettings({ active, isAdmin }: { active: boolean; isAdmin: boolean }) {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<ReplayEvaluationConfig>({ mode: "checklist_efficiency" });
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<"http" | "adapter">("http");
  const [error, setError] = useState("");

  async function load() {
    setBusy(true);
    setError("");
    try {
      const loaded = await api<Settings>("/api/replay-evaluation");
      setSettings(loaded);
      setDraft(loaded.evaluation);
      setConnection(loaded.evaluation.external_agent?.adapter ? "adapter" : "http");
    } catch (e: any) {
      setError(e.message || "加载评价配置失败");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (active && open && !settings) void load();
  }, [active, open]);

  const external: NonNullable<ReplayEvaluationConfig["external_agent"]> = { ...DEFAULT_EXTERNAL, ...draft.external_agent };
  const externalMode = draft.mode === "external_agent";
  const dirty = !!settings && JSON.stringify(draft) !== JSON.stringify(settings.evaluation);
  const valid = !externalMode || ((connection === "adapter" ? !!external.adapter
    && Number.isInteger(external.adapter.revision) && external.adapter.revision > 0
    && /^[a-f0-9]{64}$/.test(external.adapter.source_hash) : /^https?:\/\//i.test(external.url.trim()))
    && Number.isInteger(external.timeout_seconds) && external.timeout_seconds >= 1 && external.timeout_seconds <= 1800
    && Number.isFinite(external.min_score) && external.min_score >= 0 && external.min_score <= 100
    && Number.isFinite(external.min_improvement) && external.min_improvement >= 0 && external.min_improvement <= 100);

  function editExternal(values: Partial<NonNullable<ReplayEvaluationConfig["external_agent"]>>) {
    setDraft({ ...draft, external_agent: { ...external, ...values } });
  }

  async function save() {
    if (!settings || !isAdmin || !valid) return;
    setBusy(true);
    setError("");
    try {
      const saved = await api<Settings>("/api/replay-evaluation", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: settings.revision, evaluation: draft }),
      });
      setSettings(saved);
      setDraft(saved.evaluation);
      toastOk("回放评价方式已保存", "仅影响后续受理的回放任务，历史结果和进行中的任务保持原评价方式");
    } catch (e: any) {
      setError(e.message || "保存评价配置失败；请刷新后重试");
    } finally {
      setBusy(false);
    }
  }

  return (
    <details className="mb-4 rounded-lg border border-border" open={open}
      onToggle={event => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer bg-surface-subtle px-3 py-3 text-xs font-semibold">
        A/B 回放评价方式
      </summary>
      <div className="space-y-3 p-3 text-xs">
        <p className="text-muted-foreground">选择本租户后续回放使用的评价方式，适用于 Skill 和业务 Prompt 的同一条验证链路。</p>
        {error && <div role="alert" className="text-destructive">{error}</div>}
        {!settings ? <Button size="sm" variant="outline" disabled={busy} onClick={load}>
          {busy ? "加载中…" : "加载评价配置"}
        </Button> : <>
          <label className="block font-semibold">评价方式
            <select aria-label="评价方式" value={draft.mode} disabled={!isAdmin || busy}
              className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-2"
              onChange={event => setDraft({ ...draft, mode: event.target.value as ReplayEvaluationConfig["mode"],
                ...(event.target.value === "external_agent" ? { external_agent: external } : {}) })}>
              <option value="checklist_efficiency">现有方式：Checklist 完成质量＋客观效率</option>
              <option value="external_agent">外部评价 Agent：业务评分</option>
            </select>
          </label>
          {!externalMode ? <p className="text-muted-foreground">
            保留现有规则：先比较 Checklist 完成情况，再比较交互轮次、工具调用和 Token。
          </p> : <fieldset disabled={!isAdmin || busy} className="space-y-3">
            <label className="block font-semibold">外部评测器接入方式
              <select className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-2" value={connection}
                onChange={event => {
                  setConnection(event.target.value as typeof connection);
                  if (event.target.value === "http") editExternal({ adapter: undefined });
                }}>
                <option value="http">标准 HTTP 接口</option>
                <option value="adapter">Python 适配器文件</option>
              </select>
            </label>
            {connection === "adapter" && <EvaluatorAdapterPicker value={external.adapter} disabled={!isAdmin || busy}
              onChange={adapter => editExternal({ adapter })} />}
            <label className="block font-semibold">{connection === "adapter" ? "外部服务 URL（由适配器决定，可选）" : "评价 Agent HTTP URL"}
              <Input value={external.url} placeholder="https://evaluator.example.com/evaluate"
                onChange={event => editExternal({ url: event.target.value })} className="mt-1" />
            </label>
            <label className="block font-semibold">认证 Secret Ref（可选）
              <Input value={external.secret_ref} placeholder="填写已保存凭证的引用，不填写明文密钥"
                onChange={event => editExternal({ secret_ref: event.target.value })} className="mt-1" />
            </label>
            <label className="block font-semibold">评价要求（可选）
              <Textarea value={external.rubric} rows={4} placeholder="描述业务评价维度与评分要求"
                onChange={event => editExternal({ rubric: event.target.value })} className="mt-1" />
            </label>
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block font-semibold">请求超时（秒）
                <Input type="number" min={1} max={1800} value={external.timeout_seconds}
                  onChange={event => editExternal({ timeout_seconds: event.target.valueAsNumber })} className="mt-1" />
              </label>
              <label className="block font-semibold">候选最低平均分
                <Input type="number" min={0} max={100} step="any" value={external.min_score}
                  onChange={event => editExternal({ min_score: event.target.valueAsNumber })} className="mt-1" />
              </label>
              <label className="block font-semibold">最低平均分提升
                <Input type="number" min={0} max={100} step="any" value={external.min_improvement}
                  onChange={event => editExternal({ min_improvement: event.target.valueAsNumber })} className="mt-1" />
              </label>
            </div>
            <p className="text-muted-foreground">
              分数范围为 0–100，越高越好。按所有回放 Case 的平均分比较，候选须有正向提升并达到所设门槛。
              Token、轮次和 Checklist 继续记录展示，不作为此模式的最终采纳标准。
              外部评价失败会显示“无法确定”，不会自动改用现有方式或发布。
            </p>
            <details>
              <summary className="cursor-pointer font-semibold">HTTP 接口返回格式</summary>
              <pre className="mt-2 overflow-auto rounded border border-border p-2 text-[11px]">{JSON.stringify({
                baseline: { score: 70, reason: "基线评价理由" }, candidate: { score: 85, reason: "候选评价理由" },
              }, null, 2)}</pre>
              <p className="mt-1 text-muted-foreground">TE 将同一 Case 的真实基线与候选执行内容一并发送；标准 HTTP 使用 Secret Ref 对应的 Bearer Token，Python 适配器可按外部接口协议转换请求和响应。</p>
            </details>
          </fieldset>}
          <div className="flex flex-wrap gap-2">
            {isAdmin && <Button size="sm" onClick={save} disabled={busy || !dirty || !valid}>
              {busy ? "保存中…" : "保存回放评价方式"}
            </Button>}
            <Button size="sm" variant="outline" disabled={busy} onClick={load}>重新加载已保存配置</Button>
          </div>
          <p className="text-[11px] text-muted-foreground">仅显式保存后生效。已受理任务沿用冻结的评价配置；切换模式保留外部 Agent 配置。</p>
        </>}
      </div>
    </details>
  );
}
