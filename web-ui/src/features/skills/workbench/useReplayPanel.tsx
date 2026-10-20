import { api } from "@/api/client";
import { type UserProfile } from "@/features/admin/api/client";
import { type WorkbenchDataset,type WorkbenchMaterial,type WorkbenchRun } from "@/features/replay/api/client";
import { isActiveRun } from "@/features/replay/api/datasets";
import { type SkillDetail,type SkillListResp } from "@/features/skills/api/client";
import { runSkillCommand } from "@/features/skills/api/skillJobs";
import { fileToB64 } from "@/lib/file";
import { toastErr,toastOk } from "@/lib/toast";
import { useCallback,useEffect,useRef,useState } from "react";
import { checklistLines,checkText,commonMaterialRoot,DatasetDraft,datasetForEvolution,datasetMaterials,datasetSkillIds,EMPTY_DATASET,hasInlineMaterialSection,inlineMaterialText,primaryCase } from "./ReplayPanelShared";

export function useReplayPanel({
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
const [skills, setSkills] = useState<SkillListResp["skills"]>([]);
const [skillName, setSkillName] = useState(lockedSkillName);
const [skill, setSkill] = useState<SkillDetail | null>(null);
const [draftMd, setDraftMd] = useState(externalDraftMd ?? "");
const [datasets, setDatasets] = useState<WorkbenchDataset[]>([]);
const [selectedDatasetId, setSelectedDatasetId] = useState("");
const [runs, setRuns] = useState<WorkbenchRun[]>([]);
const [activeRun, setActiveRun] = useState<WorkbenchRun | null>(null);
const [loading, setLoading] = useState(false);
const [running, setRunning] = useState(false);
const [savingSkill, setSavingSkill] = useState(false);
const [showDatasetForm, setShowDatasetForm] = useState(false);
const [datasetDraft, setDatasetDraft] = useState<DatasetDraft>(EMPTY_DATASET);
const [datasetFiles, setDatasetFiles] = useState<WorkbenchMaterial[]>([]);
const [existingMaterials, setExistingMaterials] = useState<WorkbenchMaterial[]>([]);
const [materialRoot, setMaterialRoot] = useState("");
const [filesTouched, setFilesTouched] = useState(false);
const [savingDataset, setSavingDataset] = useState(false);
const [importingDataset, setImportingDataset] = useState(false);
const [synthesizing, setSynthesizing] = useState(false);
const [synthesizeSessionIds, setSynthesizeSessionIds] = useState("");
const [maxInteractions, setMaxInteractions] = useState(8);
const [timeoutSeconds, setTimeoutSeconds] = useState(600);
const loaded = useRef(false);
const datasetImportRef = useRef<HTMLInputElement>(null);
const externalDraftRef = useRef(externalDraftMd);
externalDraftRef.current = externalDraftMd;
const dirty = !!skill && draftMd !== (skill.skill_md || "");
const isAdmin = user?.role === "admin";
const loadSkill = useCallback(async (name: string) => {
    if (!name) {
      setSkill(null);
      if (externalDraftRef.current == null) setDraftMd("");
      return;
    }
    const detail = await api<SkillDetail>(`/api/replay-lab/skills/${encodeURIComponent(name)}`);
    setSkill(detail);
    // Embedded evaluation keeps the caller-provided draft as the candidate;
    // standalone lab initialises the editor from the stored skill.
    if (externalDraftRef.current == null) setDraftMd(detail.skill_md || "");
  }, []);
const loadWorkspace = useCallback(async (name: string) => {
    if (!name) {
      setDatasets([]);
      setRuns([]);
      return;
    }
    const [datasetResp, runResp] = await Promise.all([
      api<{ datasets: WorkbenchDataset[] }>(
        `/api/replay-lab/datasets?skill_id=${encodeURIComponent(name)}`
      ),
      api<{ runs: WorkbenchRun[] }>(
        `/api/replay-lab/runs?skill_id=${encodeURIComponent(name)}&limit=100`
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
      const skillResp = await api<SkillListResp>("/api/replay-lab/skills");
      const nextSkills = skillResp.skills || [];
      setSkills(nextSkills);
      const nextName = (
        lockedSkillName
          ? lockedSkillName
          : skillName && nextSkills.some((item) => item.name === skillName)
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
  }, [loadSkill, loadWorkspace, skillName, lockedSkillName]);
useEffect(() => {
    if (active && !loaded.current) {
      loaded.current = true;
      refresh();
    }
  }, [active, refresh]);
useEffect(() => {
    if (!lockedSkillName || !active || lockedSkillName === skillName) return;
    setSkillName(lockedSkillName);
    void Promise.all([loadSkill(lockedSkillName), loadWorkspace(lockedSkillName)]);
  }, [lockedSkillName, active, loadSkill, loadWorkspace]);
useEffect(() => {
    if (externalDraftMd != null) setDraftMd(externalDraftMd);
  }, [externalDraftMd]);
useEffect(() => {
    if (!active || !runs.some((run) => isActiveRun(run.status))) return;
    const timer = window.setInterval(async () => {
      try {
        const response = await api<{ runs: WorkbenchRun[] }>(
          `/api/replay-lab/runs?skill_id=${encodeURIComponent(skillName)}&limit=100`
        );
        setRuns(response.runs || []);
        if (activeRun?.run_id) {
          const detail = await api<WorkbenchRun>(
            `/api/replay-lab/runs/${encodeURIComponent(activeRun.run_id)}`
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
      await api(`/api/replay-lab/skills/${encodeURIComponent(skillName)}/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_skill_md: draftMd, expected_sha256: skill?.sha256 }),
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
    setDatasetDraft({ ...EMPTY_DATASET, skill_ids: skillName ? [skillName] : [] });
    setDatasetFiles([]);
    setExistingMaterials([]);
    setMaterialRoot("");
    setFilesTouched(true);
    setShowDatasetForm(true);
  }
function editDataset(dataset: WorkbenchDataset) {
    const datasetCase = primaryCase(dataset);
    setDatasetDraft({
      dataset_id: dataset.dataset_id,
      skill_ids: datasetSkillIds(dataset).length ? datasetSkillIds(dataset) : (skillName ? [skillName] : []),
      name: dataset.name || "",
      query: datasetCase?.query || "",
      outputChecks: checkText(dataset, "output"),
      trajectoryChecks: checkText(dataset, "trajectory"),
      disclosure_batch_size: datasetCase?.replay?.progressive_disclosure?.batch_size || 4,
      enabled_for_evolution: datasetForEvolution(dataset),
    });
    setDatasetFiles([]);
    setExistingMaterials(datasetMaterials(dataset));
    setMaterialRoot(commonMaterialRoot(datasetMaterials(dataset)));
    setFilesTouched(false);
    setShowDatasetForm(true);
  }
async function uploadDatasetFiles(files: FileList) {
    try {
      const payload: WorkbenchMaterial[] = [];
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
    if (!datasetDraft.skill_ids.length || !datasetDraft.query.trim()) {
      toastErr("请选择至少一个 Skill 并填写实验 Query");
      return;
    }
    setSavingDataset(true);
    try {
      const output = checklistLines(datasetDraft.outputChecks);
      const trajectory = checklistLines(datasetDraft.trajectoryChecks);
      const payload: Record<string, any> = {
        schema_version: "team-replay.dataset",
        dataset_id: datasetDraft.dataset_id,
        name: datasetDraft.name,
        description: "",
        skills: datasetDraft.skill_ids.map((skill_id) => ({ skill_id })),
        source: editingDataset?.source || { kind: "manual" },
        cases: [{
          case_id: primaryCase(editingDataset)?.case_id || `case-${crypto.randomUUID()}`,
          query: datasetDraft.query,
          skill_ids: datasetDraft.skill_ids,
          checks: [
            ...output.map((text, index) => ({ id: `R${String(index + 1).padStart(2, "0")}`, kind: "output", text })),
            ...trajectory.map((text, index) => ({ id: `T${String(index + 1).padStart(2, "0")}`, kind: "trajectory", text })),
          ],
          materials: existingMaterials,
          provenance: primaryCase(editingDataset)?.provenance || {},
          replay: {
            progressive_disclosure: {
              enabled: true,
              initial_visibility: "query_only",
              batch_size: datasetDraft.disclosure_batch_size,
              stop_when: "all_checklist_items_satisfied",
            },
          },
          metadata: primaryCase(editingDataset)?.metadata || {},
        }],
        created_at: editingDataset?.created_at,
        updated_at: new Date().toISOString(),
        metadata: {
          ...(editingDataset?.metadata || {}),
          read_only: false,
          enabled_for_evolution: datasetDraft.enabled_for_evolution,
        },
      };
      if (!payload.dataset_id) delete payload.dataset_id;
      if (!payload.created_at) delete payload.created_at;
      if (editingDataset?.revision) payload.expected_revision = editingDataset.revision;
      if (filesTouched) payload.files = datasetFiles;
      const saved = await api<WorkbenchDataset>("/api/replay-lab/datasets", {
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
      const definition = entries.find((file) => file.name === "dataset.json");
      if (!definition) {
        toastErr("导入失败", "请选择包含 dataset.json 的数据集目录");
        return;
      }
      const document = JSON.parse(await definition.text());
      if (document.schema_version !== "team-replay.dataset" || !Array.isArray(document.cases)) {
        throw new Error("数据集必须使用 team-replay.dataset 和 cases[]");
      }
      const materials: WorkbenchMaterial[] = [];
      for (const file of entries) {
        if (file === definition) continue;
        materials.push({
          path: file.webkitRelativePath || file.name,
          size: file.size,
          content_b64: await fileToB64(file),
        });
      }
      const saved = await api<WorkbenchDataset>("/api/replay-lab/datasets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...document,
          dataset_id: `import-${crypto.randomUUID()}`,
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
async function cloneDataset(dataset: WorkbenchDataset) {
    try {
      const cloned = await api<WorkbenchDataset>(
        `/api/replay-lab/datasets/${encodeURIComponent(dataset.dataset_id)}/clone`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        }
      );
      toastOk("已复制为可编辑数据集", cloned.name);
      await loadWorkspace(skillName);
      setSelectedDatasetId(cloned.dataset_id);
    } catch (error: any) {
      toastErr("复制数据集失败", error.message);
    }
  }
async function deleteDataset(dataset: WorkbenchDataset) {
    if (!window.confirm(`确认删除数据集「${dataset.name}」？`)) return;
    try {
      await api(
        `/api/replay-lab/datasets/${encodeURIComponent(dataset.dataset_id)}?skill_id=${encodeURIComponent(skillName)}`,
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
      const run = await api<WorkbenchRun>("/api/replay-lab/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          skill_id: skillName,
          dataset_id: selectedDatasetId,
          candidate_skill_md: draftMd,
          candidate_files: externalFiles,
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
      const sessionIds = synthesizeSessionIds
        .split(/[\s,，]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const result = await runSkillCommand<{ datasets: WorkbenchDataset[]; count: number; source_session_count: number }>(
        "/api/replay-lab/datasets/synthesize",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            skill_id: skillName,
            ...(sessionIds.length ? { session_ids: sessionIds } : {}),
          }),
        },
        "/api/replay-lab",
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
      const detail = await api<WorkbenchRun>(
        `/api/replay-lab/runs/${encodeURIComponent(runId)}`
      );
      setActiveRun(detail);
    } catch (error: any) {
      toastErr("加载实验详情失败", error.message);
    }
  }
useEffect(() => { onRunChange?.(activeRun); }, [activeRun, onRunChange]);
useEffect(() => {
    if (runSignal > 0 && active && !running && skill) void startRun();
  }, [runSignal]);
const manualCount = datasets.filter((item) => item.source?.kind !== "evolution").length;
const evolvedCount = datasets.filter((item) => item.source?.kind === "evolution").length;
const regressionCount = datasets.filter(datasetForEvolution).length;
const completedCount = runs.filter((run) => run.status === "succeeded").length;
const selectedDataset = datasets.find(
    (item) => item.dataset_id === selectedDatasetId
  );
const editingDataset = datasets.find(
    (item) => item.dataset_id === datasetDraft.dataset_id
  );
const hasInlineMaterial = hasInlineMaterialSection(datasetDraft.query);
const inlineMaterial = inlineMaterialText(datasetDraft.query);
return { skills, skillName, skill, draftMd, setDraftMd, datasets, selectedDatasetId, setSelectedDatasetId, runs, activeRun, loading, running, savingSkill, showDatasetForm, setShowDatasetForm, datasetDraft, setDatasetDraft, datasetFiles, existingMaterials, materialRoot, setMaterialRoot, filesTouched, savingDataset, importingDataset, synthesizing, synthesizeSessionIds, setSynthesizeSessionIds, maxInteractions, setMaxInteractions, timeoutSeconds, setTimeoutSeconds, datasetImportRef, dirty, isAdmin, refresh, selectSkill, saveSkill, createDataset, editDataset, uploadDatasetFiles, saveDataset, importDatasetPackage, cloneDataset, deleteDataset, startRun, synthesizeDatasets, openRun, manualCount, evolvedCount, regressionCount, completedCount, selectedDataset, editingDataset, hasInlineMaterial, inlineMaterial };
}
