import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
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

type Props = ComponentProps<typeof SuggestedAction>;

/** Renders `SuggestedAction` with safe defaults (a proposed send_email,
 * owner-level `canExecute`, no-op handlers) — individual tests override only
 * what they care about, matching every existing card-component test file's
 * convention in this codebase (`potential-opportunity-card.test.tsx`). */
function renderAction(overrides: Partial<Props> = {}) {
  const props: Props = {
    action: makeAction(),
    onApprove: vi.fn(),
    onReject: vi.fn(),
    canExecute: true,
    onExecute: vi.fn(),
    ...overrides,
  };
  return render(<SuggestedAction {...props} />);
}

describe("SuggestedAction", () => {
  describe("a proposed send_email", () => {
    it("renders the recipient, subject, body, and both decision buttons", () => {
      renderAction();

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
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
    });

    it("joins multiple recipients for display", () => {
      renderAction({
        action: makeAction({
          payload: { ...makeAction().payload, recipients: ["a@x.com", "b@x.com"] },
        }),
      });

      expect(screen.getByText("a@x.com, b@x.com")).toBeInTheDocument();
    });

    it("calls onApprove with the action when Approve is clicked", async () => {
      const onApprove = vi.fn().mockResolvedValue(undefined);
      renderAction({ onApprove });

      await userEvent.click(
        screen.getByRole("button", { name: "Approve: Reply to Alex about pricing" }),
      );

      expect(onApprove).toHaveBeenCalledWith(makeAction());
    });

    it("calls onReject with the action when Reject is clicked", async () => {
      const onReject = vi.fn().mockResolvedValue(undefined);
      renderAction({ onReject });

      await userEvent.click(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      );

      expect(onReject).toHaveBeenCalledWith(makeAction());
    });

    it("disables both buttons while a decision is in flight", async () => {
      const onApprove = vi.fn(() => new Promise<void>(() => {})); // never resolves
      renderAction({ onApprove });

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
      renderAction({ onApprove });

      const approveButton = screen.getByRole("button", {
        name: "Approve: Reply to Alex about pricing",
      });
      await userEvent.click(approveButton);
      await userEvent.click(approveButton);

      expect(onApprove).toHaveBeenCalledTimes(1);
    });

    it("shows a safe error and keeps the proposal visible when approval fails", async () => {
      const onApprove = vi.fn().mockRejectedValue(new Error("network"));
      renderAction({ onApprove });

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
      renderAction({ onReject });

      await userEvent.click(
        screen.getByRole("button", { name: "Reject: Reply to Alex about pricing" }),
      );

      expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("renders the body as plain text, never as HTML", () => {
      const htmlLookingBody = "<b>not actually bold</b>";
      renderAction({ action: makeAction({ payload: { ...makeAction().payload, body: htmlLookingBody } }) });

      expect(screen.getByText(htmlLookingBody)).toBeInTheDocument();
      expect(document.querySelector("b")).not.toBeInTheDocument();
    });
  });

  describe("lifecycle status", () => {
    it("shows Rejected and no decision buttons once rejected", () => {
      renderAction({ action: makeAction({ status: "rejected" }) });

      expect(screen.getByText("Rejected")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("shows Executing with no mutation buttons", () => {
      renderAction({ action: makeAction({ status: "executing" }) });

      expect(screen.getByText("Executing")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });
  });

  describe("unknown or malformed data", () => {
    it("does not crash for an unknown action type", () => {
      renderAction({
        action: makeAction({ type: "update_opportunity_stage", payload: { target_stage: "qualified" } }),
      });

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
      expect(screen.getByText("Reply to Alex about pricing")).toBeInTheDocument();
    });

    it("does not crash and shows a safe fallback for a malformed send_email payload", () => {
      renderAction({ action: makeAction({ payload: { subject: 42, body: null } }) });

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
      expect(screen.getByText(/couldn.t be displayed/)).toBeInTheDocument();
    });

    it("does not crash when the payload is entirely empty", () => {
      renderAction({ action: makeAction({ payload: {} }) });

      expect(screen.getByText("Suggested action")).toBeInTheDocument();
    });

    it("still offers Approve/Reject for a malformed payload while still proposed", () => {
      renderAction({ action: makeAction({ payload: { recipients: "not-an-array" } }) });

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
      renderAction({
        action: makeAction({
          payload: { recipients: [longRecipient], subject: longSubject, body: longBody },
        }),
      });

      expect(screen.getByText(longRecipient)).toBeInTheDocument();
      expect(screen.getByText(longSubject)).toBeInTheDocument();
      expect(screen.getByText(longBody)).toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // M8.3b: Send email (execution)
  // -------------------------------------------------------------------------

  describe("approved send_email — owner", () => {
    it("shows Send email, not Approve/Reject/Approved-only", () => {
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true });

      expect(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      ).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Approve:/ })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /^Reject:/ })).not.toBeInTheDocument();
      expect(screen.getByText("Approved")).toBeInTheDocument(); // status badge still shown
    });

    it("calls onExecute with the action when Send email is clicked", async () => {
      const onExecute = vi.fn().mockResolvedValue(undefined);
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      await userEvent.click(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      );

      expect(onExecute).toHaveBeenCalledWith(makeAction({ status: "approved" }));
    });

    it("disables Send while a request is in flight and shows Sending…", async () => {
      const onExecute = vi.fn(() => new Promise<void>(() => {}));
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      const sendButton = screen.getByRole("button", {
        name: "Send email: Reply to Alex about pricing",
      });
      await userEvent.click(sendButton);

      await waitFor(() => expect(sendButton).toHaveTextContent("Sending…"));
      expect(sendButton).toBeDisabled();
    });

    it("never issues a second execute request while one is already pending", async () => {
      const onExecute = vi.fn(() => new Promise<void>(() => {}));
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      const sendButton = screen.getByRole("button", {
        name: "Send email: Reply to Alex about pricing",
      });
      await userEvent.click(sendButton);
      await userEvent.click(sendButton);

      expect(onExecute).toHaveBeenCalledTimes(1);
    });

    it("shows a product-facing message, not a raw 403, when execution is forbidden", async () => {
      const { ApiError } = await import("@/lib/api-client");
      const onExecute = vi.fn().mockRejectedValue(new ApiError(403, "forbidden", "forbidden"));
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      await userEvent.click(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      );

      expect(
        await screen.findByText("Only the workspace owner can send this."),
      ).toBeInTheDocument();
    });

    it("shows a product-facing message, not a raw 409, when the proposal is no longer sendable", async () => {
      const { ApiError } = await import("@/lib/api-client");
      const onExecute = vi
        .fn()
        .mockRejectedValue(new ApiError(409, "not_executable", "An action that is executing cannot be executed."));
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      await userEvent.click(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      );

      expect(
        await screen.findByText("This proposal is no longer ready to send."),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      ).toBeEnabled();
    });

    it("keeps the proposal visible and the button enabled again on network failure", async () => {
      const onExecute = vi.fn().mockRejectedValue(new Error("network"));
      renderAction({ action: makeAction({ status: "approved" }), canExecute: true, onExecute });

      await userEvent.click(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      );

      expect(await screen.findByText(/Couldn.t send this/)).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Send email: Reply to Alex about pricing" }),
      ).toBeEnabled();
    });
  });

  describe("approved send_email — non-owner", () => {
    it("shows only the Approved status, no Send email button", () => {
      renderAction({ action: makeAction({ status: "approved" }), canExecute: false });

      expect(screen.getByText("Approved")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });
  });

  describe("terminal send_email states", () => {
    it("renders Sent for a completed send_email, with no button", () => {
      renderAction({ action: makeAction({ status: "completed", result: { external_reference_id: "msg_1" } }) });

      expect(screen.getByText("Sent")).toBeInTheDocument();
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      // Internal Gmail ids are never shown to the user.
      expect(screen.queryByText("msg_1")).not.toBeInTheDocument();
      // Recipient/subject/body remain visible as historical context.
      expect(screen.getByText("alex@example.com")).toBeInTheDocument();
      expect(screen.getByText("Re: Pro plan pricing")).toBeInTheDocument();
    });

    it("renders Failed to send, the safe result.error, and a Settings link, with no button", () => {
      renderAction({
        action: makeAction({
          status: "failed",
          result: { error: "Gmail is connected without permission to send email; it needs to be reconnected." },
        }),
      });

      expect(screen.getByText("Failed to send")).toBeInTheDocument();
      expect(
        screen.getByText(
          "Gmail is connected without permission to send email; it needs to be reconnected.",
        ),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Go to Settings" })).toHaveAttribute(
        "href",
        "/settings",
      );
      expect(screen.queryByRole("button")).not.toBeInTheDocument();
    });

    it("still shows the Settings link for a failed send with no result.error", () => {
      renderAction({ action: makeAction({ status: "failed", result: null }) });

      expect(screen.getByText("Failed to send")).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "Go to Settings" })).toBeInTheDocument();
    });

    it("does not show a Settings link for a non-send_email failed action", () => {
      renderAction({
        action: makeAction({ type: "update_opportunity_stage", status: "failed", result: null }),
      });

      expect(screen.getByText("Failed")).toBeInTheDocument();
      expect(screen.queryByRole("link", { name: "Go to Settings" })).not.toBeInTheDocument();
    });
  });

  describe("Send is never offered outside approved+owner+valid-payload", () => {
    it("proposed cannot Send", () => {
      renderAction({ action: makeAction({ status: "proposed" }), canExecute: true });
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
    });

    it("rejected cannot Send", () => {
      renderAction({ action: makeAction({ status: "rejected" }), canExecute: true });
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
    });

    it("executing cannot Send", () => {
      renderAction({ action: makeAction({ status: "executing" }), canExecute: true });
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
    });

    it("completed cannot Send", () => {
      renderAction({ action: makeAction({ status: "completed" }), canExecute: true });
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
    });

    it("failed cannot Send (no retry)", () => {
      renderAction({ action: makeAction({ status: "failed" }), canExecute: true });
      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      expect(screen.queryByText(/retry|try again/i)).not.toBeInTheDocument();
    });

    it("a malformed approved send_email payload cannot Send, even though it is approved", () => {
      renderAction({
        action: makeAction({ status: "approved", payload: { subject: 1, body: 2 } }),
        canExecute: true,
      });

      expect(screen.queryByRole("button", { name: /^Send email:/ })).not.toBeInTheDocument();
      expect(screen.getByText(/couldn.t be displayed/)).toBeInTheDocument();
    });

    it("an approved unknown action type cannot Send (no generic execute UI)", () => {
      renderAction({
        action: makeAction({ status: "approved", type: "update_opportunity_stage" }),
        canExecute: true,
      });

      expect(screen.queryByRole("button")).not.toBeInTheDocument();
      expect(screen.getByText("Approved")).toBeInTheDocument();
    });
  });
});
