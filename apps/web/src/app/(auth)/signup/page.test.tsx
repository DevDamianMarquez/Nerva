import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";

import SignupPage from "./page";

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

describe("SignupPage", () => {
  beforeEach(() => {
    authState = { status: "unauthenticated", workspace: null };
    replace.mockClear();
    refresh.mockClear();
    mockedApi.post.mockReset();
  });

  it("submits the signup form and refreshes the session on success", async () => {
    mockedApi.post.mockResolvedValue({ id: "u1", email: "a@b.com", name: "Alice", created_at: "" });

    render(<SignupPage />);
    await userEvent.type(screen.getByLabelText("Name"), "Alice");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/auth/signup", {
        name: "Alice",
        email: "a@b.com",
        password: "correct horse battery",
      }),
    );
    expect(refresh).toHaveBeenCalled();
  });

  it("shows an error when the email is already registered", async () => {
    const { ApiError } = await import("@/lib/api-client");
    mockedApi.post.mockRejectedValue(new ApiError(409, "conflict", "taken"));

    render(<SignupPage />);
    await userEvent.type(screen.getByLabelText("Name"), "Alice");
    await userEvent.type(screen.getByLabelText("Email"), "a@b.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct horse battery");
    await userEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An account with this email already exists.",
    );
    expect(refresh).not.toHaveBeenCalled();
  });

  it("redirects to /onboarding when already authenticated without a workspace", () => {
    authState = { status: "authenticated", workspace: null };

    render(<SignupPage />);

    expect(replace).toHaveBeenCalledWith("/onboarding");
  });
});
