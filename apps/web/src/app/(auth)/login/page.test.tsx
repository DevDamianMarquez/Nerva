import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";

import LoginPage from "./page";

const replace = vi.fn();
let authState: { status: string; workspace: unknown };
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
  return { api: { post: vi.fn() }, ApiError: MockApiError };
});

const mockedApi = vi.mocked(api, true);

describe("LoginPage", () => {
  beforeEach(() => {
    authState = { status: "unauthenticated", workspace: null };
    replace.mockClear();
    refresh.mockClear();
    mockedApi.post.mockReset();
  });

  it("submits credentials and refreshes the session on success", async () => {
    mockedApi.post.mockResolvedValue({ id: "u1", email: "a@b.com", name: "A", created_at: "" });

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/auth/login", {
        email: "a@b.com",
        password: "correct horse battery",
      }),
    );
    expect(refresh).toHaveBeenCalled();
  });

  it("shows an error for incorrect credentials", async () => {
    const { ApiError } = await import("@/lib/api-client");
    mockedApi.post.mockRejectedValue(new ApiError(401, "unauthenticated", "nope"));

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Incorrect email or password.");
    expect(refresh).not.toHaveBeenCalled();
  });

  it("disables the form while submitting", async () => {
    let resolvePost: (value: unknown) => void = () => {};
    mockedApi.post.mockReturnValue(
      new Promise((resolve) => {
        resolvePost = resolve;
      }),
    );

    render(<LoginPage />);
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
    expect(screen.getByLabelText("Email")).toBeDisabled();
    resolvePost({ id: "u1", email: "a@b.com", name: "A", created_at: "" });
  });

  it("redirects to /today when already authenticated with a workspace", () => {
    authState = { status: "authenticated", workspace: { id: "w1" } };

    render(<LoginPage />);

    expect(replace).toHaveBeenCalledWith("/today");
  });

  it("redirects to /onboarding when authenticated without a workspace", () => {
    authState = { status: "authenticated", workspace: null };

    render(<LoginPage />);

    expect(replace).toHaveBeenCalledWith("/onboarding");
  });
});
