import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "@/lib/api-client";
import type {
  AnalysisBackfillResult,
  GoogleConnectResponse,
  Integration,
  Member,
  SyncResult,
} from "@/lib/types";

import { SettingsView } from "./settings-view";

// A stable reference: a fresh object per call would re-run the load effect forever.
const workspace = {
  id: "w1",
  name: "Northwind Studio",
  slug: "northwind-studio",
  created_at: "",
  role: "owner" as const,
};
vi.mock("@/lib/auth-context", () => ({ useAuth: () => ({ workspace }) }));

const replace = vi.fn();
// A stable reference, matching real Next.js (useRouter() returns the same object
// across renders): returning a fresh object per call here would make `router` a new
// dependency on every render of an effect that lists it, which — combined with that
// effect's own setState — is an infinite render loop the test would just hang on.
const router = { replace };
let searchParams = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => router,
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

const member: Member = {
  user_id: "u1",
  email: "alex@example.com",
  name: "Alex Morgan",
  role: "owner",
  joined_at: "2024-01-01T00:00:00Z",
};

const disconnectedIntegration: Integration = {
  id: "i1",
  workspace_id: "w1",
  provider: "google",
  status: "disconnected",
  external_account_email: "",
  last_synced_at: null,
  last_error: null,
  created_at: "2024-01-01T00:00:00Z",
  can_send: false,
};

const connectedIntegration: Integration = {
  ...disconnectedIntegration,
  status: "connected",
  external_account_email: "alex@gmail.example",
  last_synced_at: "2024-01-02T09:30:00Z",
  can_send: true,
};

function mockData(members: Member[], integrations: Integration[]): void {
  mockedApi.get.mockImplementation((path: string) =>
    Promise.resolve(path.includes("integrations") ? integrations : members),
  );
}

describe("SettingsView", () => {
  beforeEach(() => {
    mockedApi.get.mockReset();
    mockedApi.post.mockReset();
    replace.mockClear();
    searchParams = new URLSearchParams();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the real workspace name and URL", async () => {
    mockData([member], []);

    render(<SettingsView />);

    expect(screen.getByLabelText("Name")).toHaveValue("Northwind Studio");
    expect(screen.getByLabelText("Workspace URL")).toHaveValue("northwind-studio");
    await screen.findByText("Alex Morgan");
  });

  it("lists real members once loaded", async () => {
    mockData([member], []);

    render(<SettingsView />);

    const members = await screen.findByRole("region", { name: "Members" });
    expect(within(members).getByText("Alex Morgan")).toBeInTheDocument();
    expect(within(members).getByText("Owner")).toBeInTheDocument();
  });

  it("offers a retry option when loading fails", async () => {
    mockedApi.get.mockRejectedValue(new Error("network"));

    render(<SettingsView />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows Gmail as not connected with a Connect Gmail button", async () => {
    mockData([member], []);

    render(<SettingsView />);

    const integrations = await screen.findByRole("region", { name: "Integrations" });
    expect(within(integrations).getByText("Not connected")).toBeInTheDocument();
    expect(within(integrations).getByRole("button", { name: "Connect Gmail" })).toBeEnabled();
  });

  it("shows Gmail as connected with the account email and last synced time", async () => {
    mockData([member], [connectedIntegration]);

    render(<SettingsView />);

    const integrations = await screen.findByRole("region", { name: "Integrations" });
    expect(within(integrations).getByText("alex@gmail.example")).toBeInTheDocument();
    expect(within(integrations).getByText("Connected")).toBeInTheDocument();
    expect(within(integrations).getByText(/Last synced/)).toBeInTheDocument();
    expect(within(integrations).getByRole("button", { name: "Sync now" })).toBeInTheDocument();
    expect(within(integrations).getByRole("button", { name: "Disconnect" })).toBeInTheDocument();
  });

  it("shows a needs-attention state and a Reconnect button when reauth is required", async () => {
    mockData([member], [{ ...connectedIntegration, status: "needs_reauth" }]);

    render(<SettingsView />);

    const integrations = await screen.findByRole("region", { name: "Integrations" });
    expect(within(integrations).getByText("Needs attention")).toBeInTheDocument();
    expect(
      within(integrations).getByRole("button", { name: "Reconnect Gmail" }),
    ).toBeInTheDocument();
  });

  it("shows a read-only state and a Reconnect button when the send scope is missing", async () => {
    mockData([member], [{ ...connectedIntegration, can_send: false }]);

    render(<SettingsView />);

    const integrations = await screen.findByRole("region", { name: "Integrations" });
    expect(within(integrations).getByText("Read-only")).toBeInTheDocument();
    expect(within(integrations).queryByText("Connected")).not.toBeInTheDocument();
    expect(
      within(integrations).getByRole("button", { name: "Reconnect Gmail" }),
    ).toBeInTheDocument();
    expect(within(integrations).queryByRole("button", { name: "Sync now" })).not.toBeInTheDocument();
  });

  it("starts the connect flow and redirects the browser to Google", async () => {
    mockData([member], []);
    mockedApi.post.mockResolvedValue({
      authorization_url: "https://accounts.google.com/fake-consent?state=abc",
    } satisfies GoogleConnectResponse);
    // jsdom attempts a real (network) navigation for a cross-origin `location.href`
    // assignment, which hangs the test process. `vi.stubGlobal` swaps the global
    // safely and `vi.unstubAllGlobals()` (in `afterEach` below) restores it, unlike
    // patching `window.location` with `Object.defineProperty` directly.
    vi.stubGlobal("location", { href: "" });

    render(<SettingsView />);
    await userEvent.click(await screen.findByRole("button", { name: "Connect Gmail" }));

    await waitFor(() =>
      expect(window.location.href).toBe("https://accounts.google.com/fake-consent?state=abc"),
    );
    expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/integrations/google/connect");
  });

  it("syncs and shows how many messages were imported", async () => {
    mockData([member], [connectedIntegration]);
    mockedApi.post.mockResolvedValue({
      threads_seen: 2,
      conversations_created: 2,
      conversations_updated: 0,
      messages_created: 3,
    } satisfies SyncResult);

    render(<SettingsView />);
    await userEvent.click(await screen.findByRole("button", { name: "Sync now" }));

    expect(await screen.findByText("Synced 3 messages.")).toBeInTheDocument();
    expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/integrations/i1/sync");
  });

  it("disconnects Gmail", async () => {
    mockData([member], [connectedIntegration]);
    mockedApi.post.mockResolvedValue(disconnectedIntegration);

    render(<SettingsView />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));

    await waitFor(() =>
      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/integrations/i1/disconnect"),
    );
  });

  it("shows a success banner after a successful OAuth redirect and clears the query string", async () => {
    searchParams = new URLSearchParams("integration=connected");
    mockData([member], [connectedIntegration]);

    render(<SettingsView />);

    expect(await screen.findByText("Gmail was connected.")).toBeInTheDocument();
    expect(replace).toHaveBeenCalledWith("/settings");
  });

  it("shows an error banner after a failed OAuth redirect", async () => {
    searchParams = new URLSearchParams("integration=error&reason=invalid_state");
    mockData([member], []);

    render(<SettingsView />);

    expect(
      await screen.findByText(/connection link expired or was already used/i),
    ).toBeInTheDocument();
  });

  describe("AI Understanding", () => {
    it("runs a bounded analysis batch and shows the result summary", async () => {
      mockData([member], []);
      mockedApi.post.mockResolvedValue({
        considered: 12,
        analyzed: 4,
        cached: 7,
        failed: 1,
        signals_created: 2,
      } satisfies AnalysisBackfillResult);

      render(<SettingsView />);
      const section = await screen.findByRole("region", { name: "AI Understanding" });
      await userEvent.click(within(section).getByRole("button", { name: "Run analysis" }));

      expect(mockedApi.post).toHaveBeenCalledWith("/v1/workspaces/w1/analysis/backfill");
      expect(
        await screen.findByText(
          "Analyzed 4 conversations. 7 already up to date. 1 failed. 2 potential opportunities found.",
        ),
      ).toBeInTheDocument();
    });

    it("shows honest copy when nothing needed analysis", async () => {
      mockData([member], []);
      mockedApi.post.mockResolvedValue({
        considered: 0,
        analyzed: 0,
        cached: 0,
        failed: 0,
        signals_created: 0,
      } satisfies AnalysisBackfillResult);

      render(<SettingsView />);
      await userEvent.click(await screen.findByRole("button", { name: "Run analysis" }));

      expect(
        await screen.findByText("Everything is already up to date. Nothing needed analysis."),
      ).toBeInTheDocument();
    });

    it("shows a safe error message when the backfill fails", async () => {
      mockData([member], []);
      mockedApi.post.mockRejectedValue(
        new ApiError(503, "service_unavailable", "AI analysis is not available right now.", {
          code: "ai_not_configured",
        }),
      );

      render(<SettingsView />);
      await userEvent.click(await screen.findByRole("button", { name: "Run analysis" }));

      expect(
        await screen.findByText("AI analysis is not available right now."),
      ).toBeInTheDocument();
    });

    it("disables the button and shows a loading label while the batch runs", async () => {
      mockData([member], []);
      let resolveBackfill: (value: AnalysisBackfillResult) => void = () => {};
      mockedApi.post.mockImplementation(
        () =>
          new Promise((resolve) => {
            resolveBackfill = resolve;
          }),
      );

      render(<SettingsView />);
      const button = await screen.findByRole("button", { name: "Run analysis" });
      await userEvent.click(button);

      expect(await screen.findByRole("button", { name: "Analyzing…" })).toBeDisabled();

      resolveBackfill({ considered: 0, analyzed: 0, cached: 0, failed: 0, signals_created: 0 });
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "Run analysis" })).toBeEnabled(),
      );
    });
  });
});
