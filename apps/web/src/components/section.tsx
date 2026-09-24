import type { ReactNode } from "react";

interface SectionProps {
  id: string;
  title: string;
  /** Extra content beside the title, e.g. a count or a "Sample data" badge. */
  meta?: ReactNode;
  children: ReactNode;
}

/** A titled region of a page. The heading labels the region for assistive technology. */
export function Section({ id, title, meta, children }: SectionProps) {
  const headingId = `${id}-heading`;
  return (
    <section aria-labelledby={headingId}>
      <div className="mb-1 flex items-center gap-2.5 border-b pb-2.5">
        <h2 id={headingId} className="text-sm font-semibold tracking-tight">
          {title}
        </h2>
        {meta}
      </div>
      {children}
    </section>
  );
}
