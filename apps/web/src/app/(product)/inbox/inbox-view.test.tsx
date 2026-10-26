import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api-client";
import type {
  Action,
  BusinessSignal,
  Conversation,
  ConversationAnalysis,
  ConversationDetail,
  Integration,
  MemberRole,
  ReasonConversationRead,
} from "@/lib/types";

import { InboxView } from "./inbox-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
// `role` is mutated (not reassigned) by individual tests to exercise owner/member/admin
// UX — reset in `beforeEach` so no test leaks its role into the next one.
const workspace: { id: string; name: string; slug: string; created_at: string; role: MemberRole } = {
  id: "w1",
  name: "Acme",
  slug: "acme",
  created_at: "",
  role: "owner",
};
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

// A stable reference (matching real Next.js): InboxView reads `?conversation=` to
// auto-open a conversation linked from Today's "Needs your attention".
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => searchParams,
}));

vi.mock("@/lib/api-client", () => {
  class MockApiError extends Error {
    status: number;
    code: string;
    details: unknown;
    constructor(status: number, code: string, message: string, details?: unknown) {
      super(message);
      this.status = status;
      this.code = code;
      this.details = details;
    }
  }
  return { api: { get: vi.fn(), post: vi.fn() }, ApiError: MockApiError };
});

const mockedApi = vi.mocked(api, true);

const connectedIntegration: Integration = {
  id: "i1",
  workspace_id: "w1",
  provider: "google",
  status: "connected",
  external_account_email: "alex@gmail.example",
  last_synced_at: "2024-01-01T00:00:00Z",
  last_error: null,
  created_at: "2024-01-01T00:00:00Z",
  can_send: true,
};

const conversation: Conversation = {
  id: "c1",
  workspace_id: "w1",
  contact_id: "contact1",
  integration_id: "i1",
  channel: "email",
  subject: "Pricing for 3 seats",
  last_message_at: "2024-01-02T10:00:00Z",
  preview: "Hi, can you send pricing for 3 seats?",
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-02T10:00:00Z",
};

const detail: ConversationDetail = {
  conversation,
  messages: [
    {
      id: "m1",
      conversation_id: "c1",
      direction: "inbound",
      sender_email: "priya@halcyon.example",
      sender_name: "Priya Nair",
      recipients: "alex@gmail.example",
      body_text: "Hi, can you send pricing for 3 seats?",
      sent_at: "2024-01-02T10:00:00Z",
    },
  ],
  analysis: null,
};

const completedAnalysis: ConversationAnalysis = {
  id: "an1",
  status: "completed",
  schema_version: 1,
  prompt_version: "v1",
  provider: "fake",
  model_id: "fake-model-1",
  attempts: 1,
  category: "sales_lead",
  priority: "high",
  sentiment: "positive",
  intent: "request_quote",
  summary: "Priya is asking for pricing on 3 seats.",
  requires_response: true,
  commercial_signal: true,
  confidence: 0.87,
  estimated_value_cents: 150_000,
  value_currency: "USD",
  value_is_explicit: true,
  deadline_at: "2026-06-05",
  deadline_is_explicit: false,
  suggested_next_step: "Send pricing for 3 seats.",
  mentioned_people: [],
  mentioned_companies: [],
  last_error: null,
  analyzed_at: "2024-01-02T11:00:00Z",
  updated_at: "2024-01-02T11:00:00Z",
};

const failedAnalysis: ConversationAnalysis = {
  ...completedAnalysis,
  status: "failed",
  category: null,
  priority: null,
  sentiment: null,
  intent: null,
  summary: null,
  requires_response: null,
  commercial_signal: null,
  confidence: null,
  estimated_value_cents: null,
  value_currency: null,
  value_is_explicit: null,
  deadline_at: null,
  deadline_is_explicit: null,
  suggested_next_step: null,
  last_error: "Could not reach the AI provider.",
  analyzed_at: null,
};

const minimalCompletedAnalysis: ConversationAnalysis = {
  ...completedAnalysis,
  sentiment: null,
  intent: null,
  suggested_next_step: null,
  estimated_value_cents: null,
  value_currency: null,
  value_is_explicit: null,
  deadline_at: null,
  deadline_is_explicit: null,
  confidence: null,
};

function getByPath(path: string, map: Record<string, unknown>): Promise<unknown> {
  for (const [key, value] of Object.entries(map)) {
    if (path.includes(key)) return Promise.resolve(value);
  }
  throw new Error(`Unexpected path: ${path}`);
}

describe("InboxView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
    searchParams = new URLSearchParams();
    workspace.role = "owner";
  });

  it("shows an empty state with a link to Settings when Gmail is not connected", async () => {
    mockedApi.get.mockResolvedValue([]);

    render(<InboxView />);

    expect(
      await screen.findByRole("heading", { name: "Connect Gmail to see conversations here" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Settings" })).toHaveAttribute(
      "href",
      "/settings",
    );
  });

  it("shows a different empty state when Gmail is connected but nothing has synced", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, { integrations: [connectedIntegration], conversations: [], signals: [] }),
    );

    render(<InboxView />);

    expect(
      await screen.findByRole("heading", { name: "No conversations yet" }),
    ).toBeInTheDocument();
  });

  it("lists synchronized conversations with a preview and time", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        integrations: [connectedIntegration],
        conversations: [conversation],
        signals: [],
      }),
    );

    render(<InboxView />);

    expect(await screen.findByText("Pricing for 3 seats")).toBeInTheDocument();
    expect(screen.getByText("Hi, can you send pricing for 3 seats?")).toBeInTheDocument();
  });

  it("loads and displays a conversation's messages in order when selected", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        "/conversations/c1": detail,
        "actions?conversation_id": [],
        integrations: [connectedIntegration],
        conversations: [conversation],
        signals: [],
      }),
    );

    render(<InboxView />);
    await userEvent.click(await screen.findByText("Pricing for 3 seats"));

    const detailPane = await screen.findByRole("region", { name: "Conversation" });
    expect(within(detailPane).getByText("Priya Nair")).toBeInTheDocument();
    expect(
      within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
    ).toBeInTheDocument();
  });

  it("renders message bodies as plain text, never as HTML", async () => {
    const htmlLookingBody = "<b>not actually bold</b>";
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        "/conversations/c1": {
          conversation,
          messages: [{ ...detail.messages[0], body_text: htmlLookingBody }],
          analysis: null,
        },
        "actions?conversation_id": [],
        integrations: [connectedIntegration],
        conversations: [conversation],
        signals: [],
      }),
    );

    render(<InboxView />);
    await userEvent.click(await screen.findByText("Pricing for 3 seats"));

    expect(await screen.findByText(htmlLookingBody)).toBeInTheDocument();
    expect(document.querySelector("b")).not.toBeInTheDocument();
  });

  it("offers a retry option when loading fails", async () => {
    mockedApi.get.mockRejectedValue(new Error("network"));

    render(<InboxView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("marks the current user's own sent messages as 'You'", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        "/conversations/c1": {
          conversation,
          messages: [
            {
              id: "m2",
              conversation_id: "c1",
              direction: "outbound",
              sender_email: "alex@gmail.example",
              sender_name: "Alex Morgan",
              recipients: "priya@halcyon.example",
              body_text: "Sure, here it is.",
              sent_at: "2024-01-02T11:00:00Z",
            },
          ],
          analysis: null,
        },
        "actions?conversation_id": [],
        integrations: [connectedIntegration],
        conversations: [conversation],
        signals: [],
      }),
    );

    render(<InboxView />);
    await userEvent.click(await screen.findByText("Pricing for 3 seats"));

    await waitFor(() => expect(screen.getByText("You")).toBeInTheDocument());
  });

  describe("Nerva Understanding", () => {
    function mockLoaded(detailOverride: ConversationDetail, signals: BusinessSignal[] = []) {
      mockedApi.get.mockImplementation((path: string) =>
        getByPath(path, {
          "/conversations/c1": detailOverride,
          "actions?conversation_id": [],
          integrations: [connectedIntegration],
          conversations: [conversation],
          signals,
        }),
      );
    }

    it("shows a not-analyzed state with an Analyze action when there is no analysis", async () => {
      mockLoaded(detail);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findByText("Not analyzed yet.")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Analyze this conversation with Nerva" }),
      ).toBeInTheDocument();
    });

    it("shows structured understanding for a completed analysis", async () => {
      mockLoaded({ ...detail, analysis: completedAnalysis });

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findByText("Sales lead")).toBeInTheDocument();
      expect(screen.getByText("High priority")).toBeInTheDocument();
      expect(screen.getByText("Needs response")).toBeInTheDocument();
      expect(screen.getByText(completedAnalysis.summary!)).toBeInTheDocument();
      expect(screen.getByText("Send pricing for 3 seats.")).toBeInTheDocument();
      expect(screen.getByText("$1,500.00")).toBeInTheDocument();
      expect(screen.getByText("87% confidence")).toBeInTheDocument();
    });

    it("does not break when optional analysis fields are null", async () => {
      mockLoaded({ ...detail, analysis: minimalCompletedAnalysis });

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findByText("Sales lead")).toBeInTheDocument();
      expect(screen.queryByText("Suggested next step")).not.toBeInTheDocument();
      expect(screen.queryByText("Potential value")).not.toBeInTheDocument();
      expect(screen.queryByText("Deadline")).not.toBeInTheDocument();
    });

    it("shows a safe failure message for a failed analysis", async () => {
      mockLoaded({ ...detail, analysis: failedAnalysis });

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findByText("Could not reach the AI provider.")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Re-analyze this conversation with Nerva" }),
      ).toBeInTheDocument();
    });

    it("analyzes a conversation and shows the result without a reload", async () => {
      mockLoaded(detail);
      mockedApi.post.mockResolvedValue(completedAnalysis);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const analyzeButton = await screen.findByRole("button", {
        name: "Analyze this conversation with Nerva",
      });
      await userEvent.click(analyzeButton);

      expect(await screen.findByText(completedAnalysis.summary!)).toBeInTheDocument();
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/conversations/c1/analyze");
    });

    it("disables the analyze button while a request is in flight", async () => {
      mockLoaded(detail);
      let resolveAnalyze: (value: ConversationAnalysis) => void = () => {};
      mockedApi.post.mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveAnalyze = resolve;
          }),
      );

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const analyzeButton = await screen.findByRole("button", {
        name: "Analyze this conversation with Nerva",
      });
      await userEvent.click(analyzeButton);

      expect(await screen.findByText("Analyzing…")).toBeInTheDocument();
      expect(analyzeButton).toBeDisabled();
      await userEvent.click(analyzeButton); // a second click while in flight must not re-trigger
      expect(mockedApi.post).toHaveBeenCalledTimes(1);

      resolveAnalyze(completedAnalysis);
      await waitFor(() => expect(screen.getByText(completedAnalysis.summary!)).toBeInTheDocument());
    });

    it("shows a safe error message when analysis fails, without a raw exception string", async () => {
      mockLoaded(detail);
      mockedApi.post.mockRejectedValue(
        new ApiError(503, "service_unavailable", "AI analysis is not available right now.", {
          code: "ai_not_configured",
        }),
      );

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Analyze this conversation with Nerva" }),
      );

      expect(
        await screen.findByText("AI analysis is not available right now."),
      ).toBeInTheDocument();
      expect(screen.queryByText(/Traceback|Error:/)).not.toBeInTheDocument();
    });

    it("shows a contextual potential-opportunity card for the open conversation's pending signal", async () => {
      const signal: BusinessSignal = {
        id: "s1",
        conversation_id: "c1",
        analysis_id: "an1",
        type: "potential_opportunity",
        status: "pending",
        source_fingerprint: "fp1",
        title: "Pricing for 3 seats",
        summary: "Asking about pricing for 3 seats.",
        estimated_value_cents: 150_000,
        currency: "USD",
        value_is_explicit: true,
        confidence: 0.87,
        confirmed_by_user_id: null,
        confirmed_at: null,
        dismissed_by_user_id: null,
        dismissed_at: null,
        created_opportunity_id: null,
        created_at: "2024-01-02T11:00:00Z",
        updated_at: "2024-01-02T11:00:00Z",
      };
      mockLoaded({ ...detail, analysis: completedAnalysis }, [signal]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(
        await screen.findByRole("button", { name: "Confirm opportunity: Pricing for 3 seats" }),
      ).toBeInTheDocument();
    });

    it("confirming the contextual signal removes its decision card", async () => {
      const signal: BusinessSignal = {
        id: "s1",
        conversation_id: "c1",
        analysis_id: "an1",
        type: "potential_opportunity",
        status: "pending",
        source_fingerprint: "fp1",
        title: "Pricing for 3 seats",
        summary: null,
        estimated_value_cents: null,
        currency: null,
        value_is_explicit: null,
        confidence: null,
        confirmed_by_user_id: null,
        confirmed_at: null,
        dismissed_by_user_id: null,
        dismissed_at: null,
        created_opportunity_id: null,
        created_at: "2024-01-02T11:00:00Z",
        updated_at: "2024-01-02T11:00:00Z",
      };
      mockLoaded({ ...detail, analysis: completedAnalysis }, [signal]);
      mockedApi.post.mockResolvedValue({
        signal: { ...signal, status: "confirmed" },
        opportunity: {},
      });

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Confirm opportunity: Pricing for 3 seats" }),
      );

      await waitFor(() =>
        expect(
          screen.queryByRole("button", { name: "Confirm opportunity: Pricing for 3 seats" }),
        ).not.toBeInTheDocument(),
      );
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/signals/s1/confirm");
    });
  });

  describe("Suggested actions", () => {
    function makeAction(overrides: Partial<Action> = {}): Action {
      return {
        id: "act1",
        workspace_id: "w1",
        opportunity_id: null,
        conversation_id: "c1",
        type: "send_email",
        status: "proposed",
        summary: "Reply to Priya with pricing",
        payload: {
          recipients: ["priya@halcyon.example"],
          subject: "Re: Pricing for 3 seats",
          body: "Hi Priya,\nHere is the pricing you asked about.",
        },
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
        created_at: "2024-01-02T11:05:00Z",
        updated_at: "2024-01-02T11:05:00Z",
        ...overrides,
      };
    }

    function mockLoaded(actions: Action[] | (() => Promise<Action[]>)) {
      mockedApi.get.mockImplementation((path: string) => {
        if (path.includes("actions?conversation_id")) {
          return typeof actions === "function" ? actions() : Promise.resolve(actions);
        }
        return getByPath(path, {
          "/conversations/c1": detail,
          integrations: [connectedIntegration],
          conversations: [conversation],
          signals: [],
        });
      });
    }

    it("fetches Actions scoped to the open conversation", async () => {
      mockLoaded([makeAction()]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      await screen.findByText("Suggested action");
      expect(mockedApi.get).toHaveBeenCalledWith(
        "/v1/workspaces/w1/actions?conversation_id=c1",
      );
    });

    it("a conversation with no Actions remains fully usable", async () => {
      mockLoaded([]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const detailPane = await screen.findByRole("region", { name: "Conversation" });

      // Messages still render — no scary error for the common "nothing proposed" case.
      await waitFor(() =>
        expect(
          within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
        ).toBeInTheDocument(),
      );
      expect(screen.queryByText("Suggested action")).not.toBeInTheDocument();
      expect(screen.queryByText(/Couldn.t load this/)).not.toBeInTheDocument();
    });

    it("shows a loading state for Actions without blocking the rest of the page", async () => {
      mockLoaded(() => new Promise<Action[]>(() => {})); // never resolves

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const detailPane = await screen.findByRole("region", { name: "Conversation" });

      await waitFor(() =>
        expect(
          within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
        ).toBeInTheDocument(),
      );
      expect(await screen.findByText("Loading…")).toBeInTheDocument();
    });

    it("a failed Actions request shows a local retry, and never hides messages/analysis", async () => {
      mockLoaded(() => Promise.reject(new Error("network")));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const detailPane = await screen.findByRole("region", { name: "Conversation" });

      expect(
        await within(detailPane).findByText("Hi, can you send pricing for 3 seats?"),
      ).toBeInTheDocument();
      expect(screen.getByText(/Couldn.t load this/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    });

    it("renders a proposed send_email with recipient, subject, body and both decisions", async () => {
      mockLoaded([makeAction()]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findByText("priya@halcyon.example")).toBeInTheDocument();
      expect(screen.getByText("Re: Pricing for 3 seats")).toBeInTheDocument();
      expect(
        screen.getByText(/Here is the pricing you asked about\./),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Reject: Reply to Priya with pricing" }),
      ).toBeInTheDocument();
    });

    it("renders every Action when the conversation has more than one", async () => {
      mockLoaded([
        makeAction({ id: "act1", summary: "First proposal", status: "proposed" }),
        makeAction({ id: "act2", summary: "Second proposal", status: "approved" }),
      ]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));

      expect(await screen.findAllByText("Suggested action")).toHaveLength(2);
      expect(
        screen.getByRole("button", { name: "Approve: First proposal" }),
      ).toBeInTheDocument();
      expect(screen.getByText("Approved")).toBeInTheDocument(); // the second, already decided
      // The already-decided proposal keeps no decision buttons for itself.
      expect(
        screen.queryByRole("button", { name: /Approve: Second proposal/ }),
      ).not.toBeInTheDocument();
    });

    it("approving calls the backend's approve endpoint and reflects the returned state", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockResolvedValue(makeAction({ status: "approved" }));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      );

      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/actions/act1/approve");
      expect(await screen.findByText("Approved")).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      ).not.toBeInTheDocument();
      // The proposal's own content remains visible after the decision.
      expect(screen.getByText("Re: Pricing for 3 seats")).toBeInTheDocument();
    });

    it("rejecting calls the backend's reject endpoint and reflects the returned state", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockResolvedValue(makeAction({ status: "rejected" }));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Reject: Reply to Priya with pricing" }),
      );

      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/actions/act1/reject");
      expect(await screen.findByText("Rejected")).toBeInTheDocument();
    });

    it("a failed approval keeps the previous proposal visible with its buttons re-enabled", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockRejectedValue(new Error("network"));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      );

      expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      ).toBeEnabled();
      expect(screen.getByText("Re: Pricing for 3 seats")).toBeInTheDocument();
    });

    it("a failed rejection keeps the previous proposal visible", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockRejectedValue(new Error("network"));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Reject: Reply to Priya with pricing" }),
      );

      expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
      expect(screen.getByText("Re: Pricing for 3 seats")).toBeInTheDocument();
    });

    it("disables both buttons while a decision request is in flight", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockImplementation(() => new Promise(() => {})); // never resolves

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const approveButton = await screen.findByRole("button", {
        name: "Approve: Reply to Priya with pricing",
      });
      await userEvent.click(approveButton);

      await waitFor(() => expect(approveButton).toBeDisabled());
      expect(
        screen.getByRole("button", { name: "Reject: Reply to Priya with pricing" }),
      ).toBeDisabled();
    });

    it("never calls execute or reason when approving or rejecting", async () => {
      mockLoaded([makeAction()]);
      mockedApi.post.mockResolvedValue(makeAction({ status: "approved" }));

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      await userEvent.click(
        await screen.findByRole("button", { name: "Approve: Reply to Priya with pricing" }),
      );
      await waitFor(() => expect(screen.getByText("Approved")).toBeInTheDocument());

      for (const [path] of mockedApi.post.mock.calls) {
        expect(path).not.toContain("/execute");
        expect(path).not.toContain("/reason");
      }
    });

    it("an unknown Action type does not crash the conversation page", async () => {
      mockLoaded([
        makeAction({
          type: "update_opportunity_stage",
          payload: { target_stage: "qualified" },
        }),
      ]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const detailPane = await screen.findByRole("region", { name: "Conversation" });

      expect(await screen.findByText("Suggested action")).toBeInTheDocument();
      expect(
        within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
      ).toBeInTheDocument();
    });

    it("a malformed send_email payload does not crash the conversation page", async () => {
      mockLoaded([makeAction({ payload: { subject: 123, body: null } })]);

      render(<InboxView />);
      await userEvent.click(await screen.findByText("Pricing for 3 seats"));
      const detailPane = await screen.findByRole("region", { name: "Conversation" });

      expect(await screen.findByText("Suggested action")).toBeInTheDocument();
      expect(
        within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
      ).toBeInTheDocument();
    });

    // -------------------------------------------------------------------
    // M8.3b: Suggest reply (reasoning)
    // -------------------------------------------------------------------

    describe("Suggest reply", () => {
      function reasonResult(overrides: Partial<ReasonConversationRead> = {}): ReasonConversationRead {
        return { outcome: "action_created", action: makeAction(), detail: null, ...overrides };
      }

      it("is visible as soon as a conversation is open, even with no Actions yet", async () => {
        mockLoaded([]);
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        expect(await screen.findByRole("button", { name: "Suggest reply" })).toBeInTheDocument();
      });

      it("calls exactly the /reason endpoint with no body", async () => {
        mockLoaded([]);
        mockedApi.post.mockResolvedValue(reasonResult({ outcome: "no_action", action: null }));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/conversations/c1/reason");
        expect(mockedApi.post).toHaveBeenCalledTimes(1);
      });

      it("disables the button and shows Thinking… while pending", async () => {
        mockLoaded([]);
        mockedApi.post.mockImplementation(() => new Promise(() => {}));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        const button = await screen.findByRole("button", { name: "Suggest reply" });

        await userEvent.click(button);

        await waitFor(() => expect(button).toHaveTextContent("Thinking…"));
        expect(button).toBeDisabled();
      });

      it("never issues a second reason request while one is already pending", async () => {
        mockLoaded([]);
        mockedApi.post.mockImplementation(() => new Promise(() => {}));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        const button = await screen.findByRole("button", { name: "Suggest reply" });

        await userEvent.click(button);
        await userEvent.click(button);

        expect(mockedApi.post).toHaveBeenCalledTimes(1);
      });

      it("ACTION_CREATED renders the returned proposal immediately, with no refetch", async () => {
        mockLoaded([]);
        mockedApi.post.mockResolvedValue(reasonResult());
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        mockedApi.get.mockClear();

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(await screen.findByText("Suggested action")).toBeInTheDocument();
        expect(screen.getByText("priya@halcyon.example")).toBeInTheDocument();
        expect(mockedApi.get).not.toHaveBeenCalled();
      });

      it("ACTION_DUPLICATE upserts the returned Action without duplicating the card", async () => {
        mockLoaded([makeAction()]);
        mockedApi.post.mockResolvedValue(
          reasonResult({ outcome: "action_duplicate", action: makeAction() }),
        );
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        expect(await screen.findAllByText("Suggested action")).toHaveLength(1);

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        await waitFor(() => expect(mockedApi.post).toHaveBeenCalledTimes(1));
        expect(screen.getAllByText("Suggested action")).toHaveLength(1);
      });

      it("NO_ACTION shows safe inline feedback and does not create an Action", async () => {
        mockLoaded([]);
        mockedApi.post.mockResolvedValue(reasonResult({ outcome: "no_action", action: null }));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(
          await screen.findByText("Nerva doesn’t recommend an action right now."),
        ).toBeInTheDocument();
        expect(screen.queryByText("Suggested action")).not.toBeInTheDocument();
      });

      it("POLICY_REJECTED shows safe inline feedback and does not create an Action", async () => {
        mockLoaded([]);
        mockedApi.post.mockResolvedValue(reasonResult({ outcome: "policy_rejected", action: null }));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(
          await screen.findByText("Nerva couldn’t create a safe suggestion."),
        ).toBeInTheDocument();
        expect(screen.queryByText("Suggested action")).not.toBeInTheDocument();
      });

      it.each([
        [409, "analysis_not_ready", "Analyze this conversation first, then try Suggest reply."],
        [
          409,
          "recipient_not_resolvable",
          "Nerva couldn’t find a trusted recipient for this conversation.",
        ],
        [429, "rate_limited", "Nerva is busy right now — try again in a moment."],
        [503, "ai_not_configured", "Reasoning isn’t available right now."],
        [503, "ai_unavailable", "Couldn’t reach the AI provider — try again shortly."],
        [503, "invalid_ai_output", "Nerva couldn’t produce a suggestion this time — try again."],
      ])("maps %s %s to a safe message", async (status, code, message) => {
        mockLoaded([]);
        mockedApi.post.mockRejectedValue(
          new ApiError(status, code, "backend technical message", { code }),
        );
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(await screen.findByText(message)).toBeInTheDocument();
      });

      it("a network failure shows a safe fallback and preserves existing Actions", async () => {
        mockLoaded([makeAction()]);
        mockedApi.post.mockRejectedValue(new Error("network"));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        await screen.findByText("Suggested action");

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        expect(await screen.findByText("Couldn’t get a suggestion. Try again.")).toBeInTheDocument();
        expect(screen.getByText("Suggested action")).toBeInTheDocument(); // untouched
      });

      it("clears previous feedback when a new Suggest reply attempt starts", async () => {
        mockLoaded([]);
        mockedApi.post
          .mockResolvedValueOnce(reasonResult({ outcome: "no_action", action: null }))
          .mockImplementationOnce(() => new Promise(() => {}));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        const button = await screen.findByRole("button", { name: "Suggest reply" });

        await userEvent.click(button);
        expect(
          await screen.findByText("Nerva doesn’t recommend an action right now."),
        ).toBeInTheDocument();

        await userEvent.click(button);

        await waitFor(() =>
          expect(
            screen.queryByText("Nerva doesn’t recommend an action right now."),
          ).not.toBeInTheDocument(),
        );
      });

      it("does not leak feedback across a conversation change", async () => {
        const conversation2: Conversation = {
          ...conversation,
          id: "c2",
          subject: "Second thread",
          preview: "Hey there",
        };
        mockedApi.get.mockImplementation((path: string) => {
          if (path.includes("actions?conversation_id")) return Promise.resolve([]);
          return getByPath(path, {
            "/conversations/c2": { conversation: conversation2, messages: [], analysis: null },
            "/conversations/c1": detail,
            integrations: [connectedIntegration],
            conversations: [conversation, conversation2],
            signals: [],
          });
        });
        mockedApi.post.mockResolvedValue(reasonResult({ outcome: "no_action", action: null }));

        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));
        expect(
          await screen.findByText("Nerva doesn’t recommend an action right now."),
        ).toBeInTheDocument();

        await userEvent.click(await screen.findByText("Second thread"));

        await waitFor(() =>
          expect(
            screen.queryByText("Nerva doesn’t recommend an action right now."),
          ).not.toBeInTheDocument(),
        );
      });

      it("never calls approve, reject or execute", async () => {
        mockLoaded([]);
        mockedApi.post.mockResolvedValue(reasonResult());
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(await screen.findByRole("button", { name: "Suggest reply" }));

        await waitFor(() => expect(mockedApi.post).toHaveBeenCalled());
        for (const [path] of mockedApi.post.mock.calls) {
          expect(path).toContain("/reason");
        }
      });
    });

    // -------------------------------------------------------------------
    // M8.3b: Send email (execution)
    // -------------------------------------------------------------------

    describe("Send email", () => {
      it("APPROVED send_email + owner shows Send email", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        expect(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        ).toBeInTheDocument();
      });

      it("APPROVED send_email + member does not show Send email", async () => {
        workspace.role = "member";
        mockLoaded([makeAction({ status: "approved" })]);
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await screen.findByText("Approved");
        expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      });

      it("APPROVED send_email + admin does not show Send email (backend requires owner)", async () => {
        workspace.role = "admin";
        mockLoaded([makeAction({ status: "approved" })]);
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await screen.findByText("Approved");
        expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      });

      it("Send calls exactly the /execute endpoint with no body", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockResolvedValue(makeAction({ status: "completed" }));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/actions/act1/execute");
        expect(mockedApi.post).toHaveBeenCalledTimes(1);
        expect(mockedApi.post.mock.calls[0]).toHaveLength(1); // path only — no body argument
      });

      it("disables Send and shows Sending… while pending", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockImplementation(() => new Promise(() => {}));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        const button = await screen.findByRole("button", {
          name: "Send email: Reply to Priya with pricing",
        });

        await userEvent.click(button);

        await waitFor(() => expect(button).toHaveTextContent("Sending…"));
        expect(button).toBeDisabled();
      });

      it("a returned COMPLETED renders Sent, with no Send button", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockResolvedValue(
          makeAction({ status: "completed", result: { external_reference_id: "msg_1" } }),
        );
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        expect(await screen.findByText("Sent")).toBeInTheDocument();
        expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      });

      it("a returned FAILED renders Failed to send, the safe error, and Go to Settings", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockResolvedValue(
          makeAction({
            status: "failed",
            result: {
              error:
                "Gmail is connected without permission to send email; it needs to be reconnected.",
            },
          }),
        );
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        expect(await screen.findByText("Failed to send")).toBeInTheDocument();
        expect(
          screen.getByText(
            "Gmail is connected without permission to send email; it needs to be reconnected.",
          ),
        ).toBeInTheDocument();
        expect(screen.getByRole("link", { name: "Go to Settings" })).toHaveAttribute(
          "href",
          "/settings",
        );
        expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      });

      it("an execute network failure preserves the prior APPROVED state and re-enables Send", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockRejectedValue(new Error("network"));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        expect(await screen.findByText(/Couldn.t send this/)).toBeInTheDocument();
        expect(
          screen.getByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        ).toBeEnabled();
      });

      it("a 409 execute conflict refetches the authoritative Actions list and never auto-retries execute", async () => {
        let getCalls = 0;
        mockedApi.get.mockImplementation((path: string) => {
          if (path.includes("actions?conversation_id")) {
            getCalls += 1;
            return Promise.resolve(
              getCalls === 1
                ? [makeAction({ status: "approved" })]
                : [makeAction({ status: "rejected" })], // someone else decided it meanwhile
            );
          }
          return getByPath(path, {
            "/conversations/c1": detail,
            integrations: [connectedIntegration],
            conversations: [conversation],
            signals: [],
          });
        });
        mockedApi.post.mockRejectedValue(new ApiError(409, "not_executable", "not executable"));

        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        expect(
          await screen.findByText("This proposal is no longer ready to send."),
        ).toBeInTheDocument();
        await waitFor(() => expect(screen.getByText("Rejected")).toBeInTheDocument());
        expect(getCalls).toBe(2); // the initial load, plus exactly one 409-triggered refetch
        expect(mockedApi.post).toHaveBeenCalledTimes(1); // execute was never retried automatically
      });

      it("sending one Action does not disable or remove an unrelated Action", async () => {
        mockLoaded([
          makeAction({ id: "act1", summary: "First proposal", status: "approved" }),
          makeAction({ id: "act2", summary: "Second proposal", status: "proposed" }),
        ]);
        mockedApi.post.mockImplementation(() => new Promise(() => {})); // first Send stays pending

        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));
        await userEvent.click(await screen.findByRole("button", { name: "Send email: First proposal" }));

        expect(
          await screen.findByRole("button", { name: "Approve: Second proposal" }),
        ).toBeEnabled();
        expect(screen.getByRole("button", { name: "Reject: Second proposal" })).toBeEnabled();
      });

      it("execute never calls /reason, /approve or /reject", async () => {
        mockLoaded([makeAction({ status: "approved" })]);
        mockedApi.post.mockResolvedValue(makeAction({ status: "completed" }));
        render(<InboxView />);
        await userEvent.click(await screen.findByText("Pricing for 3 seats"));

        await userEvent.click(
          await screen.findByRole("button", { name: "Send email: Reply to Priya with pricing" }),
        );

        await waitFor(() => expect(screen.getByText("Sent")).toBeInTheDocument());
        for (const [path] of mockedApi.post.mock.calls) {
          expect(path).toContain("/execute");
        }
      });
    });
  });

  it("auto-opens the conversation named by ?conversation= (from Today's attention list)", async () => {
    searchParams = new URLSearchParams("conversation=c1");
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        "/conversations/c1": detail,
        "actions?conversation_id": [],
        integrations: [connectedIntegration],
        conversations: [conversation],
        signals: [],
      }),
    );

    render(<InboxView />);

    const detailPane = await screen.findByRole("region", { name: "Conversation" });
    expect(
      within(detailPane).getByText("Hi, can you send pricing for 3 seats?"),
    ).toBeInTheDocument();
  });
});
