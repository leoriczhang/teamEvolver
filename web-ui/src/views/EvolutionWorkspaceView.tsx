import { useState } from "react";
import { Brain, Sparkles } from "lucide-react";

import type { UserProfile } from "@/api/client";
import { cn } from "@/lib/utils";
import TeamMemoryAggregationView from "@/views/TeamMemoryAggregationView";
import PromptStudioView from "@/views/PromptStudioView";

type EvolutionTab = "skills" | "memory";

const TABS = [
  { key: "skills" as const, label: "Skills 自进化", icon: Sparkles },
  { key: "memory" as const, label: "团队 Memory 自进化", icon: Brain },
];

export default function EvolutionWorkspaceView({
  active,
  user,
}: {
  active: boolean;
  user?: UserProfile | null;
}) {
  const [tab, setTab] = useState<EvolutionTab>("skills");

  return (
    <div>
      <div className="section-tabs pt-2.5">
        <div className="flex flex-wrap gap-1.5" role="tablist" aria-label="进化链路">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={cn(
                "flex items-center gap-1.5 border-b-2 px-3 py-2 text-[12px] font-[700]",
                tab === key
                  ? "border-accent text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground",
              )}
            >
              <Icon className="size-4" />
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className={cn(tab !== "skills" && "hidden")}>
        <PromptStudioView active={active && tab === "skills"} user={user} />
      </div>
      <div className={cn(tab !== "memory" && "hidden")}>
        <TeamMemoryAggregationView active={active && tab === "memory"} user={user} />
      </div>
    </div>
  );
}
