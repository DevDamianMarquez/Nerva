import { cn } from "@/lib/utils";

/** The Nerva mark: an "N" drawn as a single connected path, like a signal line. */
export function LogoMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={cn("size-6 shrink-0", className)}
    >
      <rect width="24" height="24" rx="6" className="fill-foreground" />
      <path
        d="M7.5 16.5V7.5L16.5 16.5V7.5"
        className="stroke-background"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function Logo({ className }: { className?: string }) {
  return (
    <span className={cn("flex items-center gap-2.5", className)}>
      <LogoMark />
      <span className="text-[0.95rem] font-semibold tracking-tight">Nerva</span>
    </span>
  );
}
