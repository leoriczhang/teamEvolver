import { useCallback, useEffect, useRef, useState } from "react";
import { Dot, Panel, Pill } from "@/components/common";
import { Button } from "@/components/ui/button";
import {
  api,
  type AgentIntegrationsResp,
  type EvolveModelSettings,
  type SharingConfig,
} from "@/api/client";
import { hasOpenVikingConfiguration } from "@/lib/sharing";
import { cn } from "@/lib/utils";

type SetupStep = {
  key: string;
  title: string;
  desc: string;
  done: boolean;
  /** The probe failed: status is unknown rather than "not configured". */
  unknown: boolean;
  target: string;
  actionLabel: string;
};

/**
 * 首跑配置引导：首次部署时把 README 里散落的配置步骤（模型 → OpenViking →
 * Agent 接入 → 会话数据）收敛成运行总览顶部的一张清单，全部完成后自动消失。
 */
export default function SetupChecklist({
  active,
  onNavigate,
}: {
  active: boolean;
  onNavigate: (view: string) => void;
}) {
  const [model, setModel] = useState<EvolveModelSettings | null>(null);
  const [sharing, setSharing] = useState<SharingConfig | null>(null);
  const [agentCount, setAgentCount] = useState<number | null>(null);
  const [sessionCount, setSessionCount] = useState<number | null>(null);
  const [loaded, setLoaded] = useState(false);
  const loadedRef = useRef(false);

  const refresh = useCallback(async () => {
    const [m, s, a, sess] = await Promise.allSettled([
      api<EvolveModelSettings>("/api/model-settings"),
      api<SharingConfig>("/api/sharing-config"),
      api<AgentIntegrationsResp>("/api/agent-integrations"),
      api<{ sessions: unknown[]; total?: number }>("/sessions?limit=1&offset=0"),
    ]);
    if (m.status === "fulfilled") setModel(m.value);
    if (s.status === "fulfilled") setSharing(s.value);
    if (a.status === "fulfilled") setAgentCount((a.value.agents || []).length);
    if (sess.status === "fulfilled") {
      setSessionCount(Number(sess.value.total ?? (sess.value.sessions || []).length));
    }
    setLoaded(true);
  }, []);

  useEffect(() => {
    if (!active) {
      loadedRef.current = false;
      return;
    }
    if (loadedRef.current) return;
    loadedRef.current = true;
    void refresh();
  }, [active, refresh]);

  const steps: SetupStep[] = [
    {
      key: "model",
      title: "配置进化模型",
      desc: "OpenAI-compatible Base URL、Model 与 API Key；进化与技能挖掘共用该模型。",
      done: Boolean(model?.model && model?.base_url && model?.api_key_present),
      unknown: model === null,
      target: "model",
      actionLabel: "去配置",
    },
    {
      key: "openviking",
      title: "连接 OpenViking",
      desc: "个人记忆与团队资源的上下文存储；未连接时“个人与团队资产”不可用。",
      done: hasOpenVikingConfiguration(sharing),
      unknown: sharing === null,
      target: "health",
      actionLabel: "去配置",
    },
    {
      key: "agent",
      title: "注册 Agent 接入",
      desc: "注册 Runtime 并启用 Session / Context / Replay / Skill Sync 能力。",
      done: (agentCount ?? 0) > 0,
      unknown: agentCount === null,
      target: "health",
      actionLabel: "去注册",
    },
    {
      key: "sessions",
      title: "让会话进入进化队列",
      desc: "接入 Langfuse 数据源，或由已注册的 Agent 使用租户凭证与用户 ID 上报真实会话。",
      done: (sessionCount ?? 0) > 0,
      unknown: sessionCount === null,
      target: "datasource",
      actionLabel: "去接入",
    },
  ];

  const doneCount = steps.filter((step) => step.done).length;
  const anyKnown = steps.some((step) => !step.unknown);
  // Nothing to guide once setup is complete, and no false alarms when every
  // probe failed (e.g. the service is still starting).
  if (!loaded || !anyKnown || doneCount === steps.length) return null;

  return (
    <div className="mb-[18px]">
      <Panel title="配置引导" count={`完成 ${doneCount}/${steps.length}`}>
        <div className="space-y-2 p-3.5">
          {steps.map((step) => (
            <div
              key={step.key}
              className={cn(
                "flex flex-wrap items-center gap-3 rounded-lg border px-3.5 py-2.5",
                step.done ? "border-border bg-surface-subtle" : "border-amber-300 bg-amber-50/70"
              )}
            >
              <Dot state={step.done ? "on" : "off"} />
              <div className="min-w-[220px] flex-1">
                <div className="text-[13px] font-bold">{step.title}</div>
                <div className="mt-0.5 text-[11.5px] leading-relaxed text-muted-foreground">
                  {step.desc}
                </div>
              </div>
              {step.done ? (
                <Pill tone="green">已完成</Pill>
              ) : (
                <div className="flex items-center gap-2">
                  {step.unknown && <span className="text-[11px] text-muted-foreground">无法确认</span>}
                  <Button variant="outline" size="sm" onClick={() => onNavigate(step.target)}>
                    {step.actionLabel}
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
