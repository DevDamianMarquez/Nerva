import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { Member } from "@/lib/types";

import { SettingsView } from "./settings-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = {
  id: "w1",
  name: "Northwind Studio",
  slug: "northwind-studio",
  created_at: "",
  role: "owner" as const,
};
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn() },
}));

const mockedApi = vi.mocked(api, true);

const member: Member = {
  user_id: "u1",
  email: "alex@example.com",
  name: "Alex Morgan",
  role: "owner",
  joined_at: "2024-01-01T00:00:00Z",
};

describe("SettingsView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
  });

  it("shows the real workspace name and URL", async () => {
    mockedApi.get.mockResolvedValue([member]);

    render(<SettingsView />);

    expect(screen.getByLabelText("Name")).toHaveValue("Northwind Studio");
    expect(screen.getByLabelText("Workspace URL")).toHaveValue("northwind-studio");
    await screen.findByText("Alex Morgan");
  });

  it("lists real members once loaded", async () => {
    mockedApi.get.mockResolvedValue([member]);

    render(<SettingsView />);

    const members = await screen.findByRole("region", { name: "Members" });
    expect(within(members).getByText("Alex Morgan")).toBeInTheDocument();
    expect(within(members).getByText("Owner")).toBeInTheDocument();
  });

  it("shows Gmail as not connected", () => {
    mockedApi.get.mockResolvedValue([member]);

    render(<SettingsView />);

    const integrations = screen.getByRole("region", { name: "Integrations" });
    expect(within(integrations).getByText("Not connected")).toBeInTheDocument();
    expect(within(integrations).getByRole("button", { name: "Connect" })).toBeDisabled();
  });

  it("offers a retry option when the member list fails to load", async () => {
    mockedApi.get.mockRejectedValue(new Error("network"));

    render(<SettingsView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
