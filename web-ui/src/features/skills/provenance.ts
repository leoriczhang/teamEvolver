import type { Candidate, SkillVersionProvenance } from "./api/client";

export function candidateBaseline(candidate?: Pick<Candidate, "baseline" | "base_version" | "proposed_action"> | null): string {
  const version = candidate?.baseline?.version ?? candidate?.base_version;
  if (version != null && version > 0) return `基于 Skill v${version}`;
  if (candidate?.baseline?.kind === "new"
    || (version === 0 && ["create", "create_skill"].includes(candidate?.proposed_action || ""))) {
    return "新建技能 · 无基线版本";
  }
  return "基线版本未记录";
}

export function versionOrigin(provenance?: SkillVersionProvenance): string {
  if (provenance?.kind === "te") {
    return provenance.user_modified ? "TE 生成 · 用户修改后发布" : "TE 修改并发布";
  }
  if (provenance?.kind === "manual") {
    if (provenance.action === "rollback") return "用户手动回滚";
    if (provenance.source === "transfer") return "用户手动导入";
    return "用户手动上传 / 修改";
  }
  return "来源未记录";
}
