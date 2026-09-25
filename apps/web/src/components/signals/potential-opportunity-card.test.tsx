import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { BusinessSignal } from "@/lib/types";

import { PotentialOpportunityCard } from "./potential-opportunity-card";

function makeSignal(overrides: Partial<BusinessSignal> = {}): BusinessSignal {
  return {
    id: "s1",
    conversation_id: "c1",
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
    ...overrides,
  };
}

describe("PotentialOpportunityCard", () => {
  it("shows the title, summary, estimated value and confidence", () => {
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={vi.fn()} onDismiss={vi.fn()} />,
    );

    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Interested in a 20-seat plan.")).toBeInTheDocument();
    expect(screen.getByText(/\$4,000\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\(estimated\)/)).toBeInTheDocument();
    expect(screen.getByText(/87%/)).toBeInTheDocument();
  });

  it("does not render a value line when no estimate or confidence is present", () => {
    render(
      <PotentialOpportunityCard
        signal={makeSignal({ estimated_value_cents: null, currency: null, confidence: null })}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.queryByText(/Potential value/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Confidence/)).not.toBeInTheDocument();
  });

  it("calls onConfirm with the signal when Confirm is clicked", async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={onConfirm} onDismiss={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Confirm opportunity: Acme Corp" }));

    expect(onConfirm).toHaveBeenCalledWith(makeSignal());
  });

  it("calls onDismiss with the signal when Dismiss is clicked", async () => {
    const onDismiss = vi.fn().mockResolvedValue(undefined);
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={vi.fn()} onDismiss={onDismiss} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Dismiss: Acme Corp" }));

    expect(onDismiss).toHaveBeenCalledWith(makeSignal());
  });

  it("shows a loading label and disables both buttons while confirming", async () => {
    let resolveConfirm: () => void = () => {};
    const onConfirm = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveConfirm = resolve;
        }),
    );
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={onConfirm} onDismiss={vi.fn()} />,
    );

    const confirmButton = screen.getByRole("button", { name: "Confirm opportunity: Acme Corp" });
    await userEvent.click(confirmButton);

    // The accessible name stays stable (what it acts on); the visible label and
    // disabled state are what communicate "in progress" here.
    await waitFor(() => expect(confirmButton).toHaveTextContent("Confirming…"));
    expect(confirmButton).toBeDisabled();
    expect(screen.getByRole("button", { name: "Dismiss: Acme Corp" })).toBeDisabled();

    resolveConfirm();
  });

  it("prevents a second click while a decision is already in flight", async () => {
    const onConfirm = vi.fn(() => new Promise<void>(() => {})); // never resolves
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={onConfirm} onDismiss={vi.fn()} />,
    );

    const button = screen.getByRole("button", { name: "Confirm opportunity: Acme Corp" });
    await userEvent.click(button);
    await userEvent.click(button);

    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("shows an error and keeps the signal visible if confirming fails", async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error("network"));
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={onConfirm} onDismiss={vi.fn()} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Confirm opportunity: Acme Corp" }));

    expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm opportunity: Acme Corp" })).toBeEnabled();
  });

  it("shows an error and keeps the signal visible if dismissing fails", async () => {
    const onDismiss = vi.fn().mockRejectedValue(new Error("network"));
    render(
      <PotentialOpportunityCard signal={makeSignal()} onConfirm={vi.fn()} onDismiss={onDismiss} />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Dismiss: Acme Corp" }));

    expect(await screen.findByText(/Couldn.t save that decision/)).toBeInTheDocument();
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
  });

  it("falls back to a generic title when the signal has none", () => {
    render(
      <PotentialOpportunityCard
        signal={makeSignal({ title: null, summary: null })}
        onConfirm={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByText("Potential opportunity")).toBeInTheDocument();
  });
});
