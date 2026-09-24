import { render, screen } from "@testing-library/react";
import { beforeAll, describe, expect, it } from "vitest";

import NervaPage from "./page";

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {};
});

describe("Nerva", () => {
  it("shows the chat with a message composer, marked as not connected yet", () => {
    render(<NervaPage />);

    expect(screen.getByRole("heading", { level: 1, name: "Nerva" })).toBeInTheDocument();
    expect(screen.getByLabelText("Message Nerva")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /follow-up this week/ })).toBeInTheDocument();
  });
});
