import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api-client";
import type { Conversation, ConversationDetail, Integration } from "@/lib/types";

import { InboxView } from "./inbox-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = { id: "w1", name: "Acme", slug: "acme", created_at: "", role: "owner" as const };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn() },
}));

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
      getByPath(path, { integrations: [connectedIntegration], conversations: [] }),
    );

    render(<InboxView />);

    expect(
      await screen.findByRole("heading", { name: "No conversations yet" }),
    ).toBeInTheDocument();
  });

  it("lists synchronized conversations with a preview and time", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, { integrations: [connectedIntegration], conversations: [conversation] }),
    );

    render(<InboxView />);

    expect(await screen.findByText("Pricing for 3 seats")).toBeInTheDocument();
    expect(screen.getByText("Hi, can you send pricing for 3 seats?")).toBeInTheDocument();
  });

  it("loads and displays a conversation's messages in order when selected", async () => {
    mockedApi.get.mockImplementation((path: string) =>
      getByPath(path, {
        "/conversations/c1": detail,
        integrations: [connectedIntegration],
        conversations: [conversation],
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
        },
        integrations: [connectedIntegration],
        conversations: [conversation],
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
        },
        integrations: [connectedIntegration],
        conversations: [conversation],
      }),
    );

    render(<InboxView />);
    await userEvent.click(await screen.findByText("Pricing for 3 seats"));

    await waitFor(() => expect(screen.getByText("You")).toBeInTheDocument());
  });
});
