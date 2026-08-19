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

type Tab = "form" | "raw";

// Editor for a user's own personal-space skill (OpenViking peers/{uid}/skills).
// Distinct from SkillEditModal, which edits the admin-only team library.
export default function PersonalSkillEditModal({
  userId,
  name,
  open,
  onClose,
  onSaved,
}: {
  userId: string;
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
  const [saving, setSaving] = useState(false);
  const currentName = useRef<string | null>(null);

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
      return;
    }
    api<SkillDetail>(
      `/api/users/${encodeURIComponent(userId)}/skills/${encodeURIComponent(name as string)}?space=personal`,
    )
      .then((s) => {
        currentName.current = name as string;
        setFName(s.name || "");
        setFCategory(s.category || "general");
        setFDesc(s.description || "");
        setFBody(s.body || "");
        setFRaw(s.skill_md || "");
      })
      .catch((e) => toastErr("读取失败", e.message));
  }, [open, name, isEdit, userId]);

  async function saveSkill() {
    const finalName = currentName.current || fName.trim();
    if (!finalName) {
      toastErr("请填写名称");
      return;
    }
    setSaving(true);
    try {
      await api(`/api/users/${encodeURIComponent(userId)}/skills`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          space: "personal",
          name: finalName,
          description: fDesc,
          category: fCategory || "general",
          body: fBody,
          skill_md: fRaw.trim(),
        }),
      });
      toastOk(isEdit ? "已保存个人技能" : "已创建个人技能", finalName);
      onClose();
      onSaved();
    } catch (e: any) {
      toastErr("保存失败", e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="flex max-h-[88vh] w-full !max-w-[860px] flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>{isEdit ? "编辑个人技能 · " + name : "新建个人技能"}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-wrap gap-1.5">
          <TabBtn active={tab === "form"} onClick={() => setTab("form")}>
            表单编辑
          </TabBtn>
          <TabBtn active={tab === "raw"} onClick={() => setTab("raw")}>
            原文 SKILL.md
          </TabBtn>
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
          : "border-border bg-transparent hover:bg-muted",
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
