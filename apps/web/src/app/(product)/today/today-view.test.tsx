import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { Action, ActivityEvent, BusinessSignal, ConversationAttention } from "@/lib/types";

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
  conversation_id: null,
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

const attentionItem: ConversationAttention = {
  conversation_id: "c1",
  subject: "Server is down",
  last_message_at: "2024-01-02T10:00:00Z",
  category: "customer_support",
  priority: "urgent",
  requires_response: true,
  summary: "Customer reports the server is unreachable.",
  analyzed_at: "2024-01-02T10:05:00Z",
};

const signal: BusinessSignal = {
  id: "s1",
  conversation_id: "c2",
  analysis_id: "an1",
  type: "potential_opportunity",
  status: "pending",
  source_fingerprint: "fp1",
  title: "Acme Corp",
  summary: "Interested in a 20-seat plan.",
  estimated_value_cents: 400_000,
  currency: "USD",
  value_is_explicit: false,
  confidence: 0.87,
  confirmed_by_user_id: null,
  confirmed_at: null,
  dismissed_by_user_id: null,
  dismissed_at: null,
  created_opportunity_id: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
};

function getByPath(path: string, map: Record<string, unknown>): Promise<unknown> {
  for (const [key, value] of Object.entries(map)) {
    if (path.includes(key)) return Promise.resolve(value);
  }
  throw new Error(`Unexpected path: ${path}`);
}

function mockAllEmpty() {
  mockedApi.get.mockImplementation((path: string) =>
    getByPath(path, { actions: [], activity: [], attention: [], signals: [] }),
  );
}

describe("TodayView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
  });

  it("shows pending approvals and recent activity once loaded", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        actions: [action],
        activity: [activityEvent],
        attention: [],
        signals: [],
      }),
    );

    render(<TodayView />);

    expect(await screen.findByText("Send follow-up")).toBeInTheDocument();
    const activity = screen.getByRole("region", { name: "Recent activity" });
    expect(within(activity).getByText("Nerva proposed: Send follow-up")).toBeInTheDocument();
  });

  it("shows empty-state copy when there is nothing to approve or show", async () => {
    mockAllEmpty();

    render(<TodayView />);

    expect(await screen.findByText("No actions need your approval.")).toBeInTheDocument();
    expect(screen.getByText("Nothing has happened yet.")).toBeInTheDocument();
  });

  it("approving an action calls the API and reloads the list", async () => {
    mockedApi.get
      .mockResolvedValueOnce([action])
      .mockResolvedValueOnce([activityEvent])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([activityEvent])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([]);
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

  describe("Needs your attention", () => {
    it("shows conversations Nerva's analysis flagged as needing attention", async () => {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, { actions: [], activity: [], attention: [attentionItem], signals: [] }),
      );

      render(<TodayView />);

      const section = await screen.findByRole("region", { name: "Needs your attention" });
      expect(within(section).getByText("Server is down")).toBeInTheDocument();
      expect(within(section).getByText("Urgent")).toBeInTheDocument();
      expect(within(section).getByText("Needs response")).toBeInTheDocument();
      expect(
        within(section).getByText("Customer reports the server is unreachable."),
      ).toBeInTheDocument();
    });

    it("shows an empty state when nothing needs attention", async () => {
      mockAllEmpty();

      render(<TodayView />);

      expect(
        await screen.findByText("Nothing needs your attention right now."),
      ).toBeInTheDocument();
    });

    it("links each row to the corresponding Inbox conversation", async () => {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, { actions: [], activity: [], attention: [attentionItem], signals: [] }),
      );

      render(<TodayView />);

      const link = await screen.findByRole("link", { name: /Server is down/ });
      expect(link).toHaveAttribute("href", "/inbox?conversation=c1");
    });
  });

  describe("Potential opportunities", () => {
    it("shows a pending signal with its value and confidence", async () => {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, { actions: [], activity: [], attention: [], signals: [signal] }),
      );

      render(<TodayView />);

      const section = await screen.findByRole("region", { name: "Potential opportunities" });
      expect(within(section).getByText("Acme Corp")).toBeInTheDocument();
      expect(within(section).getByText("Interested in a 20-seat plan.")).toBeInTheDocument();
      expect(within(section).getByText(/\$4,000\.00/)).toBeInTheDocument();
      expect(within(section).getByText(/87%/)).toBeInTheDocument();
    });

    it("shows an empty state when there are no pending signals", async () => {
      mockAllEmpty();

      render(<TodayView />);

      expect(await screen.findByText("No potential opportunities right now.")).toBeInTheDocument();
    });

    it("confirming a signal calls the API and removes it from the queue", async () => {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, { actions: [], activity: [], attention: [], signals: [signal] }),
      );
      mockedApi.post.mockResolvedValue({
        signal: { ...signal, status: "confirmed" },
        opportunity: {},
      });

      render(<TodayView />);
      await userEvent.click(
        await screen.findByRole("button", { name: "Confirm opportunity: Acme Corp" }),
      );

      await waitFor(() =>
        expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/signals/s1/confirm"),
      );
      await waitFor(() => expect(screen.queryByText("Acme Corp")).not.toBeInTheDocument());
    });

    it("dismissing a signal calls the API and removes it from the queue", async () => {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, { actions: [], activity: [], attention: [], signals: [signal] }),
      );
      mockedApi.post.mockResolvedValue(undefined);

      render(<TodayView />);
      await userEvent.click(await screen.findByRole("button", { name: "Dismiss: Acme Corp" }));

      await waitFor(() =>
        expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/signals/s1/dismiss"),
      );
      await waitFor(() => expect(screen.queryByText("Acme Corp")).not.toBeInTheDocument());
    });
  });
});
