"use client";

import { useEffect, useState } from "react";

import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Section } from "@/components/section";
import { ActivityList } from "@/components/today/activity-list";
import { PendingApprovals } from "@/components/today/pending-approvals";
import { api } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { Action, ActivityEvent } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export function TodayView() {
  const { workspace } = useAuth();
  const [actions, setActions] = useState<Action[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  // Bump this to re-run the effect below: after a decision, or from the retry button.
  const [reloadToken, setReloadToken] = useState(0);
  const reload = () => {
    setState("loading");
    setReloadToken((token) => token + 1);
  };

  useEffect(() => {
    if (!workspace) return;
    let cancelled = false;
    Promise.all([
      api.get<Action[]>(`/v1/workspaces/${workspace.id}/actions?status=proposed`),
      api.get<ActivityEvent[]>(`/v1/workspaces/${workspace.id}/activity?limit=10`),
    ])
      .then(([proposed, recent]) => {
        if (cancelled) return;
        setActions(proposed);
        setActivity(recent);
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("error");
      });
    return () => {
      cancelled = true;
    };
  }, [workspace, reloadToken]);

  if (!workspace) return null;
  const workspaceId = workspace.id;

  const decide = async (actionId: string, decision: "approve" | "reject") => {
    await api.post<Action>(`/v1/workspaces/${workspaceId}/actions/${actionId}/${decision}`);
    reload();
  };

  return (
    <PageContainer width="wide">
      <PageHeader
        title="Today"
        description="What Nerva is waiting on you to approve, and what has happened recently."
      />

      {state === "error" ? (
        <LoadError onRetry={reload} />
      ) : (
        <div className="grid gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_19rem]">
          <Section id="approvals" title="Pending approvals">
            {state === "loading" ? (
              <p className="py-8 text-center text-muted-foreground">Loading…</p>
            ) : (
              <PendingApprovals
                actions={actions}
                onApprove={(id) => decide(id, "approve")}
                onReject={(id) => decide(id, "reject")}
              />
            )}
          </Section>

          <Section id="activity" title="Recent activity">
            {state === "loading" ? (
              <p className="py-8 text-center text-muted-foreground">Loading…</p>
            ) : activity.length === 0 ? (
              <p className="py-8 text-center text-muted-foreground">Nothing has happened yet.</p>
            ) : (
              <ActivityList items={activity} />
            )}
          </Section>
        </div>
      )}
    </PageContainer>
  );
}
