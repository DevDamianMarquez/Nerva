"use client";

import { CheckCircle2, Mail, XCircle } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type {
  AnalysisBackfillResult,
  GoogleConnectResponse,
  Integration,
  Member,
  SyncResult,
} from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters =
    parts.length >= 2 ? [parts[0]?.[0], parts[parts.length - 1]?.[0]] : [name.slice(0, 2)];
  return letters.join("").toUpperCase() || "?";
}

function roleLabel(role: string): string {
  return role.charAt(0).toUpperCase() + role.slice(1);
}

function connectErrorMessage(reason: string | null): string {
  switch (reason) {
    case "google_auth_failed":
      return "Google could not verify that request. Please try connecting again.";
    case "missing_refresh_token":
      return "Google did not grant lasting access. Please try connecting again.";
    case "state_user_mismatch":
    case "not_a_member":
      return "That connection attempt did not match your current session. Please try again.";
    case "invalid_state":
      return "That connection link expired or was already used. Please try again.";
    default:
      return "Connecting Gmail did not complete. Please try again.";
  }
}

function SettingsSection({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  const headingId = `${id}-heading`;
  return (
    <section
      aria-labelledby={headingId}
      className="grid gap-4 md:grid-cols-[14rem_minmax(0,1fr)] md:gap-10"
    >
      <div className="space-y-1">
        <h2 id={headingId} className="text-sm font-semibold tracking-tight">
          {title}
        </h2>
        <p className="text-muted-foreground">{description}</p>
      </div>
      <div>{children}</div>
    </section>
  );
}

function GmailIntegration({
  workspaceId,
  integration,
  onChange,
}: {
  workspaceId: string;
  integration: Integration | null;
  onChange: () => Promise<void>;
}) {
  const [connecting, setConnecting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [syncResult, setSyncResult] = useState<SyncResult | null>(null);

  const connect = async () => {
    setActionError(null);
    setConnecting(true);
    try {
      const { authorization_url } = await api.post<GoogleConnectResponse>(
        `/v1/workspaces/${workspaceId}/integrations/google/connect`,
      );
      window.location.href = authorization_url;
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not start connecting Gmail.");
      setConnecting(false);
    }
  };

  const sync = async () => {
    if (!integration || syncing) return;
    setActionError(null);
    setSyncResult(null);
    setSyncing(true);
    try {
      const result = await api.post<SyncResult>(
        `/v1/workspaces/${workspaceId}/integrations/${integration.id}/sync`,
      );
      setSyncResult(result);
      await onChange();
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.message : "Could not sync Gmail. Try again shortly.",
      );
    } finally {
      setSyncing(false);
    }
  };

  const disconnect = async () => {
    if (!integration) return;
    setActionError(null);
    try {
      await api.post(`/v1/workspaces/${workspaceId}/integrations/${integration.id}/disconnect`);
      await onChange();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Could not disconnect Gmail.");
    }
  };

  const connected = integration && integration.status !== "disconnected";
  // A `connected` integration can still lack `gmail.send` — connected before this
  // scope was requested, or reconnected under an older scope list. Distinct from
  // `needs_reauth`: Google has not revoked anything, Nerva just never asked for
  // permission to send. Treated the same way in the UI (a "reconnect" action fixes
  // both), but never shown as if it were already fully connected.
  const needsSendScope = Boolean(
    connected && integration.status === "connected" && !integration.can_send,
  );
  const needsReconnect = !connected || integration?.status === "needs_reauth" || needsSendScope;

  return (
    <ul className="divide-y rounded-lg border">
      <li className="flex flex-wrap items-center gap-x-4 gap-y-3 px-4 py-4">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background text-muted-foreground">
          <Mail className="size-4" aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1 space-y-0.5">
          {connected ? (
            <>
              <p className="flex flex-wrap items-center gap-2 font-medium">
                {integration.external_account_email}
                {integration.status === "needs_reauth" ? (
                  <Badge variant="destructive">Needs attention</Badge>
                ) : integration.status === "error" ? (
                  <Badge variant="destructive">Sync failed</Badge>
                ) : needsSendScope ? (
                  <Badge variant="outline">Read-only</Badge>
                ) : (
                  <Badge variant="secondary">Connected</Badge>
                )}
              </p>
              <p className="text-muted-foreground">
                {integration.status === "needs_reauth"
                  ? "Reconnect Gmail to keep syncing your conversations."
                  : needsSendScope
                    ? "Reconnect Gmail to let Nerva send approved emails from this mailbox."
                    : integration.last_synced_at
                      ? `Last synced: ${new Date(integration.last_synced_at).toLocaleString()}`
                      : "Not synced yet."}
              </p>
            </>
          ) : (
            <>
              <p className="flex items-center gap-2 font-medium">
                Gmail <Badge variant="outline">Not connected</Badge>
              </p>
              <p className="text-muted-foreground">
                Nerva requests permission to read your business conversations and to send email,
                used only for an action you&rsquo;ve explicitly approved. It cannot delete or change
                anything else in your mailbox.
              </p>
            </>
          )}
          {actionError ? (
            <p role="alert" className="text-destructive">
              {actionError}
            </p>
          ) : syncResult ? (
            <p className="text-muted-foreground">
              Synced {syncResult.messages_created}{" "}
              {syncResult.messages_created === 1 ? "message" : "messages"}.
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 gap-2">
          {needsReconnect ? (
            <Button onClick={() => void connect()} disabled={connecting}>
              {connecting ? "Redirecting…" : integration ? "Reconnect Gmail" : "Connect Gmail"}
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => void sync()} disabled={syncing}>
                {syncing ? "Syncing…" : "Sync now"}
              </Button>
              <Button variant="outline" onClick={() => void disconnect()}>
                Disconnect
              </Button>
            </>
          )}
        </div>
      </li>
    </ul>
  );
}

function summarizeBackfillResult(result: AnalysisBackfillResult): string {
  if (result.considered === 0) {
    return "Everything is already up to date. Nothing needed analysis.";
  }
  const parts = [
    `Analyzed ${result.analyzed} ${result.analyzed === 1 ? "conversation" : "conversations"}.`,
  ];
  if (result.cached > 0) {
    parts.push(`${result.cached} already up to date.`);
  }
  if (result.failed > 0) {
    parts.push(`${result.failed} failed.`);
  }
  if (result.signals_created > 0) {
    const noun = result.signals_created === 1 ? "opportunity" : "opportunities";
    parts.push(`${result.signals_created} potential ${noun} found.`);
  }
  return parts.join(" ");
}

function AiUnderstanding({ workspaceId }: { workspaceId: string }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AnalysisBackfillResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const backfillResult = await api.post<AnalysisBackfillResult>(
        `/v1/workspaces/${workspaceId}/analysis/backfill`,
      );
      setResult(backfillResult);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not run analysis. Try again shortly.",
      );
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="max-w-xl space-y-3">
      <p className="text-muted-foreground">
        Nerva can read your imported conversations to summarize them and surface potential
        opportunities. Each run analyzes a bounded batch — run it again if more remain.
      </p>
      <Button variant="outline" onClick={() => void run()} disabled={running}>
        {running ? "Analyzing…" : "Run analysis"}
      </Button>
      {error ? (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      ) : result ? (
        <p role="status" className="text-muted-foreground">
          {summarizeBackfillResult(result)}
        </p>
      ) : null}
    </div>
  );
}

export function SettingsView() {
  const { workspace } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [members, setMembers] = useState<Member[]>([]);
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [state, setState] = useState<LoadState>("loading");
  // Derived from the query string on first render (the OAuth callback redirects here
  // with `?integration=...`) — computed directly rather than in an effect, since it
  // only ever needs the value the page was loaded with.
  const [connectBanner] = useState<{ ok: boolean; reason: string | null } | null>(() => {
    const integrationParam = searchParams.get("integration");
    if (!integrationParam) return null;
    return { ok: integrationParam === "connected", reason: searchParams.get("reason") };
  });

  const load = useCallback(async () => {
    if (!workspace) return;
    setState("loading");
    try {
      const [memberList, integrationList] = await Promise.all([
        api.get<Member[]>(`/v1/workspaces/${workspace.id}/members`),
        api.get<Integration[]>(`/v1/workspaces/${workspace.id}/integrations`),
      ]);
      setMembers(memberList);
      setIntegrations(integrationList);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [workspace]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  useEffect(() => {
    if (searchParams.get("integration")) {
      router.replace("/settings");
    }
  }, [searchParams, router]);

  if (!workspace) return null;

  const googleIntegration =
    integrations.find((integration) => integration.provider === "google") ?? null;

  return (
    <PageContainer width="default">
      <PageHeader
        title="Settings"
        description="Manage your workspace, who has access to it, and the tools Nerva connects to."
      />

      {connectBanner ? (
        <div
          role="status"
          className={`flex items-center gap-2 rounded-lg border px-4 py-3 text-sm ${
            connectBanner.ok
              ? "border-success/30 bg-success/10 text-success"
              : "border-destructive/30 bg-destructive/10 text-destructive"
          }`}
        >
          {connectBanner.ok ? (
            <CheckCircle2 className="size-4 shrink-0" aria-hidden="true" />
          ) : (
            <XCircle className="size-4 shrink-0" aria-hidden="true" />
          )}
          {connectBanner.ok ? "Gmail was connected." : connectErrorMessage(connectBanner.reason)}
        </div>
      ) : null}

      <SettingsSection
        id="workspace"
        title="Workspace"
        description="How your company appears in Nerva."
      >
        <div className="max-w-md space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="workspace-name" className="text-sm font-medium">
              Name
            </label>
            <Input id="workspace-name" defaultValue={workspace.name} readOnly />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="workspace-slug" className="text-sm font-medium">
              Workspace URL
            </label>
            <Input id="workspace-slug" defaultValue={workspace.slug} readOnly />
          </div>
          <p className="text-xs text-muted-foreground">Editing workspace details is coming soon.</p>
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        id="members"
        title="Members"
        description="People who can see this workspace and approve what Nerva proposes."
      >
        <div className="space-y-4">
          {state === "error" ? (
            <LoadError onRetry={() => void load()} />
          ) : (
            <ul className="divide-y rounded-lg border">
              {state === "loading" ? (
                <li className="px-4 py-8 text-center text-muted-foreground">Loading…</li>
              ) : (
                members.map((member) => (
                  <li key={member.user_id} className="flex items-center gap-3 px-4 py-3">
                    <Avatar className="size-8">
                      <AvatarFallback className="text-xs font-semibold">
                        {initials(member.name)}
                      </AvatarFallback>
                    </Avatar>
                    <div className="min-w-0 flex-1 leading-tight">
                      <p className="truncate font-medium">{member.name}</p>
                      <p className="truncate text-xs text-muted-foreground">{member.email}</p>
                    </div>
                    <Badge variant="secondary">{roleLabel(member.role)}</Badge>
                  </li>
                ))
              )}
            </ul>
          )}
          <Button variant="outline" disabled>
            Invite a teammate
          </Button>
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        id="integrations"
        title="Integrations"
        description="The tools Nerva reads from and acts in, always with your approval."
      >
        {state === "loading" ? (
          <div className="rounded-lg border px-4 py-8 text-center text-muted-foreground">
            Loading…
          </div>
        ) : (
          <GmailIntegration
            workspaceId={workspace.id}
            integration={googleIntegration}
            onChange={load}
          />
        )}
        <p className="mt-3 text-xs text-muted-foreground">
          Nerva requests read access to Gmail, plus permission to send an email only for an
          action you&rsquo;ve approved. It cannot delete or change anything else in your mailbox.
        </p>
      </SettingsSection>

      <Separator />

      <SettingsSection
        id="ai-understanding"
        title="AI Understanding"
        description="How Nerva reads and makes sense of your conversations."
      >
        <AiUnderstanding workspaceId={workspace.id} />
      </SettingsSection>
    </PageContainer>
  );
}
