import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api-client";
import type { Opportunity } from "@/lib/types";

import { OpportunitiesView } from "./opportunities-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = { id: "w1", name: "Acme", slug: "acme", created_at: "", role: "owner" as const };
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

vi.mock("@/lib/api-client", () => {
  class MockApiError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { api: { get: vi.fn(), post: vi.fn() }, ApiError: MockApiError };
});

const mockedApi = vi.mocked(api, true);

const opportunity: Opportunity = {
  id: "o1",
  workspace_id: "w1",
  contact_id: null,
  company_id: null,
  conversation_id: null,
  title: "Halcyon Freight: 3 seats",
  summary: null,
  stage: "qualified",
  estimated_value_cents: 180_000,
  currency: "USD",
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
};

describe("OpportunitiesView", () => {
  beforeAll(() => {
    // Radix's Sheet portal needs scrollIntoView / PointerEvent capture helpers jsdom lacks.
    Element.prototype.scrollIntoView = () => {};
    Element.prototype.hasPointerCapture = () => false;
    Element.prototype.releasePointerCapture = () => {};
  });

  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
  });

  it("shows the empty state and stage counts when there are no opportunities", async () => {
    mockedApi.get.mockResolvedValue([]);

    render(<OpportunitiesView />);

    expect(
      await screen.findByRole("heading", { name: "No opportunities yet" }),
    ).toBeInTheDocument();
    for (const stage of ["New", "Qualified", "Proposal", "Won", "Lost"]) {
      expect(screen.getByText(stage).nextElementSibling).toHaveTextContent("0");
    }
  });

  it("lists real opportunities once loaded", async () => {
    mockedApi.get.mockResolvedValue([opportunity]);

    render(<OpportunitiesView />);

    expect(await screen.findByText("Halcyon Freight: 3 seats")).toBeInTheDocument();
    const table = within(screen.getByRole("table"));
    expect(table.getByText("Qualified")).toBeInTheDocument();
    expect(table.getByText("$1,800.00")).toBeInTheDocument();
  });

  it("creates a new opportunity from the New opportunity panel", async () => {
    mockedApi.get.mockResolvedValueOnce([]).mockResolvedValueOnce([opportunity]);
    mockedApi.post.mockResolvedValue(opportunity);

    render(<OpportunitiesView />);
    await screen.findByRole("heading", { name: "No opportunities yet" });

    await userEvent.click(screen.getByRole("button", { name: "New opportunity" }));
    await userEvent.type(screen.getByLabelText("Title"), "Halcyon Freight: 3 seats");
    await userEvent.click(screen.getByRole("button", { name: "Create opportunity" }));

    expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/opportunities", {
      title: "Halcyon Freight: 3 seats",
    });
    expect(await screen.findByText("Halcyon Freight: 3 seats")).toBeInTheDocument();
  });

  it("offers a retry option when loading fails", async () => {
    mockedApi.get.mockRejectedValue(new ApiError(500, "internal_error", "boom"));

    render(<OpportunitiesView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
