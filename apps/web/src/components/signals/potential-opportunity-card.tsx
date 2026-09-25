"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api-client";
import type { BusinessSignal } from "@/lib/types";

function formatValue(cents: number, currency: string): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

type Busy = "confirm" | "dismiss" | null;

interface PotentialOpportunityCardProps {
  signal: BusinessSignal;
  onConfirm: (signal: BusinessSignal) => Promise<void>;
  onDismiss: (signal: BusinessSignal) => Promise<void>;
}

/** A single pending `potential_opportunity` decision: confirm creates the
 * Opportunity through the backend (never here); dismiss just marks the signal
 * declined. Reused by both Today's "Potential opportunities" queue and the
 * Inbox conversation detail pane, so the confirm/dismiss behavior — including
 * "an error leaves the signal visible, never silently removes it" — lives in
 * exactly one place. */
export function PotentialOpportunityCard({
  signal,
  onConfirm,
  onDismiss,
}: PotentialOpportunityCardProps) {
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const title = signal.title || "Potential opportunity";

  const act = async (kind: Busy, action: (signal: BusinessSignal) => Promise<void>) => {
    if (busy) return; // a second click while one decision is in flight is a no-op
    setBusy(kind);
    setError(null);
    try {
      await action(signal);
      // On success the parent removes this signal from its list; there is
      // nothing further to render from this instance.
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn’t save that decision. Try again.");
      setBusy(null);
    }
  };

  return (
    <div className="space-y-2.5 rounded-lg border p-4">
      <div>
        <p className="font-medium">{title}</p>
        {signal.summary && signal.summary !== title ? (
          <p className="text-sm text-muted-foreground">{signal.summary}</p>
        ) : null}
      </div>

      {signal.estimated_value_cents !== null && signal.currency ? (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>
            Potential value: {formatValue(signal.estimated_value_cents, signal.currency)}
            {signal.value_is_explicit === false ? " (estimated)" : null}
          </span>
          {signal.confidence !== null ? (
            <span>Confidence: {Math.round(signal.confidence * 100)}%</span>
          ) : null}
        </div>
      ) : signal.confidence !== null ? (
        <p className="text-xs text-muted-foreground">
          Confidence: {Math.round(signal.confidence * 100)}%
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}

      <div className="flex gap-2 pt-0.5">
        <Button
          size="sm"
          disabled={busy !== null}
          aria-label={`Confirm opportunity: ${title}`}
          onClick={() => void act("confirm", onConfirm)}
        >
          {busy === "confirm" ? "Confirming…" : "Confirm opportunity"}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={busy !== null}
          aria-label={`Dismiss: ${title}`}
          onClick={() => void act("dismiss", onDismiss)}
        >
          {busy === "dismiss" ? "Dismissing…" : "Dismiss"}
        </Button>
      </div>
    </div>
  );
}
