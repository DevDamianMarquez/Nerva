"use client";

import Link from "next/link";
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

/** `STATUS_LABEL`, specialized for `send_email`'s two terminal states — the
 * product-facing "Sent" / "Failed to send" the task calls for, rather than
 * the generic "Completed" / "Failed" every other (future) action type still
 * falls back to. */
function statusLabel(action: Action): string {
  if (action.type === "send_email") {
    if (action.status === "completed") return "Sent";
    if (action.status === "failed") return "Failed to send";
  }
  return STATUS_LABEL[action.status];
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

type Decision = "approve" | "reject" | "execute";

interface SuggestedActionProps {
  action: Action;
  onApprove: (action: Action) => Promise<void>;
  onReject: (action: Action) => Promise<void>;
  /** Whether the current user is authorized to send this (owner-only on the
   * backend — see `routes.actions.execute_action`). UX-only: hiding the
   * button never substitutes for the backend's own 403, which remains the
   * actual authorization boundary. */
  canExecute: boolean;
  onExecute: (action: Action) => Promise<void>;
}

function decisionErrorMessage(decision: Decision, err: unknown): string {
  if (decision === "execute" && err instanceof ApiError) {
    // Product-facing text distinct from the backend's own (more technical)
    // message — see `routes.actions.execute_action`'s 403/409 mappings.
    if (err.status === 403) return "Only the workspace owner can send this.";
    if (err.status === 409) return "This proposal is no longer ready to send.";
  }
  if (err instanceof ApiError) return err.message;
  return decision === "execute"
    ? "Couldn’t send this. Try again."
    : "Couldn’t save that decision. Try again.";
}

/** One proposed (or already-decided) Action shown for human review in a
 * conversation's detail pane.
 *
 * `payload` is treated as untrusted display data: rendered as plain text only
 * (never `dangerouslySetInnerHTML`, never interpreted as HTML/links), and a
 * shape this component does not recognize degrades to a safe fallback rather
 * than crashing. Recipient/subject/body are review-only — nothing here lets a
 * user edit them, and approving/rejecting/executing only ever send the
 * action's `id`, never its payload, to the backend; executing never calls
 * `/reason` or `/approve`/`/reject` (see each prop's own caller in
 * `InboxView`). The backend response is the only source of truth for the
 * resulting status — this component never guesses a transition locally, and
 * never shows a terminal state before the backend actually returns one. */
export function SuggestedAction({
  action,
  onApprove,
  onReject,
  canExecute,
  onExecute,
}: SuggestedActionProps) {
  const [pending, setPending] = useState<Decision | null>(null);
  const [error, setError] = useState<string | null>(null);
  const typeLabel = humanizeType(action.type);
  const details = action.type === "send_email" ? parseSendEmailPayload(action.payload) : null;
  // A malformed payload is an additional frontend safety gate, not a
  // substitute for backend authorization: if the recipient/subject/body
  // cannot be safely parsed and shown for review, Send is never offered,
  // even when the backend status is already `approved`.
  const canShowSend =
    action.type === "send_email" && action.status === "approved" && canExecute && details !== null;
  const resultError =
    action.result && typeof action.result.error === "string" ? action.result.error : null;

  const decide = async (decision: Decision, run: (action: Action) => Promise<void>) => {
    if (pending) return; // a second click while one decision is in flight is a no-op
    setPending(decision);
    setError(null);
    try {
      await run(action);
      // On success the parent updates this action from the backend's own
      // response; there is nothing further to set here.
    } catch (err) {
      setError(decisionErrorMessage(decision, err));
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-dashed p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-semibold tracking-tight">Suggested action</h3>
        <Badge variant={STATUS_BADGE_VARIANT[action.status]}>{statusLabel(action)}</Badge>
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

      {action.type === "send_email" && action.status === "failed" ? (
        <div className="space-y-1.5 border-t pt-3 text-sm">
          {resultError ? <p className="text-destructive">{resultError}</p> : null}
          <Link href="/settings" className="font-medium underline underline-offset-2">
            Go to Settings
          </Link>
        </div>
      ) : null}

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
      ) : canShowSend ? (
        <div className="flex gap-2 pt-0.5">
          <Button
            size="sm"
            disabled={pending !== null}
            aria-label={`Send email: ${action.summary}`}
            onClick={() => void decide("execute", onExecute)}
          >
            {pending === "execute" ? "Sending…" : "Send email"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
