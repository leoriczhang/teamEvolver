import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { toastOk, toastErr } from "@/lib/toast";
import { api, type SkillDetail } from "@/api/client";
import DropZone from "./DropZone";
import { fileToB64 } from "@/lib/file";
import { ListViewport, PaginationControls, usePagedItems } from "@/components/common";

type Tab = "form" | "raw" | "files";

export default function SkillEditModal({
  name,
  open,
  onClose,
  onSaved,
}: {
  name: string | null | undefined; // null=create, string=edit
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = typeof name === "string";
  const [tab, setTab] = useState<Tab>("form");
  const [fName, setFName] = useState("");
  const [fCategory, setFCategory] = useState("general");
  const [fDesc, setFDesc] = useState("");
  const [fBody, setFBody] = useState("");
  const [fRaw, setFRaw] = useState("");
  const [files, setFiles] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const currentName = useRef<string | null>(null); // null => create mode

  // (re)load when opened
  useEffect(() => {
    if (!open) return;
    setTab("form");
    if (!isEdit) {
      currentName.current = null;
      setFName("");
      setFCategory("general");
      setFDesc("");
      setFBody("");
      setFRaw("");
      setFiles([]);
      return;
    }
    // edit
    api<SkillDetail>(`/api/skills/${encodeURIComponent(name as string)}`)
      .then((s) => {
        currentName.current = name as string;
        setFName(s.name || "");
        setFCategory(s.category || "general");
        setFDesc(s.description || "");
        setFBody(s.body || "");
        setFRaw(s.skill_md || "");
        setFiles(s.files || []);
      })
      .catch((e) => toastErr("读取失败", e.message));
  }, [open, name, isEdit]);

  async function reloadFiles() {
    if (!currentName.current) return;
    try {
      const s = await api<SkillDetail>(
        `/api/skills/${encodeURIComponent(currentName.current)}`
      );
      setFiles(s.files || []);
    } catch {}
  }

  async function saveSkill() {
    const finalName = currentName.current || fName.trim();
    if (!finalName) {
      toastErr("请填写名称");
      return;
    }
    setSaving(true);
    try {
      const r = await api<{ created?: boolean; version?: number }>("/api/skills", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: finalName,
          description: fDesc,
          category: fCategory || "general",
          body: fBody,
          skill_md: fRaw.trim(),
        }),
      });
      toastOk(r.created ? "已创建技能" : "已保存技能", `版本 ${r.version || 0}`);
      onClose();
      onSaved();
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  async function delFile(rel: string) {
    if (!currentName.current) return;
    if (!window.confirm(`确认删除文件「${rel}」？`)) return;
    try {
      const encoded = rel.split("/").map(encodeURIComponent).join("/");
      await api(
        `/api/skills/${encodeURIComponent(currentName.current)}/files/${encoded}`,
        { method: "DELETE" }
      );
      toastOk("已删除文件");
      reloadFiles();
    } catch (e: any) {
      toastErr("删除失败", e.message);
    }
  }

  async function uploadFiles(list: FileList) {
    if (!currentName.current) {
      toastErr("请先保存技能再上传附件");
      return;
    }
    try {
      const payload = [];
      for (const f of Array.from(list)) {
        payload.push({ path: f.name, content_b64: await fileToB64(f) });
      }
      const r = await api<{ written: string[] }>(
        `/api/skills/${encodeURIComponent(currentName.current)}/files`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ files: payload }),
        }
      );
      toastOk(`已上传 ${r.written.length} 个文件`);
      reloadFiles();
    } catch (e: any) {
      toastErr("上传失败", e.message);
    }
  }

  const extras = files.filter((f) => f !== "SKILL.md");
  const extrasPager = usePagedItems(extras);

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="flex max-h-[88vh] w-full !max-w-[860px] flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑技能 · " + name : "新建技能"}</DialogTitle>
        </DialogHeader>

        {/* tabs */}
        <div className="flex flex-wrap gap-1.5">
          <TabBtn active={tab === "form"} onClick={() => setTab("form")}>
            表单编辑
          </TabBtn>
          <TabBtn active={tab === "raw"} onClick={() => setTab("raw")}>
            原文 SKILL.md
          </TabBtn>
          {isEdit && (
            <TabBtn active={tab === "files"} onClick={() => setTab("files")}>
              附件文件
            </TabBtn>
          )}
        </div>

        <div className="-mr-1 overflow-auto pr-1">
          {tab === "form" && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3.5">
                <Fld label="名称 *" hint="仅字母数字及 . - _；已存在则视为更新">
                  <Input
                    value={fName}
                    disabled={isEdit}
                    placeholder="my-skill"
                    onChange={(e) => setFName(e.target.value)}
                  />
                </Fld>
                <Fld label="分类">
                  <Input
                    value={fCategory}
                    placeholder="general"
                    onChange={(e) => setFCategory(e.target.value)}
                  />
                </Fld>
              </div>
              <Fld label="描述 *">
                <Textarea
                  rows={2}
                  value={fDesc}
                  placeholder="Use when …. NOT for: …"
                  onChange={(e) => setFDesc(e.target.value)}
                />
              </Fld>
              <Fld label="正文 (Markdown)">
                <Textarea
                  rows={14}
                  className="mono"
                  value={fBody}
                  placeholder={"# 标题\n\n技能说明…"}
                  onChange={(e) => setFBody(e.target.value)}
                />
              </Fld>
            </div>
          )}

          {tab === "raw" && (
            <Fld
              label="SKILL.md 原文（含 YAML frontmatter）"
              hint="填写后将按原文写入，覆盖表单字段。"
            >
              <Textarea
                rows={22}
                className="mono"
                value={fRaw}
                placeholder={"---\nname: ...\ndescription: ...\n---\n\n# ..."}
                onChange={(e) => setFRaw(e.target.value)}
              />
            </Fld>
          )}

          {tab === "files" && isEdit && (
            <div className="space-y-3">
              {extras.length ? (
                <>
                  <ListViewport maxHeight="320px">
                    <ul className="m-0 list-none p-0">
                      {extrasPager.items.map((f) => (
                        <li
                          key={f}
                          className="mb-1.5 flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-[12.5px]"
                        >
                          <span className="mono break-all">{f}</span>
                          <Button variant="destructive" size="sm" onClick={() => delFile(f)}>
                            删除
                          </Button>
                        </li>
                      ))}
                    </ul>
                  </ListViewport>
                  <PaginationControls {...extrasPager} onPageChange={extrasPager.setPage} />
                </>
              ) : (
                <p className="text-xs text-muted-foreground">暂无附件文件（仅 SKILL.md）。</p>
              )}
              <DropZone
                multiple
                onFiles={uploadFiles}
                label="点击或拖拽文件到此处上传（支持多选，写入技能目录，不含 SKILL.md）"
              />
            </div>
          )}
        </div>

        <DialogFooter className="!bg-transparent !px-0 !py-0">
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button disabled={saving} onClick={saveSkill}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "rounded-md border px-3.5 py-1 text-xs font-semibold transition-colors",
        active
          ? "border-sidebar-primary bg-sidebar-primary text-white"
          : "border-border bg-transparent hover:bg-muted"
      )}
    >
      {children}
    </button>
  );
}

function Fld({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-semibold text-muted-foreground">{label}</label>
      {children}
      {hint && <div className="mt-1.5 text-[11px] text-muted-soft">{hint}</div>}
    </div>
  );
}
