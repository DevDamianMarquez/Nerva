import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { ActivityEvent } from "@/lib/types";

import { ActivityView } from "./activity-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = { id: "w1", name: "Acme", slug: "acme", created_at: "", role: "owner" as const };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn() },
}));

const mockedApi = vi.mocked(api, true);

const event: ActivityEvent = {
  id: "e1",
  workspace_id: "w1",
  actor_type: "user",
  actor_user_id: "u1",
  event_type: "workspace.created",
  entity_type: "workspace",
  entity_id: "w1",
  summary: "Workspace “Acme” was created.",
  data: {},
  created_at: "2024-01-01T00:00:00Z",
};

describe("ActivityView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
  });

  it("shows an empty state and what will be recorded when there is no activity yet", async () => {
    mockedApi.get.mockResolvedValue([]);

    render(<ActivityView />);

    expect(await screen.findByRole("heading", { name: "No activity yet" })).toBeInTheDocument();
    expect(screen.getByText(/Each approval or rejection/)).toBeInTheDocument();
  });

  it("lists real activity events once loaded", async () => {
    mockedApi.get.mockResolvedValue([event]);

    render(<ActivityView />);

    expect(await screen.findByText("Workspace “Acme” was created.")).toBeInTheDocument();
  });

  it("offers a retry option when loading fails", async () => {
    mockedApi.get.mockRejectedValue(new Error("network"));

    render(<ActivityView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
