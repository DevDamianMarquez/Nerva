"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import type { Action, ActionStatus } from "@/lib/types";

const STATUS_LABEL: Record<ActionStatus, string> = {
  proposed: "Proposed",
  approved: "Approved",
  rejected: "Rejected",
  executing: "Executing",
  completed: "Completed",
  failed: "Failed",
};

const STATUS_BADGE_VARIANT: Record<ActionStatus, "outline" | "secondary" | "destructive"> = {
  proposed: "outline",
  approved: "secondary",
  rejected: "destructive",
  executing: "outline",
  completed: "secondary",
  failed: "destructive",
};

function humanizeType(type: string): string {
  const spaced = type.replaceAll("_", " ").trim();
  if (!spaced) return "Action";
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

interface SendEmailDetails {
  recipients: string[];
  subject: string;
  body: string;
}

/** `Action.payload` is untrusted *display* data as far as this component is
 * concerned (see the module docstring): the backend already validated it
 * before persisting, but this parser makes no assumption about shape and
 * returns `null` for anything unexpected rather than rendering a possibly
 * malformed field or crashing the page. */
function parseSendEmailPayload(payload: Record<string, unknown>): SendEmailDetails | null {
  const { recipients, subject, body } = payload;
  if (!Array.isArray(recipients) || recipients.length === 0) return null;
  if (!recipients.every((recipient): recipient is string => typeof recipient === "string")) {
    return null;
  }
  if (typeof subject !== "string" || typeof body !== "string") return null;
  return { recipients, subject, body };
}

type Decision = "approve" | "reject";

interface SuggestedActionProps {
  action: Action;
  onApprove: (action: Action) => Promise<void>;
  onReject: (action: Action) => Promise<void>;
}

/** One proposed (or already-decided) Action shown for human review in a
 * conversation's detail pane.
 *
 * `payload` is treated as untrusted display data: rendered as plain text only
 * (never `dangerouslySetInnerHTML`, never interpreted as HTML/links), and a
 * shape this component does not recognize degrades to a safe fallback rather
 * than crashing. Recipient/subject/body are review-only — nothing here lets a
 * user edit them, and approving/rejecting only ever sends the action's `id`,
 * never its payload, to the backend (M8.2 adds no execute call and no
 * reasoning trigger; see `onApprove`/`onReject`'s callers). The backend
 * response is the only source of truth for the resulting status — this
 * component never guesses a transition locally. */
export function SuggestedAction({ action, onApprove, onReject }: SuggestedActionProps) {
  const [pending, setPending] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const typeLabel = humanizeType(action.type);
  const details = action.type === "send_email" ? parseSendEmailPayload(action.payload) : null;

  const decide = async (decision: Decision, run: (action: Action) => Promise<void>) => {
    if (pending) return; // a second click while one decision is in flight is a no-op
    setPending(decision);
    setError(null);
    try {
      await run(action);
      // On success the parent updates this action from the backend's own
      // response; there is nothing further to set here.
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn’t save that decision. Try again.");
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-dashed p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-tight">Suggested action</h3>
        <Badge variant={STATUS_BADGE_VARIANT[action.status]}>
          {STATUS_LABEL[action.status]}
        </Badge>
      </div>

      <p className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {typeLabel}
      </p>

      {action.type === "send_email" ? (
        details ? (
          <div className="space-y-2.5 text-sm">
            <div className="min-w-0">
              <p className="text-xs font-medium text-muted-foreground">To</p>
              <p className="break-words">{details.recipients.join(", ")}</p>
            </div>
            <div className="min-w-0">
              <p className="text-xs font-medium text-muted-foreground">Subject</p>
              <p className="break-words">{details.subject}</p>
            </div>
            <div className="min-w-0">
              <p className="text-xs font-medium text-muted-foreground">Body</p>
              {/* Plain text only: React escapes this like any other string —
                  nothing here is ever rendered as HTML. */}
              <p className="whitespace-pre-wrap break-words">{details.body}</p>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            This proposal’s details couldn’t be displayed.
          </p>
        )
      ) : (
        <p className="text-sm break-words">{action.summary}</p>
      )}

      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}

      {action.status === "proposed" ? (
        <div className="flex gap-2 pt-0.5">
          <Button
            variant="outline"
            size="sm"
            disabled={pending !== null}
            aria-label={`Reject: ${action.summary}`}
            onClick={() => void decide("reject", onReject)}
          >
            {pending === "reject" ? "Rejecting…" : "Reject"}
          </Button>
          <Button
            size="sm"
            disabled={pending !== null}
            aria-label={`Approve: ${action.summary}`}
            onClick={() => void decide("approve", onApprove)}
          >
            {pending === "approve" ? "Approving…" : "Approve"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
