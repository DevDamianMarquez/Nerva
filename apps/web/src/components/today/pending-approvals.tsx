"use client";

import { Check, X } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { Action } from "@/lib/types";

function actionTypeLabel(type: string): string {
  const spaced = type.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

interface PendingApprovalsProps {
  actions: readonly Action[];
  onApprove: (id: string) => Promise<void>;
  onReject: (id: string) => Promise<void>;
}

/** The approval queue. Deciding calls the API; the action leaves this list once it is
 * no longer `proposed` (the parent reloads the `proposed`-filtered list after each
 * decision), so there is no local "approved" / "rejected" state to track here. */
export function PendingApprovals({ actions, onApprove, onReject }: PendingApprovalsProps) {
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const decide = async (id: string, action: (id: string) => Promise<void>) => {
    setPendingId(id);
    setErrors((current) => ({ ...current, [id]: "" }));
    try {
      await action(id);
    } catch {
      setErrors((current) => ({ ...current, [id]: "Couldn’t save that decision. Try again." }));
    } finally {
      setPendingId(null);
    }
  };

  return (
    <div>
      <p className="sr-only" role="status" aria-live="polite">
        {actions.length === 0
          ? "No approvals waiting."
          : `${actions.length} ${actions.length === 1 ? "approval" : "approvals"} waiting.`}
      </p>

      {actions.length === 0 ? (
        <p className="py-8 text-center text-muted-foreground">No actions need your approval.</p>
      ) : (
        <ul className="divide-y">
          {actions.map((action) => (
            <li
              key={action.id}
              className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 py-4"
            >
              <div className="min-w-0 space-y-1">
                <p className="font-medium">{action.summary}</p>
                <p className="text-xs text-muted-foreground">
                  {actionTypeLabel(action.type)} · Proposed by{" "}
                  {action.created_by_type === "nerva" ? "Nerva" : "a teammate"}
                </p>
                {errors[action.id] ? (
                  <p className="text-xs text-destructive">{errors[action.id]}</p>
                ) : null}
              </div>
              <div className="flex shrink-0 gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`Reject: ${action.summary}`}
                  disabled={pendingId === action.id}
                  onClick={() => void decide(action.id, onReject)}
                >
                  <X data-icon="inline-start" aria-hidden="true" />
                  Reject
                </Button>
                <Button
                  size="sm"
                  aria-label={`Approve: ${action.summary}`}
                  disabled={pendingId === action.id}
                  onClick={() => void decide(action.id, onApprove)}
                >
                  <Check data-icon="inline-start" aria-hidden="true" />
                  Approve
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
