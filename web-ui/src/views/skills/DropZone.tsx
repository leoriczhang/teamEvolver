import { useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";

/**
 * Click-or-drag file drop zone. Mirrors console.html wireDrop():
 * - multiple=true  → passes the FileList to onFiles
 * - multiple=false → passes a single File to onFiles
 */
export default function DropZone({
  onFiles,
  label,
  multiple = false,
  accept,
  disabled = false,
}: {
  onFiles: (files: FileList) => void;
  label: ReactNode;
  multiple?: boolean;
  accept?: string;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  return (
    <>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        aria-label={typeof label === "string" ? label : "选择上传文件"}
        className={cn("drop", over && "over", disabled && "cursor-not-allowed opacity-55")}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!disabled && (e.key === "Enter" || e.key === " ")) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragEnter={(e) => {
          e.preventDefault();
          if (disabled) return;
          setOver(true);
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (disabled) return;
          setOver(true);
        }}
        onDragLeave={(e) => {
          e.preventDefault();
          setOver(false);
        }}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          if (!disabled && e.dataTransfer.files.length) onFiles(e.dataTransfer.files);
        }}
      >
        {label}
      </div>
      <input
        ref={inputRef}
        type="file"
        multiple={multiple}
        accept={accept}
        disabled={disabled}
        className="hidden"
        onChange={(e) => {
          if (e.target.files?.length) onFiles(e.target.files);
          e.target.value = "";
        }}
      />
    </>
  );
}
