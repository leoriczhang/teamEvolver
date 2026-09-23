import { useEffect, useState, type ReactNode } from "react";
import { api, getActiveTenantId, tenantHeaders } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { fileToB64 } from "@/lib/file";
import { toastErr, toastOk } from "@/lib/toast";

type Channel = "zip" | "marketplace" | "git";
type Direction = "import" | "export";
type Options = Record<string, string | boolean>;
interface TransferResult {
  imported?: { name: string; status: string; version: number }[];
  skipped?: { name: string; reason: string }[];
  errors?: { name: string; error: string }[];
  exported?: string[];
  url?: string;
  branch?: string;
  commit?: string;
  version?: string;
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block space-y-1.5 text-xs font-medium"><span>{label}</span>{children}</label>;
}
const selectClass = "h-9 w-full rounded-md border border-border bg-background px-3 text-sm";

export default function SkillTransferModal({ direction, onClose, onImported, selectedName = "" }: {
  direction: Direction | null;
  onClose: () => void;
  onImported: () => void;
  selectedName?: string;
}) {
  const [channel, setChannel] = useState<Channel>("zip");
  const [options, setOptions] = useState<Options>({});
  const [conflict, setConflict] = useState("replace");
  const [skills, setSkills] = useState<{ name: string; version?: number }[]>([]);
  const [names, setNames] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [fileLabel, setFileLabel] = useState("");
  const [result, setResult] = useState<TransferResult | null>(null);
  const [error, setError] = useState("");
  const importing = direction === "import";
  const marketProvider = String(options.provider || "clawhub");
  const gitMode = String(options.mode || "new_branch");
  const tenant = getActiveTenantId();

  useEffect(() => {
    let cancelled = false;
    setOptions({}); setResult(null); setError(""); setFileLabel(""); setChannel("zip");
    setNames(selectedName ? [selectedName] : []); setConflict("replace"); setSkills([]);
    if (direction === "export") {
      setLoading(true);
      api<{ skills: { name: string; version?: number }[] }>("/api/skills/transfer/skills")
        .then(r => { if (!cancelled) setSkills(r.skills); })
        .catch(e => { if (!cancelled) setError(e.message); })
        .finally(() => { if (!cancelled) setLoading(false); });
    }
    return () => { cancelled = true; };
  }, [direction, tenant]);

  function change(key: string, value: string | boolean) {
    setOptions(previous => ({ ...previous, [key]: value })); setResult(null); setError("");
  }
  function input(key: string, label: string, placeholder = "", type = "text") {
    return <Field label={label}><Input value={String(options[key] || "")} type={type}
      autoComplete={type === "password" ? "new-password" : "off"} placeholder={placeholder}
      onChange={e => change(key, e.target.value)} /></Field>;
  }
  function choose(key: string, label: string, values: [string, string][], fallback: string) {
    return <Field label={label}><select className={selectClass} value={String(options[key] || fallback)}
      onChange={e => change(key, e.target.value)}>{values.map(([value, text]) =>
        <option key={value} value={value}>{text}</option>)}</select></Field>;
  }

  async function pickFile(file?: File) {
    setResult(null); setError(""); change("zip_b64", "");
    if (!file) return;
    if (file.size > 64 * 1024 * 1024) { setError("ZIP 大小不能超过 64 MiB"); return; }
    setLoading(true);
    try { change("zip_b64", await fileToB64(file)); setFileLabel(file.name); }
    catch { setError("无法读取 ZIP 文件"); }
    finally { setLoading(false); }
  }

  async function submit() {
    setBusy(true); setResult(null); setError("");
    const startedTenant = tenant;
    try {
      const path = `/api/skills/${direction}`;
      const body = importing ? { channel, options, conflict } : { channel, options, names };
      // Imports/Git can exceed the generic client's short timeout. A transfer is
      // a single user action: don't retry writes automatically after a timeout.
      const response = await fetch(path, { method: "POST", headers: tenantHeaders(path, { "Content-Type": "application/json" }),
        body: JSON.stringify(body), signal: AbortSignal.timeout(1800000) });
      if (!response.ok) {
        const value = await response.json().catch(() => ({}));
        throw new Error(typeof value.detail === "string" ? value.detail : `操作失败（${response.status}）`);
      }
      if (getActiveTenantId() !== startedTenant) return;
      if (!importing && channel === "zip") {
        const url = URL.createObjectURL(await response.blob());
        const link = document.createElement("a");
        link.href = url; link.download = names.length === 1 ? `${names[0]}.zip` : "skills.zip";
        document.body.append(link); link.click(); link.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        setResult({ exported: names }); toastOk("ZIP 已生成");
      } else {
        const value = await response.json() as TransferResult;
        setResult(value);
        if (importing) onImported();
        if (!value.errors?.length) toastOk(importing ? "Skill 导入完成" : "Skill 导出完成");
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "操作失败";
      setError(message); toastErr("Skill 传输失败", message);
    } finally { setBusy(false); }
  }

  return <Dialog open={direction !== null} onOpenChange={open => { if (!open && !busy) onClose(); }}>
    <DialogContent className="w-full !max-w-[680px] max-h-[90vh] overflow-y-auto">
      <DialogHeader><DialogTitle>{importing ? "导入团队 Skill" : "导出团队 Skill"}</DialogTitle></DialogHeader>
      <fieldset disabled={busy} className="space-y-4 disabled:opacity-70">
        <Field label={importing ? "导入来源" : "导出目标"}>
          <select className={selectClass} value={channel} onChange={e => {
            setChannel(e.target.value as Channel); setOptions({}); setResult(null); setError(""); setFileLabel("");
          }}>
            <option value="zip">{importing ? "ZIP 包上传" : "ZIP 包下载"}</option>
            <option value="marketplace">{importing ? "Skill 市场拉取" : "Skill 市场上传"}</option>
            <option value="git">{importing ? "Git 仓库同步" : "Git 新建上传"}</option>
          </select>
        </Field>
        {!importing && <fieldset className="space-y-1.5 text-xs font-medium"><legend>选择 Skill（已选 {names.length}）</legend>
          <div className="max-h-44 overflow-auto rounded-md border border-border p-2 space-y-1">
            {loading ? <p>加载 Skill 列表…</p> : skills.length ? <>
              <label className="flex gap-2 p-1"><input type="checkbox" checked={names.length === skills.length}
                onChange={e => setNames(e.target.checked ? skills.map(s => s.name) : [])} />全选</label>
              {skills.map(skill => <label className="flex items-center gap-2 p-1" key={skill.name}>
                <input type="checkbox" aria-label={skill.name} checked={names.includes(skill.name)} onChange={e => setNames(previous =>
                  e.target.checked ? [...previous, skill.name] : previous.filter(n => n !== skill.name))} />
                <span>{skill.name}</span>{!!skill.version && <span className="text-muted-foreground">v{skill.version}</span>}
              </label>)}
            </> : <p className="p-2 text-muted-foreground">当前团队没有可导出的 Skill。</p>}
          </div>
        </fieldset>}
        {channel === "zip" && importing && <>
          <Field label="ZIP 文件"><Input type="file" accept=".zip" onChange={e => void pickFile(e.target.files?.[0])} /></Field>
          <p className="text-xs text-muted-foreground">{fileLabel || "支持单个 Skill 或包含多个 Skill 目录的 ZIP；最大 64 MiB。"}</p>
        </>}
        {channel === "marketplace" && <>
          {choose("provider", "Skill 市场", [["clawhub", "ClawHub"], ["http", "自定义 HTTP 市场"]], "clawhub")}
          {marketProvider === "clawhub" ? <>
            {input("registry_url", "市场地址", "https://clawhub.ai")}
            {input("slug", "Skill 标识（slug）", importing ? "市场中的 Skill 标识" : "留空使用选中的 Skill 名称")}
            {input("version", importing ? "版本（可选）" : "上传版本", importing ? "留空拉取最新版本" : "1.0.0")}
            {!importing && <p className="text-xs text-muted-foreground">ClawHub 每次上传一个 Skill，上传版本号需为新版本。</p>}
          </> : <>
            {input(importing ? "download_url" : "upload_url", importing ? "ZIP 下载地址" : "ZIP 上传地址", "https://")}
            {!importing && input("file_field", "上传文件字段名", "file")}
            <p className="text-xs text-muted-foreground">下载接口需返回 ZIP；上传接口接收 multipart ZIP 并返回成功 JSON。</p>
          </>}
          {input("token", importing ? "访问 Token（可选）" : "上传 Token", "仅用于本次操作", "password")}
        </>}
        {channel === "git" && <>
          {!importing && choose("mode", "Git 上传方式", [["new_branch", "已有仓库中新建分支"], ["new_repository", "新建仓库并上传"]], "new_branch")}
          {importing || gitMode === "new_branch" ? <>
            {input("url", "Git 仓库地址", "https://git.example.com/team/skills.git")}
            {input("branch", importing ? "来源分支（可选）" : "基础分支（可选）", "留空使用仓库默认分支")}
            {importing && input("commit", "指定 commit（可选）", "留空使用分支最新提交")}
          </> : <>
            {choose("provider", "Git 托管平台", [["github", "GitHub"], ["gitlab", "GitLab"]], "github")}
            {input("api_url", "平台 API 地址（可选）", options.provider === "gitlab" ? "https://gitlab.com/api/v4" : "https://api.github.com")}
            {input("repo_name", "新仓库名称", "team-skills")}
            {input("namespace", options.provider === "gitlab" ? "命名空间 ID（可选）" : "组织名称（可选）", "留空使用当前账号")}
            <label className="flex items-center gap-2 text-xs"><input type="checkbox" checked={options.private !== false}
              onChange={e => change("private", e.target.checked)} />创建私有仓库</label>
          </>}
          {!importing && input("new_branch", "新分支名（可选）", gitMode === "new_repository" ? "main" : "自动生成唯一分支名")}
          {input("path", "Skill 目录（相对仓库根目录）", "skills（填写 . 表示仓库根目录）")}
          {input("username", "Git 用户名（可选）", "Token 对应的 Git 用户名")}
          {input("token", "密码 / Access Token（私有仓库或新建仓库时填写）", "仅用于本次操作", "password")}
          {importing && <p className="text-xs text-muted-foreground">执行一次同步，导入指定目录中的 Skill 并记录 commit。</p>}
        </>}
        {importing && <>
          {input("name", "导入名称（仅单个 Skill 可选）", "留空自动识别 SKILL.md 中的名称")}
          <Field label="同名 Skill 处理"><select className={selectClass} value={conflict} onChange={e => setConflict(e.target.value)}>
            <option value="replace">替换并记录新版本（相同内容不重复建版本）</option>
            <option value="skip">跳过已有 Skill</option>
            <option value="error">存在同名 Skill 时停止导入</option>
          </select></Field>
        </>}
      </fieldset>
      {error && <p role="alert" className="rounded-md bg-red-500/10 p-3 text-sm text-red-600">{error}</p>}
      {result && <div role="status" className="space-y-1 rounded-md border border-border p-3 text-xs">
        {result.imported?.map(item => <p key={item.name}>{item.name} · v{item.version} · {{ created: "新增", updated: "更新", unchanged: "内容未变" }[item.status] || item.status}</p>)}
        {result.skipped?.map(item => <p key={item.name}>{item.name} · 已跳过</p>)}
        {result.errors?.map(item => <p key={item.name} className="text-red-600">{item.name} · {item.error}</p>)}
        {!!result.exported?.length && <p>已导出 {result.exported.length} 个 Skill：{result.exported.join("、")}</p>}
        {result.url && <p className="break-all">仓库：{result.url}</p>}
        {result.branch && <p>分支：{result.branch}</p>}
        {result.commit && <p className="break-all">Commit：{result.commit}</p>}
        {result.version && <p>市场版本：{result.version}</p>}
      </div>}
      <DialogFooter className="!bg-transparent !px-0 !py-0">
        <Button variant="outline" disabled={busy} onClick={onClose}>关闭</Button>
        <Button disabled={busy || loading || (!importing && !names.length) || (importing && channel === "zip" && !options.zip_b64)}
          onClick={() => void submit()}>{busy ? "正在处理…" : importing ? "导入 Skill" : "导出 Skill"}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>;
}
