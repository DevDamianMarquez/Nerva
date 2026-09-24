"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type { User, Workspace } from "@/lib/types";

type Step = "welcome" | "workspace";

export default function OnboardingPage() {
  const router = useRouter();
  const { status, user, workspace, refresh } = useAuth();
  const [step, setStep] = useState<Step>("welcome");
  const [workspaceName, setWorkspaceName] = useState("");
  // The field defaults to the user's current name until they type something of their
  // own; that needs no effect, since it can be computed directly during render.
  const [displayNameInput, setDisplayNameInput] = useState<string | null>(null);
  const displayName = displayNameInput ?? user?.name ?? "";
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (status === "unauthenticated") router.replace("/login");
    else if (status === "authenticated" && workspace) router.replace("/today");
  }, [status, workspace, router]);

  if (status !== "authenticated" || workspace || !user) return null;

  const onSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const trimmedName = displayName.trim();
      if (trimmedName && trimmedName !== user.name) {
        await api.patch<User>("/v1/auth/me", { name: trimmedName });
      }
      await api.post<Workspace>("/v1/workspaces", { name: workspaceName.trim() });
      await refresh();
      router.replace("/today");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Please try again.");
      setSubmitting(false);
    }
  };

  if (step === "welcome") {
    return (
      <div className="space-y-6 rounded-xl border bg-card p-6 text-center text-card-foreground shadow-xs sm:p-8">
        <div className="space-y-1.5">
          <h1 className="text-lg font-semibold tracking-tight">Welcome to Nerva</h1>
          <p className="text-sm text-muted-foreground">
            Let&rsquo;s set up your workspace. It only takes a moment.
          </p>
        </div>
        <Button className="w-full" onClick={() => setStep("workspace")} autoFocus>
          Get started
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-6 rounded-xl border bg-card p-6 text-card-foreground shadow-xs sm:p-8">
      <div className="space-y-1.5">
        <h1 className="text-lg font-semibold tracking-tight">Create your workspace</h1>
        <p className="text-sm text-muted-foreground">
          This is where your team&rsquo;s leads, actions and activity will live.
        </p>
      </div>

      <form onSubmit={onSubmit} className="space-y-4">
        <div className="space-y-1.5">
          <label htmlFor="workspace-name" className="text-sm font-medium">
            Workspace name
          </label>
          <Input
            id="workspace-name"
            required
            minLength={1}
            maxLength={120}
            placeholder="Acme Inc."
            value={workspaceName}
            onChange={(event) => setWorkspaceName(event.target.value)}
            disabled={submitting}
            autoFocus
          />
        </div>
        <div className="space-y-1.5">
          <label htmlFor="display-name" className="text-sm font-medium">
            Your display name <span className="font-normal text-muted-foreground">(optional)</span>
          </label>
          <Input
            id="display-name"
            maxLength={200}
            value={displayName}
            onChange={(event) => setDisplayNameInput(event.target.value)}
            disabled={submitting}
          />
        </div>

        {error ? (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        ) : null}

        <Button type="submit" className="w-full" disabled={submitting || !workspaceName.trim()}>
          {submitting ? "Creating workspace…" : "Create workspace"}
        </Button>
      </form>
    </div>
  );
}
