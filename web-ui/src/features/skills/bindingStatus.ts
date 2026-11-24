export function bindingStatus(skill: {
  version?: number | string | null;
  bound_version?: string | null;
  bound_versions?: Record<string, string>;
}): { label: string; tone: "green" | "amber"; versions: string } {
  const versions = Object.values(skill.bound_versions || {});
  if (!versions.length && skill.bound_version != null) versions.push(skill.bound_version);
  if (!versions.length) return { label: "已发布，未挂载", tone: "amber", versions: "—" };
  const current = versions.filter(version => String(version) === String(skill.version)).length;
  return {
    label: current === versions.length ? "已挂载" : current ? "部分成员未更新" : "已挂载旧版本",
    tone: current === versions.length ? "green" : "amber",
    versions: [...new Set(versions)].map(version => `v${version}`).join("、"),
  };
}
