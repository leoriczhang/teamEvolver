import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  Bot,
  Copy,
  MessageSquare,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Sparkles,
  Terminal,
  Trash2,
  User,
  Wrench,
} from "lucide-react";
import { api, type SkillDetail, type SkillLabBranch, type SkillLabDataset, type SkillLabMaterial, type SkillLabRun, type SkillLabTraceMessage, type SkillListResp, type UserProfile } from "@/api/client";
import {
  Empty,
  ListViewport,
  PaginationControls,
  Panel,
  Pill,
  StatCard,
  usePagedItems,
} from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { fileToB64 } from "@/lib/file";
import { fmtTime } from "@/lib/format";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import DropZone from "@/views/skills/DropZone";

type DatasetDraft = {
  dataset_id?: string;
  name: string;
  query: string;
  requirements: string;
  trajectory_requirements: string;
  disclosure_batch_size: number;
  enabled_for_evolution: boolean;
};

const EMPTY_DATASET: DatasetDraft = {
  name: "",
  query: "",
  requirements: "",
  trajectory_requirements: "",
  disclosure_batch_size: 4,
  enabled_for_evolution: false,
};

const METRICS = [
  { key: "interaction_turns", label: "轮次" },
  { key: "tool_call_count", label: "Tool 调用" },
  { key: "total_tokens", label: "Tokens" },
] as const;

export default function SkillLabView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [skills, setSkills] = useState<SkillListResp["skills"]>([]);
  const [skillName, setSkillName] = useState("");
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [draftMd, setDraftMd] = useState("");
  const [datasets, setDatasets] = useState<SkillLabDataset[]>([]);
  const [selectedDatasetId, setSelectedDatasetId] = useState("");
  const [runs, setRuns] = useState<SkillLabRun[]>([]);
  const [activeRun, setActiveRun] = useState<SkillLabRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [savingSkill, setSavingSkill] = useState(false);
  const [showDatasetForm, setShowDatasetForm] = useState(false);
  const [datasetDraft, setDatasetDraft] = useState<DatasetDraft>(EMPTY_DATASET);
  const [datasetFiles, setDatasetFiles] = useState<SkillLabMaterial[]>([]);
  const [existingMaterials, setExistingMaterials] = useState<SkillLabMaterial[]>([]);
  const [materialRoot, setMaterialRoot] = useState("");
  const [filesTouched, setFilesTouched] = useState(false);
  const [savingDataset, setSavingDataset] = useState(false);
  const [importingDataset, setImportingDataset] = useState(false);
  const [synthesizing, setSynthesizing] = useState(false);
  const [maxInteractions, setMaxInteractions] = useState(8);
  const [timeoutSeconds, setTimeoutSeconds] = useState(600);
  const loaded = useRef(false);
  const datasetImportRef = useRef<HTMLInputElement>(null);

  const dirty = !!skill && draftMd !== (skill.skill_md || "");
  const isAdmin = user?.role === "admin";

  const loadSkill = useCallback(async (name: string) => {
    if (!name) {
      setSkill(null);
      setDraftMd("");
      return;
    }
    const detail = await api<SkillDetail>(`/api/skills/${encodeURIComponent(name)}`);
    setSkill(detail);
    setDraftMd(detail.skill_md || "");
  }, []);

  const loadWorkspace = useCallback(async (name: string) => {
    if (!name) {
      setDatasets([]);
      setRuns([]);
      return;
    }
    const [datasetResp, runResp] = await Promise.all([
      api<{ datasets: SkillLabDataset[] }>(
        `/api/skill-lab/datasets?skill_name=${encodeURIComponent(name)}`
      ),
      api<{ runs: SkillLabRun[] }>(
        `/api/skill-lab/runs?skill_name=${encodeURIComponent(name)}&limit=100`
      ),
    ]);
    const nextDatasets = datasetResp.datasets || [];
    setDatasets(nextDatasets);
    setSelectedDatasetId((current) => (
      nextDatasets.some((item) => item.dataset_id === current)
        ? current
        : nextDatasets[0]?.dataset_id || ""
    ));
    setRuns(runResp.runs || []);
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const skillResp = await api<SkillListResp>("/api/skills");
      const nextSkills = skillResp.skills || [];
      setSkills(nextSkills);
      const nextName = (
        skillName && nextSkills.some((item) => item.name === skillName)
          ? skillName
          : nextSkills[0]?.name || ""
      );
      setSkillName(nextName);
      await Promise.all([loadSkill(nextName), loadWorkspace(nextName)]);
    } catch (error: any) {
      toastErr("加载实验评测失败", error.message);
    } finally {
      setLoading(false);
    }
  }, [loadSkill, loadWorkspace, skillName]);

  useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);

  useEffect(() => {
    if (!active || !runs.some((run) => run.status === "running")) return;
    const timer = window.setInterval(async () => {
      try {
        const response = await api<{ runs: SkillLabRun[] }>(
          `/api/skill-lab/runs?skill_name=${encodeURIComponent(skillName)}&limit=100`
        );
        setRuns(response.runs || []);
        if (activeRun?.run_id) {
          const detail = await api<SkillLabRun>(
            `/api/skill-lab/runs/${encodeURIComponent(activeRun.run_id)}`
          );
          setActiveRun(detail);
        }
      } catch {
        // Keep polling; transient storage failures are surfaced on manual refresh.
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [active, activeRun?.run_id, runs, skillName]);

  async function selectSkill(name: string) {
    if (dirty && !window.confirm("当前 Skill 草稿尚未保存，确认切换？")) return;
    setSkillName(name);
    setActiveRun(null);
    setShowDatasetForm(false);
    setLoading(true);
    try {
      await Promise.all([loadSkill(name), loadWorkspace(name)]);
    } catch (error: any) {
      toastErr("切换 Skill 失败", error.message);
    } finally {
      setLoading(false);
    }
  }

  async function saveSkill() {
    if (!isAdmin || !skillName) return;
    setSavingSkill(true);
    try {
      await api(`/api/skill-lab/skills/${encodeURIComponent(skillName)}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_skill_md: draftMd }),
      });
      toastOk("Skill 草稿已保存并生效", skillName);
      await loadSkill(skillName);
    } catch (error: any) {
      toastErr("保存 Skill 失败", error.message);
    } finally {
      setSavingSkill(false);
    }
  }

  function createDataset() {
    setDatasetDraft(EMPTY_DATASET);
    setDatasetFiles([]);
    setExistingMaterials([]);
    setMaterialRoot("");
    setFilesTouched(true);
    setShowDatasetForm(true);
  }

  function editDataset(dataset: SkillLabDataset) {
    setDatasetDraft({
      dataset_id: dataset.dataset_id,
      name: dataset.name || "",
      query: dataset.query || "",
      requirements: dataset.requirements || "",
      trajectory_requirements: dataset.trajectory_requirements || "",
      disclosure_batch_size: dataset.progressive_disclosure?.batch_size || 4,
      enabled_for_evolution: !!dataset.enabled_for_evolution,
    });
    setDatasetFiles([]);
    setExistingMaterials(dataset.materials || []);
    setMaterialRoot(commonMaterialRoot(dataset.materials || []));
    setFilesTouched(false);
    setShowDatasetForm(true);
  }

  async function uploadDatasetFiles(files: FileList) {
    try {
      const payload: SkillLabMaterial[] = [];
      const root = materialRoot.trim().replace(/^\/+|\/+$/g, "");
      for (const file of Array.from(files)) {
        const browserPath = file.webkitRelativePath || file.name;
        payload.push({
          path: file.webkitRelativePath || (root ? `${root}/${browserPath}` : browserPath),
          size: file.size,
          content_b64: await fileToB64(file),
        });
      }
      setDatasetFiles(payload);
      setFilesTouched(true);
    } catch (error: any) {
      toastErr("读取材料失败", error.message);
    }
  }

  async function saveDataset() {
    if (!skillName || !datasetDraft.query.trim()) {
      toastErr("请填写实验 Query");
      return;
    }
    setSavingDataset(true);
    try {
      const payload: Record<string, any> = {
        ...datasetDraft,
        skill_name: skillName,
        progressive_disclosure: {
          enabled: true,
          initial_visibility: "query_only",
          batch_size: datasetDraft.disclosure_batch_size,
          stop_when: "all_checklist_items_satisfied",
        },
      };
      delete payload.disclosure_batch_size;
      if (filesTouched) payload.files = datasetFiles;
      const saved = await api<SkillLabDataset>("/api/skill-lab/datasets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      toastOk(datasetDraft.dataset_id ? "数据集已更新" : "数据集已创建", saved.name);
      setShowDatasetForm(false);
      await loadWorkspace(skillName);
      setSelectedDatasetId(saved.dataset_id);
    } catch (error: any) {
      toastErr("保存数据集失败", error.message);
    } finally {
      setSavingDataset(false);
    }
  }

  async function importDatasetPackage(files: FileList) {
    const entries = Array.from(files);
    if (!entries.length) return;
    setImportingDataset(true);
    try {
      let definition: File | null = null;
      let datasetMarkdown = "";
      for (const file of entries.filter((item) => /\.md$/i.test(item.name))) {
        const text = await file.text();
        if (/^###\s*query\s*$/im.test(text)) {
          definition = file;
          datasetMarkdown = text;
          break;
        }
      }
      if (!definition) {
        toastErr("导入失败", "所选文件中没有包含 `### query` 的数据集 Markdown");
        return;
      }
      const materials: SkillLabMaterial[] = [];
      for (const file of entries) {
        if (file === definition) continue;
        materials.push({
          path: file.webkitRelativePath || file.name,
          size: file.size,
          content_b64: await fileToB64(file),
        });
      }
      const saved = await api<SkillLabDataset>("/api/skill-lab/datasets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skill_name: skillName,
          name: definition.name.replace(/\.md$/i, ""),
          dataset_markdown: datasetMarkdown,
          files: materials,
        }),
      });
      toastOk(
        "数据集已导入",
        `${saved.name} · ${materials.length} 个相关材料`
      );
      await loadWorkspace(skillName);
      setSelectedDatasetId(saved.dataset_id);
    } catch (error: any) {
      toastErr("导入数据集失败", error.message);
    } finally {
      setImportingDataset(false);
    }
  }

  async function cloneDataset(dataset: SkillLabDataset) {
    try {
      const cloned = await api<SkillLabDataset>(
        `/api/skill-lab/datasets/${encodeURIComponent(dataset.dataset_id)}/clone`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ skill_name: skillName }),
        }
      );
      toastOk("已复制为可编辑数据集", cloned.name);
      await loadWorkspace(skillName);
      setSelectedDatasetId(cloned.dataset_id);
    } catch (error: any) {
      toastErr("复制数据集失败", error.message);
    }
  }

  async function deleteDataset(dataset: SkillLabDataset) {
    if (!window.confirm(`确认删除数据集「${dataset.name}」？`)) return;
    try {
      await api(
        `/api/skill-lab/datasets/${encodeURIComponent(dataset.dataset_id)}?skill_name=${encodeURIComponent(skillName)}`,
        { method: "DELETE" }
      );
      toastOk("数据集已删除");
      await loadWorkspace(skillName);
    } catch (error: any) {
      toastErr("删除数据集失败", error.message);
    }
  }

  async function startRun() {
    if (!skillName || !selectedDatasetId) {
      toastErr("请先选择实验数据集");
      return;
    }
    const selected = datasets.find(
      (item) => item.dataset_id === selectedDatasetId
    );
    if (selected?.material_integrity?.complete === false) {
      toastErr(
        "数据集材料不完整",
        (selected.material_integrity.missing_paths || []).join("、")
      );
      return;
    }
    setRunning(true);
    try {
      const run = await api<SkillLabRun>("/api/skill-lab/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skill_name: skillName,
          dataset_id: selectedDatasetId,
          candidate_skill_md: draftMd,
          timeout_seconds: timeoutSeconds,
          max_interactions: maxInteractions,
        }),
      });
      setRuns((current) => [run, ...current]);
      setActiveRun(run);
      toastOk("True Replay 实验已启动", run.run_id);
    } catch (error: any) {
      toastErr("启动实验失败", error.message);
    } finally {
      setRunning(false);
    }
  }

  async function synthesizeDatasets() {
    if (!skillName) return;
    setSynthesizing(true);
    try {
      const result = await api<{ datasets: SkillLabDataset[]; count: number; source_session_count: number }>(
        "/api/skill-lab/datasets/synthesize",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            skill_name: skillName,
          }),
        }
      );
      toastOk(
        "历史数据集生成完成",
        `${result.source_session_count} 个 Session → ${result.count} 个 test datasets`
      );
      await loadWorkspace(skillName);
      if (result.datasets[0]?.dataset_id) {
        setSelectedDatasetId(result.datasets[0].dataset_id);
      }
    } catch (error: any) {
      toastErr("历史数据集生成失败", error.message);
    } finally {
      setSynthesizing(false);
    }
  }

  async function openRun(runId: string) {
    try {
      const detail = await api<SkillLabRun>(
        `/api/skill-lab/runs/${encodeURIComponent(runId)}`
      );
      setActiveRun(detail);
    } catch (error: any) {
      toastErr("加载实验详情失败", error.message);
    }
  }

  const manualCount = datasets.filter((item) => item.source?.kind !== "evolution").length;
  const evolvedCount = datasets.filter((item) => item.source?.kind === "evolution").length;
  const regressionCount = datasets.filter((item) => item.enabled_for_evolution).length;
  const completedCount = runs.filter((run) => run.status === "completed").length;
  const selectedDataset = datasets.find(
    (item) => item.dataset_id === selectedDatasetId
  );
  const editingDataset = datasets.find(
    (item) => item.dataset_id === datasetDraft.dataset_id
  );
  const hasInlineMaterial = hasInlineMaterialSection(datasetDraft.query);
  const inlineMaterial = inlineMaterialText(datasetDraft.query);

  return (
    <div className="mx-auto max-w-[1440px] px-[22px] py-[22px]">
      <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="当前 Skill" value={skillName || "未选择"} mono />
        <StatCard label="可编辑数据集" value={manualCount} />
        <StatCard label="进化挖掘数据集" value={evolvedCount} />
        <StatCard label="固定回归集" value={regressionCount} />
        <StatCard label="已完成实验" value={completedCount} />
      </div>

      <Panel
        title="实验对象"
        count="草稿仅在实验中生效，保存后才进入正式技能库"
        extra={
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className="size-3.5" /> 刷新
          </Button>
        }
      >
        <div className="flex flex-wrap items-end gap-3 p-4">
          <Field label="Skill">
            <select
              value={skillName}
              onChange={(event) => selectSkill(event.target.value)}
              className="h-8 min-w-[260px] rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none"
            >
              {!skills.length && <option value="">暂无 Skill</option>}
              {skills.map((item) => (
                <option key={item.name} value={item.name}>{item.name}</option>
              ))}
            </select>
          </Field>
          <Field label="实验数据集">
            <select
              value={selectedDatasetId}
              onChange={(event) => setSelectedDatasetId(event.target.value)}
              className="h-8 min-w-[320px] max-w-[520px] rounded-lg border border-border bg-background px-2 text-xs outline-none"
            >
              {!datasets.length && <option value="">请先创建数据集</option>}
              {datasets.map((item) => (
                <option key={item.dataset_id} value={item.dataset_id}>
                  {item.enabled_for_evolution
                    ? "[回归] "
                    : item.source?.kind === "evolution"
                    ? "[进化] "
                    : item.source?.kind === "synthesized"
                      ? "[合成] "
                      : "[手工] "}
                  {item.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="最大交互轮次">
            <Input
              className="w-28"
              type="number"
              min={1}
              max={20}
              value={maxInteractions}
              onChange={(event) => setMaxInteractions(Number(event.target.value) || 1)}
            />
          </Field>
          <Field label="分支超时（秒）">
            <Input
              className="w-32"
              type="number"
              min={10}
              max={3600}
              value={timeoutSeconds}
              onChange={(event) => setTimeoutSeconds(Number(event.target.value) || 600)}
            />
          </Field>
          <Button
            className="ml-auto"
            disabled={
              !skillName
              || !selectedDatasetId
              || selectedDataset?.material_integrity?.complete === false
              || running
            }
            onClick={startRun}
            title={
              selectedDataset?.material_integrity?.complete === false
                ? "请先编辑数据集并补齐输入材料"
                : undefined
            }
          >
            <Play className="size-4" /> {running ? "启动中…" : "运行 True Replay"}
          </Button>
        </div>
      </Panel>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1.05fr)_minmax(420px,0.95fr)]">
        <Panel
          title="SKILL.md 实验草稿"
          count={dirty ? "未保存变更" : "与技能库一致"}
          extra={
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!dirty}
                onClick={() => setDraftMd(skill?.skill_md || "")}
              >
                <RotateCcw className="size-3.5" /> 重置
              </Button>
              <Button
                size="sm"
                disabled={!isAdmin || !dirty || savingSkill}
                onClick={saveSkill}
                title={isAdmin ? "保存到正式技能库并立即生效" : "仅管理员可保存正式 Skill"}
              >
                <Save className="size-3.5" /> {savingSkill ? "保存中…" : "保存到技能库"}
              </Button>
            </div>
          }
        >
          {!skill ? (
            <Empty>请选择一个 Skill。</Empty>
          ) : (
            <div className="p-4">
              <Textarea
                value={draftMd}
                onChange={(event) => setDraftMd(event.target.value)}
                spellCheck={false}
                className="mono h-[520px] min-h-0 max-h-[520px] resize-none overflow-y-auto [field-sizing:fixed] text-[12px] leading-relaxed"
              />
              <div className="mt-2 flex justify-between text-[11px] text-muted-foreground">
                <span>实验使用当前编辑内容，不要求先保存。</span>
                <span>{draftMd.length.toLocaleString()} 字符</span>
              </div>
            </div>
          )}
        </Panel>

        <div>
          <Panel
            title="Skill 数据集"
            count={`${datasets.length} 个`}
            extra={
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={synthesizeDatasets}
                  disabled={!skillName || synthesizing}
                >
                  <Sparkles className="size-3.5" />
                  {synthesizing ? "生成中…" : "从历史 Session / SOP 生成"}
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => datasetImportRef.current?.click()}
                  disabled={!skillName || importingDataset}
                >
                  {importingDataset ? "导入中…" : "导入 .md + 材料"}
                </Button>
                <input
                  ref={datasetImportRef}
                  type="file"
                  multiple
                  className="hidden"
                  onChange={(event) => {
                    if (event.target.files?.length) {
                      importDatasetPackage(event.target.files);
                    }
                    event.target.value = "";
                  }}
                />
                <Button size="sm" onClick={createDataset} disabled={!skillName}>
                  <Plus className="size-3.5" /> 新建
                </Button>
              </div>
            }
          >
            {!datasets.length ? (
              <Empty>暂无数据集。可手工填写 Query、要求、轨迹要求并上传材料。</Empty>
            ) : (
              <ListViewport maxHeight="520px">
                <div className="divide-y divide-line">
                  {datasets.map((dataset) => {
                    const selected = dataset.dataset_id === selectedDatasetId;
                    const evolved = dataset.source?.kind === "evolution";
                    const synthesized = dataset.source?.kind === "synthesized";
                    const materialIntegrity = dataset.material_integrity;
                    return (
                      <div
                        key={dataset.dataset_id}
                        className={cn(
                          "cursor-pointer p-4 transition-colors hover:bg-muted/50",
                          selected && "bg-accent-soft"
                        )}
                        onClick={() => setSelectedDatasetId(dataset.dataset_id)}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-2">
                              <span className="font-semibold">{dataset.name}</span>
                              <Pill tone={evolved ? "purple" : synthesized ? "green" : "blue"}>
                                {evolved ? "进化同步生成" : synthesized ? "Session / SOP 合成" : "手工"}
                              </Pill>
                              {dataset.enabled_for_evolution && (
                                <Pill tone="amber">固定回归</Pill>
                              )}
                              {materialIntegrity?.status === "missing" ? (
                                <Pill tone="red">
                                  缺材料 {materialIntegrity.missing_paths?.length || 1}
                                </Pill>
                              ) : materialIntegrity?.mode === "inline" ? (
                                <Pill tone="green">内嵌材料</Pill>
                              ) : materialIntegrity?.status === "complete" ? (
                                <Pill tone="green">材料齐全</Pill>
                              ) : null}
                              {!!dataset.materials?.length && (
                                <Pill tone="gray">{dataset.materials.length} 个材料</Pill>
                              )}
                              <Pill tone="gray">
                                {checklistLines(dataset.requirements).length} 条 Checklist
                              </Pill>
                            </div>
                            <div className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                              {dataset.query}
                            </div>
                            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-soft">
                              {dataset.source?.session_id && (
                                <span className="mono">session: {dataset.source.session_id}</span>
                              )}
                              {dataset.source?.job_id && (
                                <span className="mono">job: {dataset.source.job_id}</span>
                              )}
                            </div>
                          </div>
                          <div className="flex shrink-0 gap-1.5" onClick={(event) => event.stopPropagation()}>
                            <Button variant="outline" size="sm" onClick={() => editDataset(dataset)}>
                              编辑
                            </Button>
                            {evolved && (
                              <Button variant="outline" size="sm" onClick={() => cloneDataset(dataset)}>
                                <Copy className="size-3.5" /> 复制
                              </Button>
                            )}
                            {!evolved && (
                              <Button variant="destructive" size="sm" onClick={() => deleteDataset(dataset)}>
                                <Trash2 className="size-3.5" />
                              </Button>
                            )}
                          </div>
                        </div>
                        {selected && <DatasetDetail dataset={dataset} />}
                      </div>
                    );
                  })}
                </div>
              </ListViewport>
            )}
          </Panel>

        </div>
      </div>

      <Dialog
        open={showDatasetForm}
        onOpenChange={(open) => {
          if (!open && !savingDataset) setShowDatasetForm(false);
        }}
      >
        <DialogContent className="flex max-h-[90vh] w-full !max-w-[860px] flex-col overflow-hidden p-0">
          <DialogHeader className="border-b border-line px-5 py-4">
            <DialogTitle>
              {datasetDraft.dataset_id ? "编辑数据集" : "新建手工数据集"}
            </DialogTitle>
          </DialogHeader>
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
            {editingDataset?.material_integrity?.status === "missing" && (
              <div className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-xs leading-relaxed text-destructive">
                当前数据集缺少输入材料：
                {(editingDataset.material_integrity.missing_paths || []).join("、")}。
                请在本次编辑中上传对应文件，或把必要内容直接写入 Query 的“材料：”段落。
              </div>
            )}
            <Field label="名称">
              <Input
                value={datasetDraft.name}
                placeholder="例如：组合与仓位分配"
                onChange={(event) => setDatasetDraft({ ...datasetDraft, name: event.target.value })}
              />
            </Field>
            <Field label="初始 Query *" hint="对应数据集 Markdown 的 `### query`。">
              <Textarea
                rows={5}
                value={datasetDraft.query}
                onChange={(event) => setDatasetDraft({ ...datasetDraft, query: event.target.value })}
              />
            </Field>
            {hasInlineMaterial && (
              <Field
                label="内嵌材料"
                hint="来自初始 Query 的“材料：”段落；这里的修改会同步回 Query。"
              >
                <Textarea
                  rows={8}
                  value={inlineMaterial}
                  className="mono max-h-[280px] overflow-y-auto text-xs leading-relaxed"
                  onChange={(event) => setDatasetDraft({
                    ...datasetDraft,
                    query: replaceInlineMaterial(
                      datasetDraft.query,
                      event.target.value,
                    ),
                  })}
                />
              </Field>
            )}
            <ChecklistEditor
              label="要求 Checklist"
              hint="首轮隐藏；每条独立编辑，作为逐轮完成条件，不计算综合分数。"
              value={datasetDraft.requirements}
              placeholder="例如：最终产物保存到指定目录"
              onChange={(requirements) => setDatasetDraft({
                ...datasetDraft,
                requirements,
              })}
            />
            <ChecklistEditor
              label="轨迹要求"
              hint="每条描述一个材料读取、工具调用、计算、校验或产物写入动作。"
              value={datasetDraft.trajectory_requirements}
              placeholder="例如：读取 q1_materials 下的全部材料"
              onChange={(trajectory_requirements) => setDatasetDraft({
                ...datasetDraft,
                trajectory_requirements,
              })}
            />
            <Field label="每轮披露条数" hint="每轮只披露下一批尚未满足的 Checklist。">
              <Input
                type="number"
                min={1}
                max={24}
                value={datasetDraft.disclosure_batch_size}
                onChange={(event) => setDatasetDraft({
                  ...datasetDraft,
                  disclosure_batch_size: Number(event.target.value) || 1,
                })}
              />
            </Field>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-border bg-background p-3">
              <input
                type="checkbox"
                className="mt-0.5 size-4"
                checked={datasetDraft.enabled_for_evolution}
                onChange={(event) => setDatasetDraft({
                  ...datasetDraft,
                  enabled_for_evolution: event.target.checked,
                })}
              />
              <span>
                <span className="block text-xs font-semibold">设为固定回归集</span>
                <span className="mt-1 block text-[11px] leading-relaxed text-muted-foreground">
                  后续该 Skill 进化时优先使用此数据集进行 True Replay；未勾选时仅用于实验台。
                </span>
              </span>
            </label>
            <Field
              label="相关材料"
              hint={
                datasetDraft.dataset_id && existingMaterials.length && !filesTouched
                  ? `保留现有 ${existingMaterials.length} 个材料；重新上传会整体替换。`
                  : "材料按文件名写入两个隔离工作区，Query 可使用相对路径引用。"
              }
            >
              {hasInlineMaterial && (
                <div className="mb-3 rounded-lg border border-success/30 bg-success/5 p-3">
                  <div className="text-xs font-semibold text-success">
                    已包含内嵌材料
                  </div>
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    共 {inlineMaterial.length.toLocaleString()} 个字符，可在上方“内嵌材料”编辑框中修改；无需重复上传附件。
                  </div>
                </div>
              )}
              <Input
                className="mb-2"
                value={materialRoot}
                placeholder="工作区材料目录，例如 q5_materials（可留空）"
                onChange={(event) => setMaterialRoot(event.target.value)}
              />
              <DropZone
                multiple
                onFiles={uploadDatasetFiles}
                label="点击或拖拽上传 CSV、JSON、XLSX、PPTX、Markdown 等实验材料"
              />
              {!!datasetFiles.length && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {datasetFiles.map((file) => (
                    <Pill key={file.path} tone="gray">
                      {file.path} · {formatBytes(file.size || 0)}
                    </Pill>
                  ))}
                </div>
              )}
            </Field>
          </div>
          <DialogFooter className="border-t border-line !bg-surface px-5 py-3">
            <Button
              variant="outline"
              disabled={savingDataset}
              onClick={() => setShowDatasetForm(false)}
            >
              取消
            </Button>
            <Button disabled={savingDataset} onClick={saveDataset}>
              <Save className="size-3.5" /> {savingDataset ? "保存中…" : "保存数据集"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Panel title="实验运行历史" count={`${runs.length} 次`}>
        {!runs.length ? (
          <Empty>尚未运行实验。</Empty>
        ) : (
          <ListViewport maxHeight="420px">
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {["状态", "运行 ID", "数据集", "结论", "轮次", "Tool", "Tokens", "时间", "操作"].map((header) => (
                    <th key={header} className="border-b border-line px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">
                      {header}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => {
                  const dimensions = run.result_summary?.efficiency?.dimensions || {};
                  return (
                    <tr key={run.run_id} className={cn(activeRun?.run_id === run.run_id && "bg-accent-soft")}>
                      <Cell><RunStatus run={run} /></Cell>
                      <Cell><span className="mono text-xs">{run.run_id}</span></Cell>
                      <Cell>{run.dataset_name || run.dataset_id}</Cell>
                      <Cell><Verdict value={run.result_summary?.verdict} /></Cell>
                      {METRICS.map((metric) => (
                        <Cell key={metric.key}>
                          <MetricInline value={dimensions[metric.key]} />
                        </Cell>
                      ))}
                      <Cell><span className="text-xs text-muted-foreground">{fmtTime(run.created_at)}</span></Cell>
                      <Cell>
                        <Button variant="outline" size="sm" onClick={() => openRun(run.run_id)}>
                          查看 Trace
                        </Button>
                      </Cell>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </ListViewport>
        )}
      </Panel>

      {activeRun && <RunDetail run={activeRun} onRefresh={() => openRun(activeRun.run_id)} />}
    </div>
  );
}

function DatasetDetail({ dataset }: { dataset: SkillLabDataset }) {
  const requirements = checklistLines(dataset.requirements);
  const trajectory = checklistLines(dataset.trajectory_requirements);
  const materialIntegrity = dataset.material_integrity;
  const sourceSessionIds = Array.from(new Set([
    ...(dataset.source?.source_session_ids || []),
    ...(dataset.source?.session_id ? [dataset.source.session_id] : []),
  ]));
  return (
    <div
      className="mt-4 space-y-4 rounded-lg border border-border bg-surface p-4"
      onClick={(event) => event.stopPropagation()}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-sm font-bold">数据集详情</div>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone="blue">初始 Query</Pill>
          <Pill tone="amber">{requirements.length} 条 Checklist</Pill>
          <Pill tone="purple">{trajectory.length} 条轨迹要求</Pill>
          {dataset.enabled_for_evolution && <Pill tone="amber">固定回归集</Pill>}
          {materialIntegrity?.status === "missing" ? (
            <Pill tone="red">材料不完整</Pill>
          ) : materialIntegrity?.mode === "inline" ? (
            <Pill tone="green">内嵌材料</Pill>
          ) : materialIntegrity?.status === "complete" ? (
            <Pill tone="green">材料齐全</Pill>
          ) : null}
          <Pill tone="gray">
            每轮披露 {dataset.progressive_disclosure?.batch_size || 4} 条
          </Pill>
        </div>
      </div>

      <DatasetSection title="初始 Query">
        <pre className="mono whitespace-pre-wrap break-words text-[12px] leading-relaxed">
          {dataset.query || "（空）"}
        </pre>
      </DatasetSection>

      <DatasetSection title={`要求 Checklist（${requirements.length}）`}>
        <PagedTextWindow
          items={requirements}
          empty="未定义 Checklist，该条目不会作为正式渐进数据集运行。"
        />
      </DatasetSection>

      <DatasetSection title={`轨迹要求（${trajectory.length}）`}>
        <PagedTextWindow items={trajectory} empty="无额外轨迹要求。" />
      </DatasetSection>

      <DatasetSection title={`相关材料（${dataset.materials?.length || 0}）`}>
        {materialIntegrity?.status === "missing" ? (
          <div className="space-y-2">
            <div className="text-xs font-semibold text-destructive">
              缺少回放所需材料，运行前必须补齐。
            </div>
            {(materialIntegrity.missing_paths || []).map((path) => (
              <div key={path} className="mono rounded border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-xs text-destructive">
                {path}
              </div>
            ))}
          </div>
        ) : dataset.materials?.length ? (
          <div className="space-y-1.5">
            {dataset.materials.map((material) => (
              <div key={material.path} className="flex items-center justify-between gap-3 rounded border border-border px-2.5 py-2 text-xs">
                <span className="mono break-all">{material.path}</span>
                <span className="shrink-0 text-muted-foreground">{formatBytes(material.size || 0)}</span>
              </div>
            ))}
          </div>
        ) : materialIntegrity?.mode === "inline" ? (
          <pre className="mono max-h-[280px] overflow-auto whitespace-pre-wrap break-words rounded-md border border-success/30 bg-success/5 p-3 text-[11px] leading-relaxed">
            {inlineMaterialText(dataset.query) || "材料已直接写入初始 Query。"}
          </pre>
        ) : sourceSessionIds.length ? (
          <span className="text-xs text-muted-foreground">
            当前 Query 不依赖外部输入文件；来源 Session 仅用于追溯和运行时上下文。
          </span>
        ) : (
          <span className="text-xs text-muted-foreground">无相关材料。</span>
        )}
      </DatasetSection>

      <div className="grid gap-3 text-[11px] text-muted-foreground md:grid-cols-2">
        <div>
          <span className="font-semibold">来源 Session：</span>
          <span className="mono break-all">{sourceSessionIds.join(", ") || "—"}</span>
        </div>
        <div>
          <span className="font-semibold">来源 Job：</span>
          <span className="mono break-all">{dataset.source?.job_id || dataset.source?.candidate_job_id || "—"}</span>
        </div>
      </div>

      <details>
        <summary className="cursor-pointer text-xs font-semibold text-muted-foreground">
          查看标准 Markdown
        </summary>
        <pre className="mono mt-2 max-h-[360px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-background p-3 text-[11px] leading-relaxed">
          {dataset.dataset_markdown || "（空）"}
        </pre>
      </details>
    </div>
  );
}

function ChecklistEditor({
  label,
  hint,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  hint: string;
  value: string;
  placeholder: string;
  onChange: (value: string) => void;
}) {
  const items = editableChecklistLines(value);

  function commit(next: string[]) {
    onChange(
      next
        .map((item, index) => `${index + 1}. ${item.replace(/\r?\n+/g, " ")}`)
        .join("\n")
    );
  }

  function update(index: number, text: string) {
    const next = [...items];
    next[index] = text;
    commit(next);
  }

  function add(afterIndex: number = items.length - 1) {
    const next = [...items];
    next.splice(afterIndex + 1, 0, "");
    commit(next);
  }

  function remove(index: number) {
    commit(items.filter((_, itemIndex) => itemIndex !== index));
  }

  return (
    <section>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <div>
          <div className="text-xs font-semibold text-muted-foreground">
            {label}（{items.filter((item) => item.trim()).length}）
          </div>
          <div className="mt-1 text-[11px] text-muted-soft">{hint}</div>
        </div>
        <Button variant="outline" size="sm" type="button" onClick={() => add()}>
          <Plus className="size-3.5" /> 新增一条
        </Button>
      </div>
      <ListViewport maxHeight="310px">
        <div className="space-y-2 rounded-lg border border-border bg-background p-2.5">
          {items.length ? items.map((item, index) => (
            <div
              key={index}
              className="flex items-start gap-2 rounded-md border border-line bg-surface p-2"
            >
              <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted text-[11px] font-bold text-muted-foreground">
                {index + 1}
              </span>
              <Textarea
                rows={2}
                value={item}
                aria-label={`${label}第 ${index + 1} 条`}
                placeholder={placeholder}
                className="min-h-[58px] flex-1 resize-none [field-sizing:fixed] text-xs leading-relaxed"
                onChange={(event) => update(index, event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    add(index);
                  }
                }}
              />
              <Button
                variant="ghost"
                size="icon-sm"
                type="button"
                aria-label={`删除${label}第 ${index + 1} 条`}
                className="shrink-0 text-destructive hover:text-destructive"
                onClick={() => remove(index)}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          )) : (
            <div className="px-3 py-5 text-center text-xs text-muted-foreground">
              暂无条目，点击“新增一条”开始填写。
            </div>
          )}
        </div>
      </ListViewport>
    </section>
  );
}

function DatasetSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <div className="mb-1.5 text-xs font-semibold text-muted-foreground">{title}</div>
      <div className="rounded-md border border-border bg-background p-3">{children}</div>
    </section>
  );
}

function PagedTextWindow({ items, empty }: { items: string[]; empty: string }) {
  const pager = usePagedItems(items, 5, 7);
  if (!items.length) {
    return <span className="text-xs text-muted-foreground">{empty}</span>;
  }
  return (
    <div className="-m-3">
      <ListViewport maxHeight="190px">
        <div className="min-h-[150px] space-y-2 p-3">
          {pager.items.map((item, index) => (
            <div
              key={`${pager.start + index}-${item}`}
              className="flex items-start gap-2.5 rounded-md border border-line bg-surface px-3 py-2.5 text-xs leading-relaxed"
            >
              <span className="grid size-6 shrink-0 place-items-center rounded-md bg-muted text-[11px] font-bold text-muted-foreground">
                {pager.start + index + 1}
              </span>
              <span className="min-w-0 flex-1">{item}</span>
            </div>
          ))}
        </div>
      </ListViewport>
      <PaginationControls
        {...pager}
        visiblePages={7}
        onPageChange={pager.setPage}
      />
    </div>
  );
}

function RunDetail({ run, onRefresh }: { run: SkillLabRun; onRefresh: () => void }) {
  const result = run.result;
  const dimensions = result?.efficiency?.dimensions || run.result_summary?.efficiency?.dimensions || {};
  const replayCase = result?.cases?.[0];
  return (
    <div id="skill-lab-run-detail">
      <Panel
        title={
          <span className="flex flex-wrap items-center gap-2">
            完整 Trace · <span className="mono text-xs">{run.run_id}</span>
            <RunStatus run={run} />
          </span>
        }
        extra={
          <Button variant="outline" size="sm" onClick={onRefresh}>
            <RefreshCw className="size-3.5" /> 刷新
          </Button>
        }
      >
        {run.status === "running" ? (
          <Empty>True Replay 正在并行执行 baseline / candidate 分支，完成后自动显示完整 Trace。</Empty>
        ) : !result ? (
          <Empty>运行结果尚未写入。</Empty>
        ) : (
          <div className="space-y-5 p-4">
            <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3.5">
              {METRICS.map((metric) => (
                <MetricCard
                  key={metric.key}
                  label={metric.label}
                  metric={dimensions[metric.key]}
                />
              ))}
              <StatCard label="客观结论" value={<Verdict value={result.verdict} />} />
              <StatCard label="模型" value={result.harness?.model || "—"} mono />
            </div>
            {result.reason && (
              <div className="rounded-lg border border-border bg-background/60 p-3 text-xs text-muted-foreground">
                {result.reason}
              </div>
            )}
            <div className="grid gap-5 2xl:grid-cols-2">
              <TraceBranch title="Baseline · 当前 Skill" branch={replayCase?.baseline} tone="gray" />
              <TraceBranch title="Candidate · 实验草稿" branch={replayCase?.candidate} tone="blue" />
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}

function TraceBranch({
  title,
  branch,
  tone,
}: {
  title: string;
  branch?: SkillLabBranch;
  tone: "gray" | "blue";
}) {
  const messages = branch?.messages || [];
  return (
    <section className="overflow-hidden rounded-lg border border-border bg-background/40">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface-subtle px-4 py-3">
        <div className="flex items-center gap-2 font-semibold">
          <Terminal className="size-4" /> {title}
        </div>
        <div className="flex flex-wrap gap-1.5">
          <Pill tone={tone}>{branch?.interaction_turns ?? 0} 轮</Pill>
          <Pill tone={tone}>{branch?.tool_call_count ?? 0} tools</Pill>
          <Pill tone={tone}>{Number(branch?.total_tokens || 0).toLocaleString()} tokens</Pill>
          <Pill tone={branch?.ok ? "green" : "red"}>{branch?.ok ? "完成" : "失败"}</Pill>
        </div>
      </div>
      {branch?.error && (
        <pre className="mono whitespace-pre-wrap break-words border-b border-line bg-red-50 p-3 text-xs text-red-700">
          {branch.error}
        </pre>
      )}
      {!!branch?.interactions?.length && (
        <div className="space-y-2 border-b border-line p-3">
          <div className="text-xs font-semibold text-muted-foreground">
            渐进式 Checklist 交互
          </div>
          {branch.interactions.map((interaction, index) => {
            const report = interaction.checklist_report as SkillLabBranch["checklist_report"] | undefined;
            return (
              <details
                key={index}
                className="overflow-hidden rounded-md border border-border bg-surface"
                open={index === branch.interactions!.length - 1}
              >
                <summary className="flex cursor-pointer flex-wrap items-center gap-2 bg-surface-subtle px-3 py-2 text-xs font-semibold">
                  第 {Number(interaction.interaction_num || index + 1)} 轮
                  <Pill tone={report?.all_satisfied ? "green" : "amber"}>
                    {report?.satisfied_count ?? 0}/{report?.total ?? 0} 已满足
                  </Pill>
                  <span className="text-muted-foreground">
                    {Number(interaction.tool_call_count || 0)} tools · {Number(interaction.total_tokens || 0).toLocaleString()} tokens
                  </span>
                </summary>
                <div className="space-y-3 border-t border-line p-3">
                  <TraceBlock label="本轮披露 / 用户消息" value={String(interaction.prompt || "")} />
                  <TraceBlock label="Agent 回复" value={String(interaction.response || "")} />
                  {!!report?.items?.length && (
                    <div>
                      <div className="mb-1.5 text-[11px] font-semibold text-muted-foreground">Checklist 判定</div>
                      <div className="space-y-1">
                        {report.items.map((item) => (
                          <div key={item.id} className="flex items-start gap-2 rounded border border-border px-2.5 py-2 text-[11px]">
                            <Pill tone={item.satisfied ? "green" : "amber"}>
                              {item.satisfied ? "满足" : "未满足"}
                            </Pill>
                            <div className="min-w-0">
                              <div><span className="mono text-muted-soft">{item.id}</span> {item.text}</div>
                              {item.evidence && <div className="mt-1 text-muted-foreground">{item.evidence}</div>}
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              </details>
            );
          })}
        </div>
      )}
      <div className="max-h-[760px] space-y-2 overflow-auto p-3">
        {messages.length ? messages.map((message, index) => (
          <TraceMessage key={`${message.role}-${index}`} message={message} index={index} />
        )) : branch?.trajectory ? (
          <pre className="mono whitespace-pre-wrap break-words text-[11px] leading-relaxed">
            {branch.trajectory}
          </pre>
        ) : (
          <Empty>该分支没有可展示的消息。</Empty>
        )}
      </div>
      <details className="border-t border-line" open>
        <summary className="cursor-pointer bg-surface-subtle px-4 py-2.5 text-xs font-semibold">
          最终输出
        </summary>
        <pre className="mono max-h-[420px] overflow-auto whitespace-pre-wrap break-words p-4 text-[11px] leading-relaxed">
          {branch?.final_response || "（空）"}
        </pre>
      </details>
    </section>
  );
}

function TraceBlock({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold text-muted-foreground">{label}</div>
      <pre className="mono max-h-[260px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-background p-2.5 text-[11px] leading-relaxed">
        {value || "（空）"}
      </pre>
    </div>
  );
}

function TraceMessage({ message, index }: { message: SkillLabTraceMessage; index: number }) {
  const role = String(message.role || "unknown");
  const icon = role === "user"
    ? <User className="size-3.5" />
    : role === "assistant"
      ? <Bot className="size-3.5" />
      : role === "tool"
        ? <Wrench className="size-3.5" />
        : <MessageSquare className="size-3.5" />;
  const tone = role === "assistant" ? "blue" : role === "tool" ? "purple" : role === "user" ? "green" : "gray";
  const toolCalls = message.tool_calls || [];
  const content = formatContent(message.content);
  return (
    <details className="overflow-hidden rounded-md border border-border bg-surface" open={role === "tool" || !!toolCalls.length}>
      <summary className="flex cursor-pointer items-center gap-2 bg-surface-subtle px-3 py-2 text-xs font-semibold">
        {icon}
        <Pill tone={tone}>{role}</Pill>
        <span>#{index + 1}</span>
        {message.name && <span className="mono text-muted-foreground">{message.name}</span>}
        {message.tool_call_id && <span className="mono truncate text-[10px] text-muted-soft">{message.tool_call_id}</span>}
      </summary>
      {content && (
        <pre className="mono overflow-auto whitespace-pre-wrap break-words border-t border-line p-3 text-[11px] leading-relaxed">
          {content}
        </pre>
      )}
      {toolCalls.map((call, callIndex) => (
        <div key={call.id || callIndex} className="border-t border-line p-3">
          <div className="mb-1 flex items-center gap-2 text-xs font-semibold">
            <Wrench className="size-3.5" />
            <span className="mono">{call.function?.name || "tool"}</span>
            {call.id && <span className="mono text-[10px] text-muted-soft">{call.id}</span>}
          </div>
          <pre className="mono max-h-[320px] overflow-auto whitespace-pre-wrap break-words rounded-md bg-[#0f172a] p-3 text-[11px] leading-relaxed text-[#cbd5e1]">
            {formatArguments(call.function?.arguments)}
          </pre>
        </div>
      ))}
    </details>
  );
}

function MetricCard({
  label,
  metric,
}: {
  label: string;
  metric?: {
    baseline?: number;
    candidate?: number;
    delta?: number;
    reduction_ratio?: number;
    winner?: string;
  };
}) {
  const delta = Number(metric?.delta || 0);
  return (
    <div className="rounded-lg border border-border bg-surface p-4 shadow-[var(--shadow-soft)]">
      <div className="mb-2 text-xs font-semibold text-muted-foreground">{label}</div>
      <div className="flex items-baseline gap-2 text-lg font-bold">
        <span>{Number(metric?.baseline || 0).toLocaleString()}</span>
        <span className="text-muted-soft">→</span>
        <span>{Number(metric?.candidate || 0).toLocaleString()}</span>
      </div>
      <div className={cn(
        "mt-1 text-xs font-semibold",
        delta > 0 ? "text-success" : delta < 0 ? "text-destructive" : "text-muted-foreground"
      )}>
        {delta > 0 ? `减少 ${delta.toLocaleString()}` : delta < 0 ? `增加 ${Math.abs(delta).toLocaleString()}` : "持平"}
      </div>
    </div>
  );
}

function MetricInline({ value }: { value?: { baseline?: number; candidate?: number; delta?: number } }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  const delta = Number(value.delta || 0);
  return (
    <span className={cn(
      "mono text-xs",
      delta > 0 ? "text-success" : delta < 0 ? "text-destructive" : "text-muted-foreground"
    )}>
      {value.baseline ?? 0}→{value.candidate ?? 0}
    </span>
  );
}

function RunStatus({ run }: { run: SkillLabRun }) {
  const tone = run.status === "running"
    ? "blue"
    : run.status === "completed"
      ? "green"
      : run.status === "skipped"
        ? "amber"
        : "red";
  const label = run.status === "running"
    ? "运行中"
    : run.status === "completed"
      ? "已完成"
      : run.status === "skipped"
        ? "已跳过"
        : "失败";
  return <Pill tone={tone}>{label}</Pill>;
}

function Verdict({ value }: { value?: string }) {
  if (!value) return <span className="text-muted-foreground">—</span>;
  const tone = value === "accept" ? "green" : value === "reject" ? "red" : "amber";
  const label = value === "accept" ? "指标优化" : value === "reject" ? "指标退化" : "指标持平";
  return <Pill tone={tone}>{label}</Pill>;
}

function Cell({ children }: { children: ReactNode }) {
  return <td className="border-b border-line px-4 py-2.5 align-top text-sm">{children}</td>;
}

function Field({
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

function formatContent(content: SkillLabTraceMessage["content"]): string {
  if (typeof content === "string") return content;
  if (content == null) return "";
  try {
    return JSON.stringify(content, null, 2);
  } catch {
    return String(content);
  }
}

function formatArguments(value: unknown): string {
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch {
    return String(value);
  }
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

const INLINE_MATERIAL_SECTION_RE =
  /(^|\n)([ \t]*(?:材料|素材|输入数据|参考资料)\s*[：:])[ \t]*\n?([\s\S]*)$/i;

function inlineMaterialText(query?: string): string {
  const match = INLINE_MATERIAL_SECTION_RE.exec(String(query || ""));
  return match?.[3]?.trim() || "";
}

function hasInlineMaterialSection(query?: string): boolean {
  return INLINE_MATERIAL_SECTION_RE.test(String(query || ""));
}

function replaceInlineMaterial(query: string, material: string): string {
  const match = INLINE_MATERIAL_SECTION_RE.exec(String(query || ""));
  if (!match || match.index == null) return query;
  const prefix = query.slice(0, match.index);
  return `${prefix}${match[1]}${match[2]}\n${material}`;
}

function checklistLines(value?: string): string[] {
  return String(value || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*/, "").trim())
    .filter(Boolean);
}

function editableChecklistLines(value?: string): string[] {
  if (!value) return [];
  return String(value)
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:[-*+]|\d+[.)、]|[（(]\d+[)）])\s*/, ""));
}

function commonMaterialRoot(materials: SkillLabMaterial[]): string {
  const paths = materials.map((item) => item.path).filter(Boolean);
  if (!paths.length || !paths[0].includes("/")) return "";
  const first = paths[0].split("/")[0];
  return paths.every((path) => path.startsWith(`${first}/`)) ? first : "";
}
