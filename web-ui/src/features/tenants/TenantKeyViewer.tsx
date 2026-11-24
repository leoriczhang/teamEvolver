import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getTenantToken } from "@/features/tenants/api/client";
import { Copy, KeyRound, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export default function TenantKeyViewer({ tenantId }: { tenantId: string }) {
  const [open, setOpen] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const request = useRef(0);

  useEffect(() => {
    setOpen(false); setToken(""); setBusy(false); setError(""); setCopied(false);
    return () => { request.current += 1; };
  }, [tenantId]);

  async function reveal() {
    const current = ++request.current;
    setOpen(true); setBusy(true); setError(""); setCopied(false);
    try {
      const result = await getTenantToken(tenantId);
      if (current !== request.current) return;
      setToken(result.agent_token || "");
      if (!result.available) setError("当前租户 Key 尚未留存，可在“成员与接入”保存原 Key。");
    } catch (e) {
      if (current === request.current) setError(e instanceof Error ? e.message : "读取 Key 失败");
    } finally {
      if (current === request.current) setBusy(false);
    }
  }

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(token);
        setCopied(true);
        return;
      }
    } catch { /* HTTP deployments can use the selected input below. */ }
    input.current?.focus();
    input.current?.select();
    try {
      if (document.execCommand?.("copy")) { setCopied(true); return; }
    } catch { /* Keep the input selected for manual copying. */ }
    setError("复制失败，请选中 Key 手动复制。");
  }

  return <div className="mt-3 space-y-3">
    {!open ? <Button size="sm" variant="outline" disabled={!tenantId} onClick={reveal}>
      <KeyRound className="size-4" />查看租户 Key
    </Button> : <div className="space-y-3 rounded-lg border bg-background p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">当前租户 Key</span>
        <Button size="sm" variant="ghost" onClick={() => {
          request.current += 1; setOpen(false); setToken(""); setBusy(false);
        }}><X className="size-4" />收起</Button>
      </div>
      <p className="text-xs text-muted-foreground">此 Key 已保存，可随时查看、复制并复用。</p>
      {busy && <p role="status" className="text-xs text-muted-foreground">正在读取 Key…</p>}
      {token && <div className="flex gap-2">
        <Input ref={input} aria-label="当前租户 Key" type="text" readOnly value={token}
          className="font-mono text-xs" onFocus={e => e.target.select()} />
        <Button size="sm" variant="outline" onClick={copy}><Copy className="size-4" />{copied ? "已复制" : "复制"}</Button>
      </div>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
    </div>}
  </div>;
}
