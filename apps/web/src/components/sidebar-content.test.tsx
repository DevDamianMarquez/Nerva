import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarContent } from "@/components/sidebar-content";

let mockPathname = "/today";
const replace = vi.fn();
const logout = vi.fn().mockResolvedValue(undefined);

vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/auth-context", () => ({
  useAuth: () => ({
    user: { id: "u1", email: "alex@example.com", name: "Alex Morgan", created_at: "" },
    workspace: {
      id: "w1",
      name: "Northwind Studio",
      slug: "northwind-studio",
      created_at: "",
      role: "owner",
    },
    logout,
  }),
}));

describe("SidebarContent", () => {
  beforeEach(() => {
    mockPathname = "/today";
    replace.mockClear();
    logout.mockClear();
  });

  it("shows the primary navigation in order", () => {
    render(<SidebarContent />);

    const nav = screen.getByRole("navigation", { name: "Primary" });
    const labels = within(nav)
      .getAllByRole("link")
      .map((link) => link.textContent);

    expect(labels).toEqual(["Today", "Inbox", "Opportunities", "Nerva", "Activity"]);
  });

  it("marks only the current page as current", () => {
    mockPathname = "/opportunities";
    render(<SidebarContent />);

    expect(screen.getByRole("link", { name: "Opportunities" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getAllByRole("link", { current: "page" })).toHaveLength(1);
  });

  it("keeps a section highlighted on its nested pages", () => {
    mockPathname = "/opportunities/abc";
    render(<SidebarContent />);

    expect(screen.getByRole("link", { name: "Opportunities" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("puts Settings and the real workspace and user area at the bottom", () => {
    mockPathname = "/settings";
    render(<SidebarContent />);

    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("Northwind Studio")).toBeInTheDocument();
    expect(screen.getByText(/Alex Morgan/)).toBeInTheDocument();
    expect(screen.getByText(/Owner/)).toBeInTheDocument();
  });

  it("links every item to its route", () => {
    render(<SidebarContent />);

    expect(screen.getByRole("link", { name: "Inbox" })).toHaveAttribute("href", "/inbox");
    expect(screen.getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/activity");
    expect(screen.getByRole("link", { name: "Settings" })).toHaveAttribute("href", "/settings");
  });

  it("notifies the parent after navigating, so the mobile drawer can close", async () => {
    const onNavigate = vi.fn();
    const { container } = render(<SidebarContent onNavigate={onNavigate} />);
    container.addEventListener("click", (event) => event.preventDefault()); // jsdom cannot navigate

    await userEvent.click(screen.getByRole("link", { name: "Inbox" }));

    expect(onNavigate).toHaveBeenCalledTimes(1);
  });

  it("signs out and sends the user to login", async () => {
    render(<SidebarContent />);

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(logout).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
