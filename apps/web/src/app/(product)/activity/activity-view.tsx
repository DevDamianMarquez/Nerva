"use client";

import { History } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { ActivityList } from "@/components/today/activity-list";
import { api } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { ActivityEvent } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

const recorded = [
  "Every action Nerva proposes",
  "Each approval or rejection, and who made it",
  "Contacts, companies and opportunities as they are created",
  "What happened when an approved action ran",
] as const;

export function ActivityView() {
  const { workspace } = useAuth();
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [state, setState] = useState<LoadState>("loading");

  const load = useCallback(async () => {
    if (!workspace) return;
    setState("loading");
    try {
      setEvents(await api.get<ActivityEvent[]>(`/v1/workspaces/${workspace.id}/activity`));
      setState("ready");
    } catch {
      setState("error");
    }
  }, [workspace]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  if (!workspace) return null;

  return (
    <PageContainer width="narrow">
      <PageHeader
        title="Activity"
        description="A permanent record of what Nerva and your team have done in this workspace."
      />

      <div className="rounded-xl border">
        {state === "error" ? (
          <LoadError onRetry={() => void load()} />
        ) : state === "loading" ? (
          <p className="py-14 text-center text-muted-foreground">Loading…</p>
        ) : events.length === 0 ? (
          <EmptyState
            icon={History}
            title="No activity yet"
            description="Once you start working in Nerva, every important step is recorded here in order, so you can always trace what happened and why."
          />
        ) : (
          <div className="px-4">
            <ActivityList items={events} />
          </div>
        )}
      </div>

      <section aria-labelledby="recorded-heading">
        <h2 id="recorded-heading" className="mb-3 text-sm font-semibold tracking-tight">
          What gets recorded
        </h2>
        <ul className="list-disc space-y-1.5 pl-5 text-muted-foreground marker:text-border">
          {recorded.map((entry) => (
            <li key={entry}>{entry}</li>
          ))}
        </ul>
      </section>
    </PageContainer>
  );
}
