import { Button } from "@/components/ui/button";

/** Shown wherever a workspace data fetch fails, with a way to try it again. */
export function LoadError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-center">
      <p className="text-sm text-muted-foreground">
        Couldn&rsquo;t load this. Check your connection and try again.
      </p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        Try again
      </Button>
    </div>
  );
}
