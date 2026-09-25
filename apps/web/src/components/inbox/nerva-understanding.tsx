import { Loader2, Sparkles } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ConversationAnalysis } from "@/lib/types";

const categoryLabels: Record<string, string> = {
  sales_lead: "Sales lead",
  customer_support: "Customer support",
  billing: "Billing",
  project: "Project",
  internal: "Internal",
  vendor: "Vendor",
  recruiting: "Recruiting",
  spam: "Spam",
  other: "Other",
};

const priorityLabels: Record<string, string> = {
  medium: "Medium priority",
  high: "High priority",
  urgent: "Urgent",
};

const sentimentLabels: Record<string, string> = {
  positive: "Positive tone",
  negative: "Negative tone",
  mixed: "Mixed tone",
};

function humanize(value: string): string {
  const spaced = value.replaceAll("_", " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function formatValue(cents: number, currency: string): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency }).format(cents / 100);
}

function formatDeadline(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

interface NervaUnderstandingProps {
  analysis: ConversationAnalysis | null;
  analyzing: boolean;
  error: string | null;
  onAnalyze: () => void;
}

/** The compact "what Nerva thinks is happening" panel in the Inbox detail pane.
 * Deliberately narrow: only business-relevant fields are shown (see the field
 * list in the Milestone 4 frontend design) — operational metadata like schema
 * version, model id, token counts and fingerprints never appear here. */
export function NervaUnderstanding({
  analysis,
  analyzing,
  error,
  onAnalyze,
}: NervaUnderstandingProps) {
  const label = analyzing ? "Analyzing…" : analysis ? "Re-analyze" : "Analyze with Nerva";
  const actionLabel = analysis
    ? "Re-analyze this conversation with Nerva"
    : "Analyze this conversation with Nerva";

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold tracking-tight">
          <Sparkles className="size-3.5 text-muted-foreground" aria-hidden="true" />
          Nerva Understanding
        </h3>
        <Button
          variant="ghost"
          size="sm"
          onClick={onAnalyze}
          disabled={analyzing}
          aria-label={actionLabel}
        >
          {analyzing ? <Loader2 className="size-3.5 animate-spin" aria-hidden="true" /> : null}
          {label}
        </Button>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-destructive">
          {error}
        </p>
      ) : analyzing ? (
        <p className="mt-3 text-sm text-muted-foreground">Nerva is reading this conversation…</p>
      ) : !analysis ? (
        <p className="mt-3 text-sm text-muted-foreground">Not analyzed yet.</p>
      ) : analysis.status === "failed" ? (
        <p className="mt-3 text-sm text-destructive">
          {analysis.last_error ?? "Nerva could not analyze this conversation."}
        </p>
      ) : (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap gap-1.5">
            {analysis.category ? (
              <Badge variant="secondary">
                {categoryLabels[analysis.category] ?? humanize(analysis.category)}
              </Badge>
            ) : null}
            {analysis.priority && analysis.priority !== "low" ? (
              <Badge variant={analysis.priority === "urgent" ? "destructive" : "outline"}>
                {priorityLabels[analysis.priority] ?? humanize(analysis.priority)}
              </Badge>
            ) : null}
            {analysis.requires_response ? <Badge variant="outline">Needs response</Badge> : null}
            {analysis.sentiment && analysis.sentiment !== "neutral" ? (
              <Badge variant="outline">
                {sentimentLabels[analysis.sentiment] ?? humanize(analysis.sentiment)}
              </Badge>
            ) : null}
          </div>

          {analysis.summary ? <p className="text-sm">{analysis.summary}</p> : null}

          {analysis.intent ? (
            <div>
              <p className="text-xs font-medium text-muted-foreground">What they want</p>
              <p className="text-sm">{humanize(analysis.intent)}</p>
            </div>
          ) : null}

          {analysis.suggested_next_step ? (
            <div>
              <p className="text-xs font-medium text-muted-foreground">Suggested next step</p>
              <p className="text-sm">{analysis.suggested_next_step}</p>
            </div>
          ) : null}

          {analysis.estimated_value_cents !== null && analysis.value_currency ? (
            <div>
              <p className="text-xs font-medium text-muted-foreground">Potential value</p>
              <p className="text-sm">
                {formatValue(analysis.estimated_value_cents, analysis.value_currency)}
                {analysis.value_is_explicit === false ? " (estimated)" : null}
              </p>
            </div>
          ) : null}

          {analysis.deadline_at ? (
            <div>
              <p className="text-xs font-medium text-muted-foreground">Deadline</p>
              <p className="text-sm">{formatDeadline(analysis.deadline_at)}</p>
            </div>
          ) : null}

          {analysis.confidence !== null ? (
            <p className="text-xs text-muted-foreground">
              {Math.round(analysis.confidence * 100)}% confidence
            </p>
          ) : null}
        </div>
      )}
    </div>
  );
}
