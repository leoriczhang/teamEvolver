import {
Empty,
ListViewport,
Panel,
Pill,
StatCard
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
import { type UserProfile } from "@/features/admin/api/client";
import { type WorkbenchRun } from "@/features/replay/api/client";
import DropZone from "@/features/skills/skills/DropZone";
import { fmtTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import {
Copy,
Play,
Plus,
RefreshCw,
RotateCcw,
Save,
Sparkles,
Trash2
} from "lucide-react";
import { Cell,ChecklistEditor,DatasetDetail,datasetForEvolution,datasetMaterials,datasetSkillIds,Field,formatBytes,MetricInline,METRICS,primaryCase,replaceInlineMaterial,RunDetail,RunStatus,Verdict } from "./ReplayPanelShared";
import { useReplayPanel } from "./useReplayPanel";

export default function ReplayPanel({
  active,
  user,
  lockedSkillName = "",
  externalDraftMd,
  embedded = false,
  compact = false,
  externalFiles,
  onRunChange,
  runSignal = 0,
}: {
  active: boolean;
  user?: UserProfile | null;
  lockedSkillName?: string;
  externalDraftMd?: string;
  embedded?: boolean;
  compact?: boolean;
  externalFiles?: Record<string, string>;
  onRunChange?: (run: WorkbenchRun | null) => void;
  runSignal?: number;
}) {
const { skills, skillName, skill, draftMd, setDraftMd, datasets, selectedDatasetId, setSelectedDatasetId, runs, activeRun, loading, running, savingSkill, showDatasetForm, setShowDatasetForm, datasetDraft, setDatasetDraft, datasetFiles, existingMaterials, materialRoot, setMaterialRoot, filesTouched, savingDataset, importingDataset, synthesizing, synthesizeSessionIds, setSynthesizeSessionIds, maxInteractions, setMaxInteractions, timeoutSeconds, setTimeoutSeconds, datasetImportRef, dirty, isAdmin, refresh, selectSkill, saveSkill, createDataset, editDataset, uploadDatasetFiles, saveDataset, importDatasetPackage, cloneDataset, deleteDataset, startRun, synthesizeDatasets, openRun, manualCount, evolvedCount, regressionCount, completedCount, selectedDataset, editingDataset, hasInlineMaterial, inlineMaterial } = useReplayPanel({ active, user, lockedSkillName, externalDraftMd, embedded, compact, externalFiles, onRunChange, runSignal });
return (
    <div className={cn("skill-lab-view min-w-0", compact && "skill-lab-compact", embedded ? "px-1 py-1" : "mx-auto max-w-[1440px] px-[22px] py-[22px]")}>
      {!compact && <div className="mb-5 grid grid-cols-[repeat(auto-fit,minmax(160px,1fr))] gap-3.5">
        <StatCard label="当前 Skill" value={skillName || "未选择"} mono />
        <StatCard label="可编辑数据集" value={manualCount} />
        <StatCard label="进化挖掘数据集" value={evolvedCount} />
        <StatCard label="固定回归集" value={regressionCount} />
        <StatCard label="已完成实验" value={completedCount} />
      </div>}

      <Panel
        title={compact ? "运行与调试" : "实验对象"}
        count={compact ? skillName : "Candidate 仅在实验中生效"}
        extra={
          <Button variant="outline" size="sm" onClick={refresh} disabled={loading}>
            <RefreshCw className="size-3.5" /> 刷新
          </Button>
        }
      >
        <div className="flex flex-wrap items-end gap-3 p-4">
          {!compact && <Field label="Skill">
            <select
              value={skillName}
              disabled={!!lockedSkillName}
              onChange={(event) => selectSkill(event.target.value)}
              className="h-8 w-[260px] min-w-0 max-w-full rounded-lg border border-border bg-background px-2 text-xs font-semibold outline-none disabled:opacity-70"
            >
              {!skills.length && <option value="">暂无 Skill</option>}
              {skills.map((item) => (
                <option key={item.name} value={item.name}>{item.name}</option>
              ))}
            </select>
          </Field>}
          <Field label="实验数据集">
            <select
              value={selectedDatasetId}
              onChange={(event) => setSelectedDatasetId(event.target.value)}
              className="h-8 w-[320px] min-w-0 max-w-full rounded-lg border border-border bg-background px-2 text-xs outline-none"
            >
              {!datasets.length && <option value="">请先创建数据集</option>}
              {datasets.map((item) => (
                <option key={item.dataset_id} value={item.dataset_id}>
                  {datasetForEvolution(item)
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
              || loading
              || !draftMd.trim()
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

      {compact && (
        <div className="lab-quick-actions">
          <p>当前 Skill 的未保存文件一起参与 Candidate 运行。</p>
          <Button variant="outline" size="sm" disabled={!skillName} onClick={createDataset}><Plus className="size-3.5" />新建数据集</Button>
          {selectedDataset && <Button variant="ghost" size="sm" onClick={() => editDataset(selectedDataset)}>编辑数据集</Button>}
          {selectedDataset && <p className="lab-query-preview">{primaryCase(selectedDataset)?.query}</p>}
        </div>
      )}
      {!compact && <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.05fr)_minmax(420px,0.95fr)]">
        <Panel
          title={embedded ? "候选草稿（来自工作空间编辑器）" : "SKILL.md 实验草稿"}
          count={dirty ? "与已保存版本有差异 · 作为 Candidate" : "与技能库一致"}
          extra={
            embedded ? (
              <span className="text-[11px] text-muted-foreground">
                Baseline=已保存版本 · Candidate=当前编辑内容
              </span>
            ) : (
              <div className="flex flex-wrap gap-2">
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
            )
          }
        >
          {!skill ? (
            <Empty>请选择一个 Skill。</Empty>
          ) : (
            <div className="p-4">
              <Textarea
                value={draftMd}
                readOnly={embedded}
                onChange={(event) => setDraftMd(event.target.value)}
                spellCheck={false}
                className={cn(
                  "mono h-[520px] min-h-0 max-h-[520px] resize-none overflow-y-auto [field-sizing:fixed] text-[12px] leading-relaxed",
                  embedded && "bg-muted/40",
                )}
              />
              <div className="mt-2 flex justify-between text-[11px] text-muted-foreground">
                <span>
                  {embedded
                    ? "在工作空间左侧编辑 SKILL.md，改动会作为 Candidate 参与本次实验。"
                    : "实验使用当前编辑内容，不要求先保存。"}
                </span>
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
              <div className="flex flex-wrap gap-2">
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
            <div className="flex flex-wrap items-center gap-2 border-b border-line bg-muted/30 px-4 py-2.5">
              <span className="text-[11px] font-semibold text-muted-foreground">
                指定 Session 重放
              </span>
              <Input
                value={synthesizeSessionIds}
                onChange={(event) => setSynthesizeSessionIds(event.target.value)}
                placeholder="填入已入库的 Session ID（逗号分隔），留空则用近期会话"
                className="h-8 min-w-0 basis-[280px] flex-1 text-xs"
              />
              <span className="text-[10px] text-muted-soft">
                数据源接入的 Session
              </span>
            </div>
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
                              {datasetForEvolution(dataset) && (
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
                              {!!datasetMaterials(dataset).length && (
                                <Pill tone="gray">{datasetMaterials(dataset).length} 个材料</Pill>
                              )}
                              {!!datasetSkillIds(dataset).length && (
                                <Pill tone="gray">{datasetSkillIds(dataset).length} 个 Skills</Pill>
                              )}
                              {dataset.cases.length > 1 && (
                                <Pill tone="gray">{dataset.cases.length} 个任务</Pill>
                              )}
                              <Pill tone="gray">
                                {primaryCase(dataset)?.checks.length || 0} 条 Checklist
                              </Pill>
                            </div>
                            <div className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
                              {primaryCase(dataset)?.query}
                            </div>
                            <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-muted-soft">
                              {primaryCase(dataset)?.provenance.session_id && (
                                <span className="mono">session: {primaryCase(dataset)?.provenance.session_id}</span>
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
      </div>}

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
            <Field label="关联 Skills *" hint="Skills 均为并列关系；当前任务可同时使用所选 Skills。">
              <div className="grid gap-2 sm:grid-cols-2">
                {skills.map((item) => {
                  const checked = datasetDraft.skill_ids.includes(item.name);
                  return (
                    <label
                      key={item.name}
                      className="flex cursor-pointer items-center gap-2 rounded-lg border border-border bg-background p-3 text-sm"
                    >
                      <input
                        type="checkbox"
                        className="size-4"
                        checked={checked}
                        onChange={(event) => setDatasetDraft({
                          ...datasetDraft,
                          skill_ids: event.target.checked
                            ? [...datasetDraft.skill_ids, item.name]
                            : datasetDraft.skill_ids.filter((name) => name !== item.name),
                        })}
                      />
                      <span className="min-w-0 truncate">{item.name}</span>
                    </label>
                  );
                })}
              </div>
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
              value={datasetDraft.outputChecks}
              placeholder="例如：最终产物保存到指定目录"
              onChange={(outputChecks) => setDatasetDraft({
                ...datasetDraft,
                outputChecks,
              })}
            />
            <ChecklistEditor
              label="轨迹要求"
              hint="每条描述一个材料读取、工具调用、计算、校验或产物写入动作。"
              value={datasetDraft.trajectoryChecks}
              placeholder="例如：读取 q1_materials 下的全部材料"
              onChange={(trajectoryChecks) => setDatasetDraft({
                ...datasetDraft,
                trajectoryChecks,
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

      {compact ? (
        <section className="lab-run-list">
          <h3>运行记录 <span>{runs.length}</span></h3>
          {!runs.length && <p>选择数据集后点击运行，Trace 将显示在下方控制台。</p>}
          {runs.map((run) => <button type="button" key={run.run_id} className={cn(activeRun?.run_id === run.run_id && "is-active")} onClick={() => void openRun(run.run_id)}>
            <span><RunStatus run={run} /><span className="truncate">{run.dataset_name || run.dataset_id}</span></span>
            <small>{fmtTime(run.created_at)} <Verdict value={run.result_summary?.verdict} /></small>
          </button>)}
        </section>
      ) : <Panel title="实验运行历史" count={`${runs.length} 次`}>
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
      </Panel>}

      {!compact && activeRun && <RunDetail run={activeRun} onRefresh={() => openRun(activeRun.run_id)} />}
    </div>
  );
}
