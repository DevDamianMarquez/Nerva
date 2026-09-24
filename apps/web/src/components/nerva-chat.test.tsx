import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it } from "vitest";

import { NervaChat } from "@/components/nerva-chat";

const suggestions = ["Which leads need a follow-up this week?", "What is waiting on my approval?"];

beforeAll(() => {
  // jsdom does not implement scrollIntoView.
  Element.prototype.scrollIntoView = () => {};
});

describe("NervaChat", () => {
  it("introduces Nerva and is honest that it cannot answer yet", () => {
    render(<NervaChat suggestions={suggestions} />);

    expect(screen.getByRole("log", { name: "Conversation with Nerva" })).toBeInTheDocument();
    expect(screen.getByText(/can’t answer yet/)).toBeInTheDocument();
    expect(screen.getByText(/Preview only/)).toBeInTheDocument();
  });

  it("does not allow sending an empty message", async () => {
    render(<NervaChat suggestions={suggestions} />);
    const send = screen.getByRole("button", { name: "Send message" });

    expect(send).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Message Nerva"), "   ");
    expect(send).toBeDisabled();
  });

  it("fills the composer when a suggestion is chosen", async () => {
    render(<NervaChat suggestions={suggestions} />);

    await userEvent.click(screen.getByRole("button", { name: suggestions[0] }));

    expect(screen.getByLabelText("Message Nerva")).toHaveValue(suggestions[0]);
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
  });

  it("moves focus into the composer after a suggestion is chosen, so Enter sends it", async () => {
    render(<NervaChat suggestions={suggestions} />);

    await userEvent.click(screen.getByRole("button", { name: suggestions[0] }));
    expect(screen.getByLabelText("Message Nerva")).toHaveFocus();

    await userEvent.keyboard("{Enter}");
    expect(screen.getByRole("log")).toHaveTextContent("has not been sent anywhere");
  });

  it("shows the message and a notice that nothing was sent, then clears the composer", async () => {
    render(<NervaChat suggestions={suggestions} />);
    const composer = screen.getByLabelText("Message Nerva");

    await userEvent.type(composer, "Who should I follow up with?");
    await userEvent.click(screen.getByRole("button", { name: "Send message" }));

    const log = screen.getByRole("log");
    expect(log).toHaveTextContent("Who should I follow up with?");
    expect(log).toHaveTextContent("has not been sent anywhere");
    expect(composer).toHaveValue("");
  });

  it("sends on Enter but keeps a newline on Shift+Enter", async () => {
    render(<NervaChat suggestions={suggestions} />);
    const composer = screen.getByLabelText("Message Nerva");

    await userEvent.type(composer, "first line{Shift>}{Enter}{/Shift}second line");
    expect(composer).toHaveValue("first line\nsecond line");
    expect(screen.getByRole("log")).not.toHaveTextContent("has not been sent anywhere");

    await userEvent.type(composer, "{Enter}");
    expect(screen.getByRole("log")).toHaveTextContent("has not been sent anywhere");
    expect(composer).toHaveValue("");
  });
});
