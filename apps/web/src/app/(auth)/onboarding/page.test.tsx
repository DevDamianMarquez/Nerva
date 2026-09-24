import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { User } from "@/lib/types";

import OnboardingPage from "./page";

const replace = vi.fn();
let authState: { status: string; user: User | null; workspace: unknown };
const refresh = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ ...authState, refresh }) }));
vi.mock("@/lib/api-client", () => {
  class MockApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { api: { post: vi.fn(), patch: vi.fn() }, ApiError: MockApiError };
});

const mockedApi = vi.mocked(api, true);

const user: User = { id: "u1", email: "alex@example.com", name: "Alex Morgan", created_at: "" };

describe("OnboardingPage", () => {
  beforeEach(() => {
    authState = { status: "authenticated", user, workspace: null };
    replace.mockClear();
    refresh.mockClear();
    mockedApi.post.mockReset();
    mockedApi.patch.mockReset();
  });

  it("shows the welcome step first, then the workspace form pre-filled with the user's name", async () => {
    render(<OnboardingPage />);

    expect(screen.getByRole("heading", { name: "Welcome to Nerva" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Get started" }));

    expect(screen.getByRole("heading", { name: "Create your workspace" })).toBeInTheDocument();
    expect(screen.getByLabelText(/Your display name/)).toHaveValue("Alex Morgan");
  });

  it("creates the workspace and redirects to /today", async () => {
    mockedApi.post.mockResolvedValue({
      id: "w1",
      name: "Acme",
      slug: "acme",
      created_at: "",
      role: "owner",
    });

    render(<OnboardingPage />);
    await userEvent.click(screen.getByRole("button", { name: "Get started" }));
    await userEvent.type(screen.getByLabelText("Workspace name"), "Acme Inc.");
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces", { name: "Acme Inc." }),
    );
    expect(mockedApi.patch).not.toHaveBeenCalled(); // display name was not changed
    expect(refresh).toHaveBeenCalled();
    expect(replace).toHaveBeenCalledWith("/today");
  });

  it("updates the display name only when it was changed", async () => {
    mockedApi.post.mockResolvedValue({
      id: "w1",
      name: "Acme",
      slug: "acme",
      created_at: "",
      role: "owner",
    });
    mockedApi.patch.mockResolvedValue({ ...user, name: "Alex Chen" });

    render(<OnboardingPage />);
    await userEvent.click(screen.getByRole("button", { name: "Get started" }));
    await userEvent.clear(screen.getByLabelText(/Your display name/));
    await userEvent.type(screen.getByLabelText(/Your display name/), "Alex Chen");
    await userEvent.type(screen.getByLabelText("Workspace name"), "Acme Inc.");
    await userEvent.click(screen.getByRole("button", { name: "Create workspace" }));

    await waitFor(() =>
      expect(mockedApi.patch).toHaveBeenCalledWith("/v1/auth/me", { name: "Alex Chen" }),
    );
  });

  it("does not force onboarding again once the user already has a workspace", () => {
    authState = { status: "authenticated", user, workspace: { id: "w1" } };

    render(<OnboardingPage />);

    expect(replace).toHaveBeenCalledWith("/today");
  });

  it("sends unauthenticated visitors to login", () => {
    authState = { status: "unauthenticated", user: null, workspace: null };

    render(<OnboardingPage />);

    expect(replace).toHaveBeenCalledWith("/login");
  });
});
