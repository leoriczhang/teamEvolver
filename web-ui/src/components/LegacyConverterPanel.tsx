import { useRef, useState } from "react";
import { CheckCheck, Play, Upload } from "lucide-react";
import { api } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { toastErr } from "@/lib/toast";

export default function LegacyConverterPanel({ previewPath, sourceType, code, disabled = false, onSourceType, onChange }: {
  previewPath?: string; sourceType: string; code: string; disabled?: boolean;
  onSourceType: (value: string) => void; onChange: (value: string) => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [raw, setRaw] = useState('{"trace":{},"observations":[]}');
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(action: "check" | "test") {
    if (!previewPath) return;
    setBusy(true);
    try {
      const body = action === "test" ? { code, raw: JSON.parse(raw) } : { code };
      const output = await api(`${previewPath}/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      setResult(JSON.stringify(output, null, 2));
    } catch (e: any) {
      toastErr("适配器检查失败", e.message);
    } finally {
      setBusy(false);
    }
  }

  return <section className="border-t border-border pt-4">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h3 className="text-sm font-semibold">会话转换模式</h3>
        <p className="mt-1 text-xs text-muted-foreground">两种模式都从 Langfuse 拉取 Session，仅转换逻辑不同。</p>
      </div>
      <select aria-label="会话转换模式" value={sourceType} disabled={disabled} onChange={e => onSourceType(e.target.value)} className="h-9 max-w-full rounded-md border border-border bg-background px-2 text-xs">
        <option value="langfuse">原生 Langfuse 映射</option>
        <option value="skillopt">兼容模式（导入旧版 converter.py）</option>
      </select>
    </div>
    {sourceType === "skillopt" && <div className="space-y-3">
      <div className="flex flex-wrap gap-2">
        <input ref={fileInput} type="file" accept=".py" className="hidden" onChange={async e => {
          const file = e.target.files?.[0];
          if (!file) return;
          if (file.size > 262144) { toastErr("适配器文件超过 256 KiB"); return; }
          onChange(await file.text()); setResult("");
        }} />
        <Button size="sm" variant="outline" disabled={disabled} onClick={() => fileInput.current?.click()}><Upload className="size-3.5" />导入 .py</Button>
        {previewPath && <>
          <Button size="sm" variant="outline" disabled={disabled || busy || !code.trim()} onClick={() => run("check")}><CheckCheck className="size-3.5" />兼容检查</Button>
          <Button size="sm" disabled={disabled || busy || !code.trim()} onClick={() => run("test")}><Play className="size-3.5" />离线试跑</Button>
        </>}
      </div>
      <label className="block text-xs font-medium">Converter 源码
        <Textarea aria-label="Converter 源码" value={code} disabled={disabled} spellCheck={false} onChange={e => { onChange(e.target.value); setResult(""); }} className="mt-2 h-48 font-mono text-xs" />
      </label>
      {previewPath && <label className="block text-xs font-medium">Raw Trace JSON
        <Textarea aria-label="Raw Trace JSON" value={raw} disabled={disabled} spellCheck={false} onChange={e => setRaw(e.target.value)} className="mt-2 h-28 font-mono text-xs" />
      </label>}
      {result && <pre aria-label="适配结果" className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md border border-border p-3 text-xs">{result}</pre>}
    </div>}
  </section>;
}
