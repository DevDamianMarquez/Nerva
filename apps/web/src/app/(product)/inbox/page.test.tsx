import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import InboxPage from "./page";

describe("Inbox", () => {
  it("shows an empty state that explains how to get started", () => {
    render(<InboxPage />);

    expect(
      screen.getByRole("heading", { name: "Connect your inbox to get started" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/You approve every action/)).toBeInTheDocument();
  });

  it("does not offer a Gmail connection that does not exist yet", () => {
    render(<InboxPage />);

    expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeDisabled();
    expect(screen.getByText(/coming in the next release/)).toBeInTheDocument();
  });
});
