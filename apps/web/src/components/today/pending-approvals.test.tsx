import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { PendingApprovals } from "@/components/today/pending-approvals";
import type { Action } from "@/lib/types";

function makeAction(overrides: Partial<Action>): Action {
  return {
    id: "a1",
    workspace_id: "w1",
    opportunity_id: null,
    conversation_id: null,
    type: "send_email",
    status: "proposed",
    summary: "Untitled",
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
    ...overrides,
  };
}

const actions: Action[] = [
  makeAction({ id: "a1", summary: "Send follow-up to Priya Nair" }),
  makeAction({
    id: "a2",
    summary: "Move Ledgerly renewal to Proposal",
    type: "update_opportunity_stage",
  }),
];

describe("PendingApprovals", () => {
  it("lists each proposal with its type and who proposed it", () => {
    render(<PendingApprovals actions={actions} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(screen.getByText("Send follow-up to Priya Nair")).toBeInTheDocument();
    expect(screen.getByText(/Send email · Proposed by Nerva/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^Approve:/ })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: /^Reject:/ })).toHaveLength(2);
  });

  it("gives every button a name that says which proposal it acts on", () => {
    render(<PendingApprovals actions={actions} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(
      screen.getByRole("button", { name: "Approve: Send follow-up to Priya Nair" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Reject: Move Ledgerly renewal to Proposal" }),
    ).toBeInTheDocument();
  });

  it("calls onApprove with the action id when Approve is clicked", async () => {
    const onApprove = vi.fn().mockResolvedValue(undefined);
    render(<PendingApprovals actions={actions} onApprove={onApprove} onReject={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Approve: Send follow-up to Priya Nair" }),
    );

    expect(onApprove).toHaveBeenCalledWith("a1");
  });

  it("calls onReject with the action id when Reject is clicked", async () => {
    const onReject = vi.fn().mockResolvedValue(undefined);
    render(<PendingApprovals actions={actions} onApprove={vi.fn()} onReject={onReject} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Reject: Move Ledgerly renewal to Proposal" }),
    );

    expect(onReject).toHaveBeenCalledWith("a2");
  });

  it("shows an error and keeps the action listed if the decision fails", async () => {
    const onApprove = vi.fn().mockRejectedValue(new Error("network"));
    render(<PendingApprovals actions={[actions[0]]} onApprove={onApprove} onReject={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Approve: Send follow-up to Priya Nair" }),
    );

    expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Approve: Send follow-up to Priya Nair" }),
    ).toBeInTheDocument();
  });

  it("shows the no-actions message when there is nothing to approve", () => {
    render(<PendingApprovals actions={[]} onApprove={vi.fn()} onReject={vi.fn()} />);

    expect(screen.getByText("No actions need your approval.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("No approvals waiting.");
  });
});
