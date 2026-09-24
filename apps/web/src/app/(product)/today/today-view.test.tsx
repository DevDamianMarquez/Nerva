import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { Action, ActivityEvent } from "@/lib/types";

import { TodayView } from "./today-view";

// A stable reference: the real hook only produces a new `workspace` object when the
// workspace actually changes, and effects here depend on it - a fresh object per call
// would re-run those effects forever.
const workspace = { id: "w1", name: "Acme", slug: "acme", created_at: "", role: "owner" as const };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
}));

const mockedApi = vi.mocked(api, true);

const action: Action = {
  id: "a1",
  workspace_id: "w1",
  opportunity_id: null,
  type: "send_email",
  status: "proposed",
  summary: "Send follow-up",
  payload: {},
  result: null,
  created_by_type: "nerva",
  created_by_user_id: null,
  approved_by_user_id: null,
  approved_at: null,
  rejected_by_user_id: null,
  rejected_at: null,
  rejection_reason: null,
  started_at: null,
  finished_at: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
};

const activityEvent: ActivityEvent = {
  id: "e1",
  workspace_id: "w1",
  actor_type: "nerva",
  actor_user_id: null,
  event_type: "action.proposed",
  entity_type: "action",
  entity_id: "a1",
  summary: "Nerva proposed: Send follow-up",
  data: {},
  created_at: "2024-01-01T00:00:00Z",
};

describe("TodayView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
  });

  it("shows pending approvals and recent activity once loaded", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      Promise.resolve(path.includes("/actions") ? [action] : [activityEvent]),
    );

    render(<TodayView />);

    expect(await screen.findByText("Send follow-up")).toBeInTheDocument();
    const activity = screen.getByRole("region", { name: "Recent activity" });
    expect(within(activity).getByText("Nerva proposed: Send follow-up")).toBeInTheDocument();
  });

  it("shows empty-state copy when there is nothing to approve or show", async () => {
    mockedApi.get.mockResolvedValue([]);

    render(<TodayView />);

    expect(await screen.findByText("No actions need your approval.")).toBeInTheDocument();
    expect(screen.getByText("Nothing has happened yet.")).toBeInTheDocument();
  });

  it("approving an action calls the API and reloads the list", async () => {
    mockedApi.get
      .mockResolvedValueOnce([action])
      .mockResolvedValueOnce([activityEvent])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([activityEvent]);
    mockedApi.post.mockResolvedValue(action);

    render(<TodayView />);
    await screen.findByText("Send follow-up");

    await userEvent.click(screen.getByRole("button", { name: "Approve: Send follow-up" }));

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/actions/a1/approve"),
    );
    expect(await screen.findByText("No actions need your approval.")).toBeInTheDocument();
  });

  it("offers a retry option when loading fails", async () => {
    mockedApi.get.mockRejectedValue(new Error("network"));

    render(<TodayView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
