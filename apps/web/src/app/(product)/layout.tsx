"use client";

import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/lib/auth-context";

/**
 * Guards every product page: unauthenticated visitors go to `/login`, authenticated
 * visitors with no workspace go to `/onboarding`. The session cookie is set by the API
 * on a different origin, so it cannot be read on the server here; the check runs on the
 * client against `GET /v1/auth/me` instead (see `AuthProvider`).
 */
export default function ProductLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  const { status, workspace, refresh } = useAuth();

  useEffect(() => {
    if (status === "unauthenticated") router.replace("/login");
    else if (status === "authenticated" && !workspace) router.replace("/onboarding");
  }, [status, workspace, router]);

  if (status === "error") {
    return (
      <div className="flex h-dvh flex-col items-center justify-center gap-3 px-4 text-center">
        <p className="font-medium">Couldn&rsquo;t reach Nerva</p>
        <p className="max-w-sm text-sm text-muted-foreground">
          The API might be starting up or unreachable. Check your connection and try again.
        </p>
        <Button variant="outline" onClick={() => void refresh()}>
          Try again
        </Button>
      </div>
    );
  }

  // "loading", "unauthenticated" and "authenticated without a workspace" all render
  // nothing here: the effect above is already sending the visitor where they belong.
  if (status !== "authenticated" || !workspace) return null;

  return <AppShell>{children}</AppShell>;
}
