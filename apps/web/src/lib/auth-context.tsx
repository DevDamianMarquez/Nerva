"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api, ApiError } from "@/lib/api-client";
import type { Me, User, Workspace } from "@/lib/types";

type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

interface AuthContextValue {
  status: AuthStatus;
  user: User | null;
  workspaces: Workspace[];
  /** The primary workspace for this milestone: the first one the user belongs to. */
  workspace: Workspace | null;
  /** Re-fetches the session. Call after signup, login, onboarding or a workspace change. */
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<User | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<Me>("/v1/auth/me");
      setUser(me.user);
      setWorkspaces(me.workspaces);
      setStatus("authenticated");
    } catch (error) {
      setUser(null);
      setWorkspaces([]);
      setStatus(error instanceof ApiError && error.status === 401 ? "unauthenticated" : "error");
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(refresh);
  }, [refresh]);

  const logout = useCallback(async () => {
    await api.post("/v1/auth/logout");
    setUser(null);
    setWorkspaces([]);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ status, user, workspaces, workspace: workspaces[0] ?? null, refresh, logout }),
    [status, user, workspaces, refresh, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within an AuthProvider");
  return context;
}
