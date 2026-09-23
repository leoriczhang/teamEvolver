import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Check, ChevronDown, ChevronRight, ChevronsDownUp, Code2, Download, FileCode2, FilePlus2, Files, FlaskConical, Folder, FolderOpen, GitCompareArrows, Loader2, PanelBottom, PanelLeft, PanelRight, Play, RefreshCw, Save, Search, Terminal, Upload, X } from "lucide-react";
import { api, cloudNote, type CloudResult, type SkillLabRun, type UserProfile } from "@/api/client";
import { MarkdownDocument } from "@/components/MarkdownWorkspace";
import { DiffView } from "@/components/WorkspaceDiffView";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { toastErr, toastOk } from "@/lib/toast";
import { cn } from "@/lib/utils";
import SkillLabView, { RunDetail } from "@/views/SkillLabView";
import SkillTransferModal from "@/views/skills/SkillTransferModal";
import { languageName } from "@/views/workbench/editorLanguage";
import "@/views/workbench/workbench.css";

const CodeEditor = lazy(() => import("@/views/workbench/CodeEditor"));

type SkillFolder = { name: string; relative_path: string; description?: string; files: string[]; version?: number };
type Workspace = { name: string; root: string; skills: SkillFolder[] };
type FileResponse = { path: string; content?: string; sha256?: string; editable: boolean; reason?: string; cloud?: CloudResult };
type Document = FileResponse & { id: string; skill: string; content: string; saved: string; isNew?: boolean };
type FileNode = { path: string; name: string; children: FileNode[]; directory: boolean };
const fileId = (skill: string, path: string) => `${skill}/${path}`;
const modified = (doc: Document) => !!doc.isNew || doc.content !== doc.saved;

function fileTree(paths: string[]): FileNode[] {
  const roots: FileNode[] = [];
  for (const path of paths) {
    let children = roots;
    const parts = path.split("/");
    parts.forEach((name, index) => {
      const currentPath = parts.slice(0, index + 1).join("/");
      let node = children.find((item) => item.path === currentPath);
      if (!node) { node = { path: currentPath, name, directory: index < parts.length - 1, children: [] }; children.push(node); }
      children = node.children;
    });
  }
  const sort = (nodes: FileNode[]): FileNode[] => nodes.sort((a, b) => Number(b.directory) - Number(a.directory) || a.name.localeCompare(b.name)).map((node) => ({ ...node, children: sort(node.children) }));
  return sort(roots);
}

export default function SkillWorkbenchView({ active, user }: {
  active: boolean; user?: UserProfile | null;
}) {
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [documents, setDocuments] = useState<Record<string, Document>>({});
  const docsRef = useRef(documents);
  docsRef.current = documents;
  const [tabs, setTabs] = useState<string[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState("");
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 980);
  const [debug, setDebug] = useState(() => window.innerWidth > 700);
  const [panel, setPanel] = useState(false);
  const [panelTab, setPanelTab] = useState<"trace" | "changes">("trace");
  const [editorMode, setEditorMode] = useState<"code" | "preview" | "diff">("code");
  const [loading, setLoading] = useState(false);
  const [opening, setOpening] = useState("");
  const [savingId, setSavingId] = useState("");
  const [loadError, setLoadError] = useState("");
  const [quickOpen, setQuickOpen] = useState(false);
  const [quickQuery, setQuickQuery] = useState("");
  const [newFileOpen, setNewFileOpen] = useState(false);
  const [transferMode, setTransferMode] = useState<"import" | "export" | null>(null);
  const [newPath, setNewPath] = useState("");
  const [newFileError, setNewFileError] = useState("");
  const [run, setRun] = useState<SkillLabRun | null>(null);
  const [runSignal, setRunSignal] = useState(0);
  const [cursor, setCursor] = useState({ line: 1, column: 1 });
  const cursors = useRef<Record<string, { line: number; column: number }>>({});
  const [explorerWidth, setExplorerWidth] = useState(248);
  const [debugWidth, setDebugWidth] = useState(336);
  const [panelHeight, setPanelHeight] = useState(285);
  const loaded = useRef(false);
  const openSequence = useRef(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const selected = documents[selectedId];
  const skillName = selected?.skill || "";
  const dirtyDocs = Object.values(documents).filter(modified);
  const skillMd = documents[fileId(skillName, "SKILL.md")];
  const canSave = user?.role === "admin";
  const candidateFiles = useMemo(() => Object.fromEntries(Object.values(documents)
    .filter((doc) => doc.skill === skillName && doc.path !== "SKILL.md" && modified(doc))
    .map((doc) => [doc.path, doc.content])), [documents, skillName]);

  const readFile = useCallback(async (skill: string, path: string) => {
    const id = fileId(skill, path);
    if (docsRef.current[id]) return docsRef.current[id];
    const response = await api<FileResponse>(`/api/replay-lab/workspace/${encodeURIComponent(skill)}/file?path=${encodeURIComponent(path)}`);
    const doc: Document = { ...response, id, skill, path, content: response.content ?? "", saved: response.content ?? "" };
    setDocuments((current) => current[id] ? current : { ...current, [id]: doc });
    return doc;
  }, []);

  const openFile = useCallback(async (skill: string, path: string, mode: "code" | "diff" = "code") => {
    const sequence = ++openSequence.current;
    const id = fileId(skill, path);
    setOpening(id);
    try {
      await readFile(skill, path);
      setTabs((current) => current.includes(id) ? current : [...current, id]);
      if (sequence !== openSequence.current) return;
      setSelectedId(id);
      setEditorMode(mode);
      setCursor(cursors.current[id] || { line: 1, column: 1 });
      setExpanded((current) => {
        const next = new Set(current).add(skill);
        const parts = path.split("/");
        for (let i = 1; i < parts.length; i++) next.add(fileId(skill, parts.slice(0, i).join("/")));
        return next;
      });
    } catch (error: any) { toastErr("打开文件失败", error.message); }
    finally { if (sequence === openSequence.current) setOpening(""); }
  }, [readFile]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setLoadError("");
    try {
      const result = await api<Workspace>("/api/replay-lab/workspace");
      if (!Array.isArray(result.skills)) throw new Error("工作区文件列表无效，请刷新重试");
      setWorkspace(result);
    }
    catch (error: any) { setLoadError(error.message); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { if (active && !loaded.current) { loaded.current = true; void refresh(); } }, [active, refresh]);
  useEffect(() => {
    if (!skillName) return;
    void readFile(skillName, "SKILL.md").catch((error) => toastErr("读取 Skill 入口失败", error.message));
  }, [skillName, readFile]);
  useEffect(() => {
    if (!dirtyDocs.length) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirtyDocs.length]);

  async function saveFile() {
    if (!selected || !modified(selected) || savingId || !canSave) return;
    const snapshot = selected;
    setSavingId(snapshot.id);
    try {
      const response = await api<FileResponse>(`/api/replay-lab/workspace/${encodeURIComponent(snapshot.skill)}/file`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: snapshot.path, content: snapshot.content, expected_sha256: snapshot.sha256 ?? null }),
      });
      setDocuments((current) => ({ ...current, [snapshot.id]: { ...current[snapshot.id], sha256: response.sha256, saved: snapshot.content, isNew: false } }));
      toastOk("文件已保存", cloudNote(response.cloud) || snapshot.path);
      void refresh();
    } catch (error: any) { toastErr("保存失败，修改已保留", error.message); }
    finally { setSavingId(""); }
  }

  function closeFile(id: string) {
    const doc = documents[id];
    if (doc && modified(doc) && !window.confirm(`「${doc.path}」尚未保存，关闭并放弃修改？`)) return;
    if (id === savingId) return;
    ++openSequence.current;
    setOpening("");
    const remaining = tabs.filter((item) => item !== id);
    setTabs(remaining);
    if (selectedId === id) setSelectedId(remaining[remaining.length - 1] || "");
    // Keep clean snapshots for Candidate construction; reload on explicit refresh.
    if (doc && modified(doc)) setDocuments((current) => {
      const next = { ...current }; delete next[id]; return next;
    });
  }

  function runSkill() {
    if (!skillName || !skillMd?.editable || opening) return;
    setDebug(true); setPanel(true); setPanelTab("trace");
    setRunSignal((value) => value + 1);
  }

  useEffect(() => {
    if (!active || quickOpen || newFileOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (document.querySelector('[role="dialog"]')) return;
      const mod = event.ctrlKey || event.metaKey;
      if (mod && event.key.toLowerCase() === "s") { event.preventDefault(); void saveFile(); }
      if (mod && event.key.toLowerCase() === "p") { event.preventDefault(); setQuickQuery(""); setQuickOpen(true); }
      if (mod && event.key.toLowerCase() === "b") { event.preventDefault(); setSidebar((value) => !value); }
      if (mod && event.key === "`") { event.preventDefault(); setPanel((value) => !value); }
      if (event.key === "F5") { event.preventDefault(); runSkill(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const onRunChange = useCallback((next: SkillLabRun | null) => {
    setRun(next);
    if (next) { setPanel(true); setPanelTab("trace"); }
  }, []);

  function toggleFolder(id: string) { setExpanded((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; }); }

  function createFile() {
    const path = newPath.trim();
    if (!path || path.startsWith("/") || /[\\:]/.test(path) || path.split("/").some((part) => !part || part === "." || part === "..")) {
      setNewFileError("请输入相对路径，例如 scripts/check.py"); return;
    }
    const id = fileId(skillName, path);
    if (documents[id] || workspace?.skills.find((skill) => skill.name === skillName)?.files.includes(path)) {
      setNewFileError("该文件已存在，请从文件树打开"); return;
    }
    const doc: Document = { id, skill: skillName, path, content: "", saved: "", editable: true, isNew: true };
    setDocuments((current) => ({ ...current, [id]: doc }));
    setTabs((current) => [...current, id]); setSelectedId(id); setEditorMode("code");
    setNewFileOpen(false);
    setExpanded((current) => new Set([...current, skillName, ...path.split("/").slice(0, -1).map((_, index) => fileId(skillName, path.split("/").slice(0, index + 1).join("/")))]));
  }

  async function reloadFile() {
    if (!selected || savingId) return;
    if (modified(selected) && !window.confirm("重新读取会放弃此文件的未保存修改，是否继续？")) return;
    const snapshot = selected;
    try {
      const response = await api<FileResponse>(`/api/replay-lab/workspace/${encodeURIComponent(snapshot.skill)}/file?path=${encodeURIComponent(snapshot.path)}`);
      setDocuments((current) => {
        // Preserve any typing that happened while the read was in flight.
        if (current[snapshot.id]?.content !== snapshot.content) return current;
        return { ...current, [snapshot.id]: { ...snapshot, ...response, content: response.content ?? "", saved: response.content ?? "", isNew: false } };
      });
    } catch (error: any) { toastErr("重新读取失败", error.message); }
  }

  function resize(event: React.PointerEvent<HTMLDivElement>, kind: "explorer" | "debug" | "panel") {
    event.preventDefault();
    const startX = event.clientX, startY = event.clientY;
    const initial = kind === "explorer" ? explorerWidth : kind === "debug" ? debugWidth : panelHeight;
    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = (next: PointerEvent) => {
      if (kind === "explorer") setExplorerWidth(Math.max(180, Math.min(380, initial + next.clientX - startX)));
      else if (kind === "debug") setDebugWidth(Math.max(300, Math.min(480, initial + startX - next.clientX)));
      else setPanelHeight(Math.max(160, Math.min((rootRef.current?.clientHeight || 800) - 260, initial + startY - next.clientY)));
    };
    const stop = () => { target.removeEventListener("pointermove", move); target.removeEventListener("pointerup", stop); target.removeEventListener("pointercancel", stop); };
    target.addEventListener("pointermove", move); target.addEventListener("pointerup", stop); target.addEventListener("pointercancel", stop);
  }

  const files = useMemo(() => (workspace?.skills || []).flatMap((skill) => {
    const paths = new Set([...skill.files, ...Object.values(documents).filter((doc) => doc.skill === skill.name && doc.isNew).map((doc) => doc.path)]);
    return [...paths].map((path) => ({ skill: skill.name, path, id: fileId(skill.name, path) }));
  }), [workspace, documents]);
  const quickFiles = files.filter((file) => file.id.toLowerCase().includes(quickQuery.toLowerCase()));

  function renderNodes(nodes: FileNode[], skill: string, depth: number): ReactNode {
    return nodes.map((node) => {
      const id = fileId(skill, node.path);
      const isOpen = !!filter || expanded.has(id);
      return <div key={id}>
        <button type="button" className={cn("wb-tree-row", id === selectedId && "is-selected")} style={{ paddingLeft: 12 + depth * 14 }}
          title={id} aria-expanded={node.directory ? isOpen : undefined} onClick={() => node.directory ? toggleFolder(id) : void openFile(skill, node.path)}>
          {node.directory ? isOpen ? <ChevronDown /> : <ChevronRight /> : <span className="wb-tree-indent" />}
          {node.directory ? <Folder className="wb-folder-icon" /> : <FileCode2 className={node.name === "SKILL.md" ? "wb-skill-icon" : "wb-file-icon"} />}
          <span>{node.name}</span>{documents[id] && modified(documents[id]) && <i title="未保存" />}
        </button>
        {node.directory && isOpen && renderNodes(node.children, skill, depth + 1)}
      </div>;
    });
  }

  return <div className="skill-workbench-shell">
    <div ref={rootRef} className={cn("skill-workbench", !sidebar && "wb-no-sidebar", !debug && "wb-no-debug")}
      style={{ "--explorer-width": `${explorerWidth}px`, "--debug-width": `${debugWidth}px`, "--panel-height": `${panelHeight}px` } as CSSProperties}>
      <header className="wb-titlebar">
        <span className="wb-project"><FolderOpen />{workspace?.name || "Workspace"}<small>代码工作区</small></span>
        <button type="button" className="wb-command" onClick={() => { setQuickQuery(""); setQuickOpen(true); }}><Search /><span>搜索并打开文件</span><kbd>Ctrl P</kbd></button>
        <div className="wb-layout-actions">
          {canSave && <>
            <IconButton label="导入 Skill（ZIP / Git / 市场）" onClick={() => setTransferMode("import")}><Upload /></IconButton>
            <IconButton label="导出 Skill" onClick={() => setTransferMode("export")}><Download /></IconButton>
          </>}
          <IconButton label="切换资源管理器（Ctrl+B）" pressed={sidebar} onClick={() => setSidebar(!sidebar)}><PanelLeft /></IconButton>
          <IconButton label="切换调试控制台（Ctrl+`）" pressed={panel} onClick={() => setPanel(!panel)}><PanelBottom /></IconButton>
          <IconButton label="切换调试配置" pressed={debug} onClick={() => setDebug(!debug)}><PanelRight /></IconButton>
        </div>
      </header>
      <div className="wb-body">
        <aside className="wb-activity" aria-label="工作区工具">
          <IconButton label="资源管理器" pressed={sidebar} onClick={() => setSidebar(!sidebar)}><Files /></IconButton>
          <IconButton label="搜索文件" onClick={() => { setSidebar(true); requestAnimationFrame(() => searchRef.current?.focus()); }}><Search /></IconButton>
          <IconButton label="运行与调试" pressed={debug} onClick={() => setDebug(!debug)}><Play /></IconButton>
          <IconButton label={`查看修改（${dirtyDocs.length}）`} pressed={panel && panelTab === "changes"} onClick={() => { setPanel(true); setPanelTab("changes"); }}><GitCompareArrows />{!!dirtyDocs.length && <b>{dirtyDocs.length}</b>}</IconButton>
          <span className="wb-activity-spacer" />
          <IconButton label="刷新文件树" onClick={() => void refresh()}><RefreshCw className={loading ? "animate-spin" : ""} /></IconButton>
        </aside>
        <aside className="wb-explorer" aria-label="资源管理器">
          <div className="wb-section-heading"><span>资源管理器 <small>EXPLORER</small></span><div>
            <IconButton label="新建文件" disabled={!skillName} onClick={() => { setNewPath(""); setNewFileError(""); setNewFileOpen(true); }}><FilePlus2 /></IconButton>
            <IconButton label="折叠所有文件夹" onClick={() => setExpanded(new Set())}><ChevronsDownUp /></IconButton>
          </div></div>
          <div className="wb-filter"><Search /><input ref={searchRef} aria-label="筛选工作区文件" placeholder="筛选文件…" value={filter} onChange={(event) => setFilter(event.target.value)} />{filter && <IconButton label="清除筛选" onClick={() => setFilter("")}><X /></IconButton>}</div>
          <div className="wb-folder-root" title={workspace?.root}><ChevronDown /><FolderOpen /><span>{workspace?.name || "skills"}</span><small>{workspace?.skills.length ?? 0}</small></div>
          <div className="wb-tree">
            {loading && !workspace && <p className="wb-muted">正在读取团队技能库…</p>}
            {loadError && <div className="wb-error">{loadError}<button onClick={() => void refresh()}>重试</button></div>}
            {workspace?.skills.map((skill) => {
              const matching = files.filter((file) => file.skill === skill.name && file.id.toLowerCase().includes(filter.toLowerCase()));
              if (filter && !matching.length) return null;
              const isOpen = !!filter || expanded.has(skill.name);
              return <div key={skill.name}><button type="button" className="wb-tree-row wb-skill-folder" aria-expanded={isOpen} title={skill.description || skill.relative_path} onClick={() => toggleFolder(skill.name)}>
                {isOpen ? <ChevronDown /> : <ChevronRight />}{isOpen ? <FolderOpen className="wb-folder-icon" /> : <Folder className="wb-folder-icon" />}<span>{skill.name}</span>
                {!!skill.version && <small className="wb-skill-version" title="团队技能库当前版本">v{skill.version}</small>}
              </button>{isOpen && renderNodes(fileTree(matching.map((file) => file.path)), skill.name, 1)}</div>;
            })}
            {workspace && !(filter ? files.filter((file) => file.id.toLowerCase().includes(filter.toLowerCase())) : files).length && <p className="wb-muted">{filter ? "没有匹配的文件" : "团队技能库中暂无 Skill。"}</p>}
          </div>
          <footer className="wb-explorer-footer" title={workspace?.root}><Folder />{workspace?.root || "正在连接工作区"}</footer>
          <div role="separator" aria-label="调整资源管理器宽度" aria-orientation="vertical" className="wb-resize wb-resize-right" onPointerDown={(event) => resize(event, "explorer")} />
        </aside>
        <main className="wb-main">
          <div className="wb-editor-area">
            <div className="wb-tabs" role="tablist" aria-label="打开的文件">
              {!tabs.length && <span className="wb-empty-tab">开始</span>}
              {tabs.map((id) => documents[id] && <div key={id} className={cn("wb-tab", selectedId === id && "is-active")}>
                <button type="button" role="tab" aria-selected={id === selectedId} title={id} onClick={() => { ++openSequence.current; setOpening(""); setSelectedId(id); setCursor(cursors.current[id] || { line: 1, column: 1 }); setEditorMode("code"); }}><FileCode2 /><span>{documents[id].path.split("/").pop()}</span>{modified(documents[id]) && <i title="未保存" />}</button>
                <button type="button" className="wb-tab-close" aria-label={`关闭 ${id}`} disabled={savingId === id} onClick={() => closeFile(id)}><X /></button>
              </div>)}
            </div>
            <div className="wb-editor-toolbar">
              <div className="wb-breadcrumb" title={selectedId}>{selected ? <><span>{selected.skill}</span><ChevronRight /><span>{selected.path}</span></> : <span>选择一个 Skill 开始</span>}</div>
              {selected && <div className="wb-editor-actions">
                <IconButton label="代码" pressed={editorMode === "code"} onClick={() => setEditorMode("code")}><Code2 /></IconButton>
                {selected.path.endsWith(".md") && <IconButton label="Markdown 预览" pressed={editorMode === "preview"} onClick={() => setEditorMode("preview")}><Files /></IconButton>}
                <IconButton label="查看文件差异" pressed={editorMode === "diff"} onClick={() => setEditorMode("diff")}><GitCompareArrows /></IconButton>
                <IconButton label="从技能库重新读取文件" disabled={!!savingId} onClick={() => void reloadFile()}><RefreshCw /></IconButton>
                <IconButton label={canSave ? "保存文件（Ctrl+S），写回技能库" : "仅管理员可保存；当前修改可参与调试"} disabled={!canSave || !modified(selected) || !!savingId || !selected.editable} onClick={() => void saveFile()}>{savingId === selectedId ? <Loader2 className="animate-spin" /> : <Save />}</IconButton>
                <button className="wb-run-button" disabled={!skillMd?.editable || !!opening} onClick={runSkill}><Play />运行调试<kbd>F5</kbd></button>
              </div>}
            </div>
            <div className="wb-editor-content">
              {opening ? <div className="wb-centered"><Loader2 className="animate-spin" /><span>正在打开 {opening}</span></div>
                : selected ? !selected.editable ? <div className="wb-centered"><FileCode2 /><p>{selected.reason}</p></div>
                : editorMode === "preview" ? <div className="wb-preview"><MarkdownDocument content={selected.content} /></div>
                : editorMode === "diff" ? <DiffView original={selected.saved} next={selected.content} />
                : null
                : <div className="wb-welcome"><div className="wb-welcome-mark"><Code2 /></div><span className="wb-eyebrow">SKILL WORKSPACE</span><h2>从一个文件，开始一次实验。</h2><p>打开 Skill 文件夹，编辑代码，<br />用真实执行结果验证每一次修改。</p>
                  {workspace && !workspace.skills.length && canSave
                    ? <button onClick={() => setTransferMode("import")}><Upload />导入 Skill 代码<ChevronRight /></button>
                    : <button onClick={() => { setQuickQuery(""); setQuickOpen(true); }}><Search />打开文件<kbd>Ctrl P</kbd></button>}
                  {workspace && !workspace.skills.length && <span className="wb-import-hint">{canSave ? "支持 ZIP 文件夹包、Git 仓库与 Skill 市场" : "请联系管理员导入 Skill 代码"}</span>}
                  {!!workspace?.skills.length && <button onClick={() => void openFile(workspace.skills[0].name, "SKILL.md")}><FileCode2 />打开第一个 SKILL.md<ChevronRight /></button>}
                  <div className="wb-shortcuts"><span>保存文件<kbd>Ctrl S</kbd></span><span>运行调试<kbd>F5</kbd></span><span>搜索内容<kbd>Ctrl F</kbd></span></div>
                </div>}
              <Suspense fallback={<div className="wb-centered">正在加载代码编辑器…</div>}>
                {tabs.map((id) => {
                  const doc = documents[id];
                  return doc?.editable ? <div key={id} className="wb-live-editor" style={{ display: id === selectedId && editorMode === "code" && !opening ? "block" : "none" }}>
                    <CodeEditor path={doc.path} value={doc.content} onCursor={(position) => { cursors.current[id] = position; if (id === selectedId) setCursor(position); }} onChange={(content) => setDocuments((current) => ({ ...current, [id]: { ...current[id], content } }))} />
                  </div> : null;
                })}
              </Suspense>
            </div>
          </div>
          {panel && <section className="wb-panel" aria-label="调试控制台">
            <div role="separator" aria-label="调整控制台高度" aria-orientation="horizontal" className="wb-resize wb-resize-top" onPointerDown={(event) => resize(event, "panel")} />
            <div className="wb-panel-tabs"><button className={cn(panelTab === "trace" && "is-active")} onClick={() => setPanelTab("trace")}><Terminal />调试控制台{run?.status === "running" && <Loader2 className="animate-spin" />}</button><button className={cn(panelTab === "changes" && "is-active")} onClick={() => setPanelTab("changes")}>修改 <span>{dirtyDocs.length}</span></button><IconButton label="关闭控制台" onClick={() => setPanel(false)}><X /></IconButton></div>
            <div className="wb-panel-content">{panelTab === "trace" ? run ? <RunDetail run={run} onRefresh={() => void api<SkillLabRun>(`/api/replay-lab/runs/${encodeURIComponent(run.run_id)}`).then(setRun).catch((error) => toastErr("读取运行结果失败", error.message))} /> : <div className="wb-console-empty"><Terminal /><div><strong>等待运行 Skill</strong><p>选择右侧数据集并运行。执行状态、工具调用和完整 Trace 会显示在这里。</p></div></div>
              : dirtyDocs.length ? dirtyDocs.map((doc) => <button className="wb-change-row" key={doc.id} onClick={() => { void openFile(doc.skill, doc.path, "diff"); }}><FileCode2 /><span>{doc.id}</span><small>{doc.isNew ? "新增" : "已修改"}</small></button>) : <div className="wb-console-empty"><Check /><span>所有打开的文件均已保存</span></div>}</div>
          </section>}
        </main>
        <aside className="wb-debug" aria-label="Skill 调试配置">
          <div role="separator" aria-label="调整调试配置宽度" aria-orientation="vertical" className="wb-resize wb-resize-left" onPointerDown={(event) => resize(event, "debug")} />
          {skillName && skillMd?.editable ? <SkillLabView key={skillName} active={active} user={user} compact embedded lockedSkillName={skillName} externalDraftMd={skillMd.content} externalFiles={candidateFiles} onRunChange={onRunChange} runSignal={runSignal} />
            : <div className="wb-debug-empty"><div className="wb-section-heading">运行与调试</div><FlaskConical /><h3>让修改经过真实验证</h3><p>打开一个 Skill 文件，选择 Test Dataset，即可运行 True Replay。</p><ol><li>打开并编辑代码</li><li>选择或新建数据集</li><li>运行后查看分支 Trace</li></ol></div>}
        </aside>
      </div>
      <footer className="wb-statusbar"><span><span className={cn("wb-status-dot", loadError && "has-error")} />{loadError ? "连接异常" : workspace ? "Workspace 已连接" : "连接中"}</span><span>{dirtyDocs.length ? `${dirtyDocs.length} 个文件未保存` : "所有修改已保存"}</span><span className="wb-status-spacer" />{selected && <><span>Ln {cursor.line}, Col {cursor.column}</span><span>UTF-8</span><span>{languageName(selected.path)}</span></>}</footer>
    </div>
    {transferMode && <SkillTransferModal direction={transferMode} selectedName={skillName} onClose={() => setTransferMode(null)} onImported={() => void refresh()} />}
    <Dialog open={quickOpen} onOpenChange={setQuickOpen}><DialogContent className="wb-quick-dialog"><DialogHeader><DialogTitle>快速打开文件</DialogTitle></DialogHeader><Input autoFocus value={quickQuery} placeholder="输入文件名或路径…" aria-label="快速打开文件搜索" onChange={(event) => setQuickQuery(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && quickFiles[0]) { setQuickOpen(false); void openFile(quickFiles[0].skill, quickFiles[0].path); } }} /><div className="wb-quick-results">{quickFiles.slice(0, 80).map((file) => <button key={file.id} onClick={() => { setQuickOpen(false); void openFile(file.skill, file.path); }}><FileCode2 /><span>{file.path.split("/").pop()}<small>{file.id}</small></span></button>)}{!quickFiles.length && <p>没有匹配的文件</p>}</div></DialogContent></Dialog>
    <Dialog open={newFileOpen} onOpenChange={setNewFileOpen}><DialogContent><DialogHeader><DialogTitle>在 {skillName} 中新建文件</DialogTitle></DialogHeader><Input autoFocus aria-label="新文件相对路径" placeholder="例如 scripts/check.py" value={newPath} onChange={(event) => setNewPath(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") createFile(); }} /><p className="text-xs text-muted-foreground">子文件夹会在保存时自动创建。</p>{newFileError && <p className="text-xs text-destructive">{newFileError}</p>}<Button onClick={createFile}>创建文件</Button></DialogContent></Dialog>
  </div>;
}

function IconButton({ label, children, onClick, pressed, disabled }: { label: string; children: ReactNode; onClick: () => void; pressed?: boolean; disabled?: boolean }) {
  return <button type="button" className="wb-icon-button" title={label} aria-label={label} aria-pressed={pressed} onClick={onClick} disabled={disabled}>{children}</button>;
}
