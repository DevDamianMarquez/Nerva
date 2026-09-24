import type { ActivityEvent } from "@/lib/types";

export function ActivityList({ items }: { items: readonly ActivityEvent[] }) {
  return (
    <ul className="divide-y">
      {items.map((item) => (
        <li key={item.id} className="space-y-0.5 py-3">
          <p>{item.summary}</p>
          <p className="text-xs text-muted-foreground">
            {item.actor_type === "nerva" ? "Nerva" : "A teammate"} ·{" "}
            {new Date(item.created_at).toLocaleString()}
          </p>
        </li>
      ))}
    </ul>
  );
}
