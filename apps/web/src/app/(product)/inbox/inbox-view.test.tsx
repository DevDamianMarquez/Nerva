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
} from "@/lib/types";

import { InboxView } from "./inbox-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = { id: "w1", name: "Acme", slug: "acme", created_at: "", role: "owner" as const };
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
