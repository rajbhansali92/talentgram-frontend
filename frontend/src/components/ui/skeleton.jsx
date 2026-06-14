import { cn } from "@/lib/utils"

function Skeleton({
  className,
  ...props
}) {
  return (
    <div
      className={cn("animate-tg-shimmer rounded-[var(--tg-radius-md)]", className)}
      {...props} />
  );
}

export { Skeleton }
