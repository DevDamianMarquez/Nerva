"use client";

import { useEffect, useState } from "react";

import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Section } from "@/components/section";
import { PotentialOpportunityCard } from "@/components/signals/potential-opportunity-card";
import { ActivityList } from "@/components/today/activity-list";
import { NeedsAttention } from "@/components/today/needs-attention";
import { PendingApprovals } from "@/components/today/pending-approvals";
import { api } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { Action, ActivityEvent, BusinessSignal, ConversationAttention } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export function TodayView() {
  const { workspace } = useAuth();
  const [actions, setActions] = useState<Action[]>([]);
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [attention, setAttention] = useState<ConversationAttention[]>([]);
  const [signals, setSignals] = useState<BusinessSignal[]>([]);
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
      api.get<ConversationAttention[]>(`/v1/workspaces/${workspace.id}/conversations/attention`),
      api.get<BusinessSignal[]>(`/v1/workspaces/${workspace.id}/signals?status=pending`),
    ])
      .then(([proposed, recent, attentionList, pendingSignals]) => {
        if (cancelled) return;
        setActions(proposed);
        setActivity(recent);
        setAttention(attentionList);
        setSignals(pendingSignals);
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

  const confirmSignal = async (signal: BusinessSignal) => {
    await api.post(`/v1/workspaces/${workspaceId}/signals/${signal.id}/confirm`);
    setSignals((current) => current.filter((s) => s.id !== signal.id));
  };

  const dismissSignal = async (signal: BusinessSignal) => {
    await api.post(`/v1/workspaces/${workspaceId}/signals/${signal.id}/dismiss`);
    setSignals((current) => current.filter((s) => s.id !== signal.id));
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
          <div className="space-y-10">
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

            <Section id="opportunities" title="Potential opportunities">
              {state === "loading" ? (
                <p className="py-8 text-center text-muted-foreground">Loading…</p>
              ) : signals.length === 0 ? (
                <p className="py-8 text-center text-muted-foreground">
                  No potential opportunities right now.
                </p>
              ) : (
                <div className="space-y-3 py-4">
                  {signals.map((signal) => (
                    <PotentialOpportunityCard
                      key={signal.id}
                      signal={signal}
                      onConfirm={confirmSignal}
                      onDismiss={dismissSignal}
                    />
                  ))}
                </div>
              )}
            </Section>

            <Section id="attention" title="Needs your attention">
              {state === "loading" ? (
                <p className="py-8 text-center text-muted-foreground">Loading…</p>
              ) : (
                <NeedsAttention items={attention} />
              )}
            </Section>
          </div>

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
