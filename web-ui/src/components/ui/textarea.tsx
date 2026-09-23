import * as React from "react"

import { cn } from "@/lib/utils"

function Textarea({ className, ...props }: React.ComponentProps<"textarea">) {
  return (
    <textarea
      data-slot="textarea"
      className={cn(
        "flex field-sizing-content min-h-20 w-full rounded-[10px] border border-input bg-white px-3 py-2.5 text-base text-foreground shadow-[0_1px_2px_rgba(15,23,42,0.025)] transition-colors outline-none placeholder:text-[#c0c6d4] hover:border-border-strong focus-visible:border-foreground focus-visible:ring-2 focus-visible:ring-foreground/8 disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-60 aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/15 md:text-[13px]",
        className
      )}
      {...props}
    />
  )
}

export { Textarea }
