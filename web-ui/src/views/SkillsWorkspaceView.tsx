import { useState } from "react";
import { Beaker, FolderTree, Library } from "lucide-react";
import type { UserProfile } from "@/api/client";
import { cn } from "@/lib/utils";
import OpenVikingWorkspaceShell from "@/views/OpenVikingWorkspaceShell";
import SkillLabView from "@/views/SkillLabView";
import SkillsView from "@/views/SkillsView";

type SkillsTab = "assets" | "openviking" | "lab";

const TABS = [
  { key: "assets" as const, label: "技能资产", icon: Library },
  { key: "openviking" as const, label: "OpenViking 可视化", icon: FolderTree },
  { key: "lab" as const, label: "Skill Lab", icon: Beaker },
];

export default function SkillsWorkspaceView({ active, user, initialTab = "assets" }: { active: boolean; user?: UserProfile | null; initialTab?: SkillsTab }) {
  const [tab, setTab] = useState<SkillsTab>(initialTab);
  return (
    <div>
      <div className="section-tabs pt-2.5">
        <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="Skills Workspace">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button key={key} type="button" role="tab" aria-selected={tab === key} onClick={() => setTab(key)}
              className={cn("flex items-center gap-1.5 border-b-2 px-3 py-2 text-[12px] font-[700]", tab === key ? "border-accent text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}>
              <Icon className="size-4" />{label}
            </button>
          ))}
        </div>
      </div>
      <div className={cn(tab !== "assets" && "hidden")}><SkillsView active={active && tab === "assets"} user={user} /></div>
      <div className={cn(tab !== "openviking" && "hidden")}><OpenVikingWorkspaceShell active={active && tab === "openviking"} mode="skills" user={user} /></div>
      <div className={cn(tab !== "lab" && "hidden")}><SkillLabView active={active && tab === "lab"} user={user} /></div>
    </div>
  );
}
