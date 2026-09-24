import { Mail } from "lucide-react";
import type { Metadata } from "next";

import { EmptyState } from "@/components/empty-state";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Inbox" };

const principles = [
  {
    title: "Read-only to start",
    body: "Nerva reads your conversations to find leads and follow-ups. It changes nothing in your mailbox.",
  },
  {
    title: "You approve every action",
    body: "Drafts and follow-ups are proposed to you first. Nothing is sent until you say so.",
  },
  {
    title: "Every step is recorded",
    body: "Each proposal and decision lands in your activity log, so you can always see what happened.",
  },
] as const;

export default function InboxPage() {
  return (
    <PageContainer width="narrow">
      <PageHeader
        title="Inbox"
        description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
      />

      <div className="rounded-xl border border-dashed">
        <EmptyState
          icon={Mail}
          title="Connect your inbox to get started"
          description="Nerva reads your email to find people worth following up with, then proposes what to do next. Once Gmail is connected, your conversations appear here."
        >
          <Button disabled>
            <Mail data-icon="inline-start" aria-hidden="true" />
            Connect Gmail
          </Button>
          <p className="text-xs text-muted-foreground">
            The Gmail connection is coming in the next release.
          </p>
        </EmptyState>
      </div>

      <section aria-labelledby="principles-heading">
        <h2 id="principles-heading" className="mb-4 text-sm font-semibold tracking-tight">
          How Nerva will work with your inbox
        </h2>
        <ul className="grid gap-x-8 gap-y-5 sm:grid-cols-3">
          {principles.map((principle) => (
            <li key={principle.title} className="space-y-1">
              <p className="font-medium">{principle.title}</p>
              <p className="text-muted-foreground">{principle.body}</p>
            </li>
          ))}
        </ul>
      </section>
    </PageContainer>
  );
}
