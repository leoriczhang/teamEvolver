// Shared formatting helpers, mirroring the original console.html logic.

export function fmtTime(t?: string | null): string {
  if (!t) return "—";
  const d = new Date(t);
  return isNaN(d.getTime())
    ? String(t)
    : d.toLocaleString("zh-CN", { hour12: false });
}
