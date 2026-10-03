import { cn } from "@/lib/utils"

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("animate-pulse bg-muted [border-radius:var(--edge-control)]", className)}
      {...props}
    />
  )
}

export { Skeleton }
