import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { Action } from "@/lib/types";

import { SuggestedAction } from "./suggested-action";

function makeAction(overrides: Partial<Action> = {}): Action {
  return {
    id: "a1",
    workspace_id: "w1",
    opportunity_id: null,
    conversation_id: "c1",
    type: "send_email",
    status: "proposed",
    summary: "Reply to Alex about pricing",
    payload: {
      recipients: ["alex@example.com"],
      subject: "Re: Pro plan pricing",
      body: "Hi Alex,\nHere is the pricing you asked about.",
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
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    ...overrides,
  };
}

describe("SuggestedAction", () => {
  describe("a proposed send_email", () => {
    it("renders the recipient, subject, body, and both decision buttons", () => {
      render(<SuggestedAction action={makeAction()} onApprove={vi.fn()} onReject={vi.fn()} />);

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
      expect(screen.getByText("alex@example.com")).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
      const body = screen.getByText(/Here is the pricing you asked about\./);
      expect(body).toBeInTheDocument();
      // The raw text content (not testing-library's whitespace-normalized
      // match) still carries the line break, and `whitespace-pre-wrap` is
      // what makes it render as one visually — see the long-content test too.
      expect(body.textContent).toBe("Hi Alex,\nHere is the pricing you asked about.");
      expect(body.className).toContain("whitespace-pre-wrap");
      expect(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      ).toBeInTheDocument();
    });

    it("joins multiple recipients for display", () => {
      render(
        <SuggestedAction
          action={makeAction({ payload: { ...makeAction().payload, recipients: ["a@x.com", "b@x.com"] } })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("a@x.com, b@x.com")).toBeInTheDocument();
    });

    it("calls onApprove with the action when Approve is clicked", async () => {
      const onApprove = vi.fn().mockResolvedValue(undefined);
      render(<SuggestedAction action={makeAction()} onApprove={onApprove} onReject={vi.fn()} />);

      await userEvent.click(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      );

      expect(onApprove).toHaveBeenCalledWith(makeAction());
    });

    it("calls onReject with the action when Reject is clicked", async () => {
      const onReject = vi.fn().mockResolvedValue(undefined);
      render(<SuggestedAction action={makeAction()} onApprove={vi.fn()} onReject={onReject} />);

      await userEvent.click(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      );

      expect(onReject).toHaveBeenCalledWith(makeAction());
    });

    it("disables both buttons while a decision is in flight", async () => {
      const onApprove = vi.fn(() => new Promise<void>(() => {})); // never resolves
      render(<SuggestedAction action={makeAction()} onApprove={onApprove} onReject={vi.fn()} />);

      const approveButton = screen.getByRole("button", {
        name: "Approve: Reply to Alex about pricing",
      });
      await userEvent.click(approveButton);

      await waitFor(() => expect(approveButton).toBeDisabled());
      expect(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      ).toBeDisabled();
    });

    it("never issues a second approve request while one is already pending", async () => {
      const onApprove = vi.fn(() => new Promise<void>(() => {}));
      render(<SuggestedAction action={makeAction()} onApprove={onApprove} onReject={vi.fn()} />);

      const approveButton = screen.getByRole("button", {
        name: "Approve: Reply to Alex about pricing",
      });
      await userEvent.click(approveButton);
      await userEvent.click(approveButton);

      expect(onApprove).toHaveBeenCalledTimes(1);
    });

    it("shows a safe error and keeps the proposal visible when approval fails", async () => {
      const onApprove = vi.fn().mockRejectedValue(new Error("network"));
      render(<SuggestedAction action={makeAction()} onApprove={onApprove} onReject={vi.fn()} />);

      await userEvent.click(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      );

      expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      ).toBeEnabled();
    });

    it("shows a safe error and keeps the proposal visible when rejection fails", async () => {
      const onReject = vi.fn().mockRejectedValue(new Error("network"));
      render(<SuggestedAction action={makeAction()} onApprove={vi.fn()} onReject={onReject} />);

      await userEvent.click(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      );

      expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("renders the body as plain text, never as HTML", () => {
      const htmlLookingBody = "<b>not actually bold</b>";
      render(
        <SuggestedAction
          action={makeAction({ payload: { ...makeAction().payload, body: htmlLookingBody } })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText(htmlLookingBody)).toBeInTheDocument();
      expect(document.querySelector("b")).not.toBeInTheDocument();
    });
  });

  describe("lifecycle status", () => {
    it("shows Approved and no decision buttons once approved", () => {
      render(
        <SuggestedAction
          action={makeAction({ status: "approved" })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Approved")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      // The proposal itself stays visible after the decision.
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("shows Rejected and no decision buttons once rejected", () => {
      render(
        <SuggestedAction
          action={makeAction({ status: "rejected" })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Rejected")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("shows Executing with no mutation buttons", () => {
      render(
        <SuggestedAction
          action={makeAction({ status: "executing" })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Executing")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });

    it("shows Completed with no mutation buttons", () => {
      render(
        <SuggestedAction
          action={makeAction({ status: "completed" })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Completed")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });

    it("shows Failed safely with no mutation buttons", () => {
      render(
        <SuggestedAction
          action={makeAction({ status: "failed" })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Failed")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });
  });

  describe("unknown or malformed data", () => {
    it("does not crash for an unknown action type", () => {
      render(
        <SuggestedAction
          action={makeAction({ type: "update_opportunity_stage", payload: { target_stage: "qualified" } })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
      expect(screen.getByText("Reply to Alex about pricing")).toBeInTheDocument();
    });

    it("does not crash and shows a safe fallback for a malformed send_email payload", () => {
      render(
        <SuggestedAction
          action={makeAction({ payload: { subject: 42, body: null } })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
      expect(screen.getByText(/couldn.t be displayed/)).toBeInTheDocument();
    });

    it("does not crash when the payload is entirely empty", () => {
      render(<SuggestedAction action={makeAction({ payload: {} })} onApprove={vi.fn()} onReject={vi.fn()} />);

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
    });

    it("still offers Approve/Reject for a malformed payload while still proposed", () => {
      render(
        <SuggestedAction
          action={makeAction({ payload: { recipients: "not-an-array" } })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      ).toBeInTheDocument();
    });
  });

  describe("long content", () => {
    it("renders a long recipient, subject and body without crashing", () => {
      const longRecipient = `${"a".repeat(80)}@example.com`;
      const longSubject = "Re: ".repeat(40).trim();
      const longBody = "This is a very long line of text.".repeat(60);
      render(
        <SuggestedAction
          action={makeAction({
            payload: { recipients: [longRecipient], subject: longSubject, body: longBody },
          })}
          onApprove={vi.fn()}
          onReject={vi.fn()}
        />,
      );

      expect(screen.getByText(longRecipient)).toBeInTheDocument();
      expect(screen.getByText(longSubject)).toBeInTheDocument();
      expect(screen.getByText(longBody)).toBeInTheDocument();
    });
  });
});
