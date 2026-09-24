import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import ProductLayout from "./layout";

const replace = vi.fn();
let authState: { status: string; workspace: unknown; refresh: () => void };

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));
vi.mock("@/lib/auth-context", () => ({ useAuth: () => authState }));
// Isolate the guard from the sidebar's own real-data rendering.
vi.mock("@/components/app-shell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="app-shell">{children}</div>
  ),
}));

describe("ProductLayout (route protection)", () => {
  beforeEach(() => {
    replace.mockClear();
    authState = { status: "loading", workspace: null, refresh: vi.fn() };
  });

  it("renders nothing while the session is loading", () => {
    const { container } = render(<ProductLayout>content</ProductLayout>);

    expect(container).toBeEmptyDOMElement();
    expect(replace).not.toHaveBeenCalled();
  });

  it("redirects unauthenticated visitors to /login", () => {
    authState.status = "unauthenticated";

    render(<ProductLayout>content</ProductLayout>);

    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("redirects authenticated visitors without a workspace to /onboarding", () => {
    authState.status = "authenticated";

    render(<ProductLayout>content</ProductLayout>);

    expect(replace).toHaveBeenCalledWith("/onboarding");
  });

  it("renders the shell and its children for an authenticated visitor with a workspace", () => {
    authState.status = "authenticated";
    authState.workspace = { id: "w1" };

    render(<ProductLayout>content</ProductLayout>);

    expect(screen.getByTestId("app-shell")).toHaveTextContent("content");
    expect(replace).not.toHaveBeenCalled();
  });

  it("offers a retry option when the session could not be loaded", async () => {
    const refresh = vi.fn();
    authState = { status: "error", workspace: null, refresh };

    render(<ProductLayout>content</ProductLayout>);

    expect(screen.getByText("Couldn’t reach Nerva")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(refresh).toHaveBeenCalled();
  });
});
