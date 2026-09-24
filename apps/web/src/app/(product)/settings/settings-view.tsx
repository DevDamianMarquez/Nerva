"use client";

import { Mail } from "lucide-react";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { api } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { Member } from "@/lib/types";

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

export function SettingsView() {
  const { workspace } = useAuth();
  const [members, setMembers] = useState<Member[]>([]);
  const [state, setState] = useState<LoadState>("loading");

  const load = useCallback(async () => {
    if (!workspace) return;
    setState("loading");
    try {
      setMembers(await api.get<Member[]>(`/v1/workspaces/${workspace.id}/members`));
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
    <PageContainer width="default">
      <PageHeader
        title="Settings"
        description="Manage your workspace, who has access to it, and the tools Nerva connects to."
      />

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
        <ul className="divide-y rounded-lg border">
          <li className="flex flex-wrap items-center gap-x-4 gap-y-3 px-4 py-4">
            <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-background text-muted-foreground">
              <Mail className="size-4" aria-hidden="true" />
            </div>
            <div className="min-w-0 flex-1 space-y-0.5">
              <p className="flex items-center gap-2 font-medium">
                Gmail <Badge variant="outline">Not connected</Badge>
              </p>
              <p className="text-muted-foreground">
                Find leads in your conversations and send the replies you approve.
              </p>
            </div>
            <Button variant="outline" disabled>
              Connect
            </Button>
          </li>
        </ul>
        <p className="mt-3 text-xs text-muted-foreground">
          Gmail is the first integration and arrives in the next release.
        </p>
      </SettingsSection>
    </PageContainer>
  );
}
