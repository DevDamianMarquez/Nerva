import type { ReactNode } from "react";

interface PageHeaderProps {
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-4">
      <div className="min-w-0 space-y-1">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description ? (
          <p className="max-w-prose text-sm text-muted-foreground">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** Standard page body: consistent width and padding across the app. */
export function PageContainer({
  children,
  width = "default",
}: {
  children: ReactNode;
  width?: "default" | "narrow" | "wide";
}) {
  const max = { narrow: "max-w-3xl", default: "max-w-5xl", wide: "max-w-6xl" }[width];
  return (
    <div className={`mx-auto w-full space-y-8 px-4 py-6 sm:px-8 sm:py-10 ${max}`}>{children}</div>
  );
}
