import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import type { ConversationAttention } from "@/lib/types";

const priorityLabels: Record<string, string> = { medium: "Medium", high: "High", urgent: "Urgent" };

function formatTime(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Conversations Nerva's analysis flagged as needing a reply, or as high/urgent
 * priority — a read-only view of already-computed backend analysis, never a
 * client-side classification of its own. Clicking a row opens it in the Inbox. */
export function NeedsAttention({ items }: { items: readonly ConversationAttention[] }) {
  if (items.length === 0) {
    return (
      <p className="py-8 text-center text-muted-foreground">
        Nothing needs your attention right now.
      </p>
    );
  }

  return (
    <ul className="divide-y">
      {items.map((item) => (
        <li key={item.conversation_id}>
          <Link
            href={`/inbox?conversation=${item.conversation_id}`}
            className="block space-y-1.5 py-3 outline-none hover:bg-accent focus-visible:bg-accent"
          >
            <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
              <p className="font-medium">{item.subject || "(no subject)"}</p>
              <p className="shrink-0 text-xs text-muted-foreground">
                {formatTime(item.last_message_at)}
              </p>
            </div>
            {item.priority && item.priority !== "low" ? (
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant={item.priority === "urgent" ? "destructive" : "outline"}>
                  {priorityLabels[item.priority] ?? item.priority}
                </Badge>
                {item.requires_response ? <Badge variant="outline">Needs response</Badge> : null}
              </div>
            ) : item.requires_response ? (
              <Badge variant="outline">Needs response</Badge>
            ) : null}
            {item.summary ? (
              <p className="truncate text-xs text-muted-foreground">{item.summary}</p>
            ) : null}
          </Link>
        </li>
      ))}
    </ul>
  );
}
