import * as React from "react"

import { cn } from "@/lib/utils"

function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-[34px] w-full min-w-0 rounded-[10px] border border-input bg-white px-3 py-1 text-base text-foreground shadow-[0_1px_2px_rgba(37,32,24,0.025)] transition-colors outline-none hover:border-border-strong focus-visible:border-foreground focus-visible:ring-2 focus-visible:ring-foreground/8 file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-[#c0c6d4] disabled:pointer-events-none disabled:cursor-not-allowed disabled:bg-muted disabled:opacity-60 aria-invalid:border-destructive aria-invalid:ring-2 aria-invalid:ring-destructive/15 md:text-[13px]",
        className
      )}
      {...props}
    />
  )
}

export { Input }
