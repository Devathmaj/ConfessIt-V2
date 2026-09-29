import { GripVertical } from "lucide-react"
import { Group, Panel, Separator } from "react-resizable-panels"

import { cn } from "@/lib/utils"

const ResizablePanelGroup = ({
  className,
  orientation = "horizontal",
  ...props
}: React.ComponentProps<typeof Group> & {
  direction?: "horizontal" | "vertical"
}) => {
  // v4 renamed `direction` to `orientation`. Accept the legacy prop so any
  // existing call sites keep working while new code uses `orientation`.
  const { direction, ...rest } = props as Record<string, unknown>
  const resolvedOrientation =
    (rest as { orientation?: "horizontal" | "vertical" }).orientation ??
    (direction as "horizontal" | "vertical" | undefined) ??
    orientation
  return (
    <Group
      orientation={resolvedOrientation}
      className={cn(
        "flex h-full w-full data-[orientation=vertical]:flex-col data-[panel-group-direction=vertical]:flex-col",
        className
      )}
      {...(rest as React.ComponentProps<typeof Group>)}
    />
  )
}

const ResizablePanel = Panel

const ResizableHandle = ({
  withHandle,
  className,
  ...props
}: React.ComponentProps<typeof Separator> & {
  withHandle?: boolean
}) => (
  <Separator
    className={cn(
      "relative flex w-px items-center justify-center bg-border after:absolute after:inset-y-0 after:left-1/2 after:w-1 after:-translate-x-1/2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-offset-1 data-[orientation=vertical]:h-px data-[orientation=vertical]:w-full data-[orientation=vertical]:after:left-0 data-[orientation=vertical]:after:h-1 data-[orientation=vertical]:after:w-full data-[orientation=vertical]:after:-translate-y-1/2 data-[orientation=vertical]:after:translate-x-0 data-[panel-group-direction=vertical]:h-px data-[panel-group-direction=vertical]:w-full [&[data-orientation=vertical]>div]:rotate-90 [&[data-panel-group-direction=vertical]>div]:rotate-90",
      className
    )}
    {...props}
  >
    {withHandle && (
      <div className="z-10 flex h-4 w-3 items-center justify-center rounded-sm border bg-border">
        <GripVertical className="h-2.5 w-2.5" />
      </div>
    )}
  </Separator>
)

export { ResizablePanelGroup, ResizablePanel, ResizableHandle }
export { Group as ResizableGroup, Panel as ResizablePanelPrimitive, Separator as ResizableSeparator }
