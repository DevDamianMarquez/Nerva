"use client";

import { Mail } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { NervaUnderstanding } from "@/components/inbox/nerva-understanding";
import { SuggestedAction } from "@/components/inbox/suggested-action";
import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { PotentialOpportunityCard } from "@/components/signals/potential-opportunity-card";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type {
  Action,
  BusinessSignal,
  Conversation,
  ConversationAnalysis,
  ConversationDetail,
  Integration,
  ReasonConversationRead,
} from "@/lib/types";
import { cn } from "@/lib/utils";

type LoadState = "loading" | "ready" | "error";

function formatTime(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function errorCode(err: ApiError): string | undefined {
  const details = err.details;
  if (details && typeof details === "object" && "code" in details) {
    const code = (details as { code?: unknown }).code;
    return typeof code === "string" ? code : undefined;
  }
  return undefined;
}

/** Maps `/reason`'s own typed failures to safe, product-facing text — never
 * the backend's raw message, which is written for an API consumer, not a
 * reader. 401 and an unrecognized `code` (including a bare 404) fall through
 * to the generic case, which is already safe (see `routes.conversations.
 * reason_about_conversation`'s own error-mapping: nothing it raises ever
 * carries provider output, a stack trace, or a config value). */
function reasonErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    switch (errorCode(err)) {
      case "analysis_not_ready":
        return "Analyze this conversation first, then try Suggest reply.";
      case "recipient_not_resolvable":
        return "Nerva couldn’t find a trusted recipient for this conversation.";
      case "rate_limited":
        return "Nerva is busy right now — try again in a moment.";
      case "ai_not_configured":
        return "Reasoning isn’t available right now.";
      case "ai_unavailable":
        return "Couldn’t reach the AI provider — try again shortly.";
      case "invalid_ai_output":
        return "Nerva couldn’t produce a suggestion this time — try again.";
      default:
        return err.message;
    }
  }
  return "Couldn’t get a suggestion. Try again.";
}

function ConversationList({
  conversations,
  selectedId,
  onSelect,
}: {
  conversations: readonly Conversation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <nav
      aria-label="Conversations"
      className={cn(
        "w-full min-w-0 shrink-0 overflow-y-auto border-r sm:w-80",
        selectedId && "hidden sm:block",
      )}
    >
      <ul className="divide-y">
        {conversations.map((conversation) => (
          <li key={conversation.id}>
            <button
              type="button"
              onClick={() => onSelect(conversation.id)}
              aria-current={selectedId === conversation.id ? "true" : undefined}
              className={cn(
                "block w-full space-y-1 px-4 py-3 text-left transition-colors hover:bg-accent",
                selectedId === conversation.id && "bg-accent",
              )}
            >
              <div className="flex items-baseline justify-between gap-2">
                <p className="truncate font-medium">{conversation.subject || "(no subject)"}</p>
                <p className="shrink-0 text-xs text-muted-foreground">
                  {formatTime(conversation.last_message_at)}
                </p>
              </div>
              <p className="truncate text-xs text-muted-foreground">
                {conversation.preview || "No preview available"}
              </p>
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function ConversationDetailPane({
  state,
  detail,
  signal,
  analyzing,
  analyzeError,
  onAnalyze,
  onConfirmSignal,
  onDismissSignal,
  onBack,
  onRetry,
  actions,
  actionsState,
  onApproveAction,
  onRejectAction,
  onExecuteAction,
  onRetryActions,
  canExecute,
  reasonPending,
  reasonFeedback,
  onSuggestReply,
}: {
  state: LoadState;
  detail: ConversationDetail | null;
  signal: BusinessSignal | null;
  analyzing: boolean;
  analyzeError: string | null;
  onAnalyze: () => void;
  onConfirmSignal: (signal: BusinessSignal) => Promise<void>;
  onDismissSignal: (signal: BusinessSignal) => Promise<void>;
  onBack: () => void;
  onRetry: () => void;
  actions: readonly Action[];
  actionsState: LoadState;
  onApproveAction: (action: Action) => Promise<void>;
  onRejectAction: (action: Action) => Promise<void>;
  onExecuteAction: (action: Action) => Promise<void>;
  onRetryActions: () => void;
  canExecute: boolean;
  reasonPending: boolean;
  reasonFeedback: string | null;
  onSuggestReply: () => void;
}) {
  if (state === "loading") {
    return <p className="p-8 text-center text-muted-foreground">Loading…</p>;
  }
  if (state === "error") {
    return (
      <div className="p-8">
        <LoadError onRetry={onRetry} />
      </div>
    );
  }
  if (!detail) return null;

  return (
    <section aria-label="Conversation" className="min-w-0 flex-1 overflow-y-auto">
      <div className="border-b p-4 sm:p-6">
        <Button variant="ghost" size="sm" onClick={onBack} className="mb-2 -ml-2 sm:hidden">
          ← Back
        </Button>
        <h2 className="text-base font-semibold tracking-tight">
          {detail.conversation.subject || "(no subject)"}
        </h2>
      </div>
      <div className="space-y-4 p-4 sm:p-6">
        <NervaUnderstanding
          analysis={detail.analysis}
          analyzing={analyzing}
          error={analyzeError}
          onAnalyze={onAnalyze}
        />
        {signal ? (
          <PotentialOpportunityCard
            signal={signal}
            onConfirm={onConfirmSignal}
            onDismiss={onDismissSignal}
          />
        ) : null}
      </div>
      <ul className="space-y-6 p-4 sm:p-6">
        {detail.messages.map((message) => (
          <li key={message.id} className="space-y-1.5">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5">
              <p className="text-sm font-medium">
                {message.direction === "outbound"
                  ? "You"
                  : message.sender_name || message.sender_email}
              </p>
              <p className="text-xs text-muted-foreground">
                {new Date(message.sent_at).toLocaleString()}
              </p>
            </div>
            {message.recipients ? (
              <p className="text-xs text-muted-foreground">To: {message.recipients}</p>
            ) : null}
            {/* Plain text only: the API already converted any HTML body to safe text
                (see docs/ARCHITECTURE.md's email-content-safety section). React escapes
                this like any other string — nothing here is ever rendered as HTML. */}
            <p className="text-sm whitespace-pre-wrap">{message.body_text}</p>
          </li>
        ))}
      </ul>

      {/* Suggest reply is always visible once a conversation is open — it does
          not depend on a client-side guess about analysis/recipient readiness
          (see `reasonErrorMessage`'s own docstring); the backend's 409s drive
          that feedback instead. Zero Actions is otherwise still a normal state
          (most conversations have none): no scary empty-state message, just no
          cards. A failure to load existing Actions is isolated to this
          section — it never blocks or hides the messages/analysis above. */}
      <div className="space-y-4 border-t p-4 sm:p-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-sm font-semibold tracking-tight">Suggested actions</h3>
          <Button variant="outline" size="sm" disabled={reasonPending} onClick={onSuggestReply}>
            {reasonPending ? "Thinking…" : "Suggest reply"}
          </Button>
        </div>

        {reasonFeedback ? (
          <p role="status" className="text-sm text-muted-foreground">
            {reasonFeedback}
          </p>
        ) : null}

        {actionsState === "loading" ? (
          <p className="text-sm text-muted-foreground">Loading…</p>
        ) : actionsState === "error" ? (
          <LoadError onRetry={onRetryActions} />
        ) : (
          actions.map((action) => (
            <SuggestedAction
              key={action.id}
              action={action}
              onApprove={onApproveAction}
              onReject={onRejectAction}
              canExecute={canExecute}
              onExecute={onExecuteAction}
            />
          ))
        )}
      </div>
    </section>
  );
}

export function InboxView() {
  const { workspace } = useAuth();
  const searchParams = useSearchParams();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [hasGmail, setHasGmail] = useState(false);
  const [state, setState] = useState<LoadState>("loading");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [detailState, setDetailState] = useState<LoadState>("ready");
  const [actions, setActions] = useState<Action[]>([]);
  const [actionsState, setActionsState] = useState<LoadState>("ready");
  const [pendingSignals, setPendingSignals] = useState<BusinessSignal[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);
  const [reasonPending, setReasonPending] = useState(false);
  const [reasonFeedback, setReasonFeedback] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!workspace) return;
    setState("loading");
    try {
      const [integrations, list, signals] = await Promise.all([
        api.get<Integration[]>(`/v1/workspaces/${workspace.id}/integrations`),
        api.get<Conversation[]>(`/v1/workspaces/${workspace.id}/conversations`),
        api.get<BusinessSignal[]>(`/v1/workspaces/${workspace.id}/signals?status=pending`),
      ]);
      setHasGmail(integrations.some((integration) => integration.status !== "disconnected"));
      setConversations(list);
      setPendingSignals(signals);
      setState("ready");
    } catch {
      setState("error");
    }
  }, [workspace]);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  // Independent of the conversation-detail fetch: a conversation with no
  // Actions (the common case) is a normal, non-error state (see
  // `ConversationDetailPane`'s own rendering), and a failure here must never
  // block or hide messages/analysis that already loaded successfully.
  const loadActions = useCallback(
    async (conversationId: string) => {
      if (!workspace) return;
      setActionsState("loading");
      try {
        const found = await api.get<Action[]>(
          `/v1/workspaces/${workspace.id}/actions?conversation_id=${conversationId}`,
        );
        setActions(found);
        setActionsState("ready");
      } catch {
        setActionsState("error");
      }
    },
    [workspace],
  );

  const selectConversation = useCallback(
    (id: string) => {
      if (!workspace) return;
      setSelectedId(id);
      setAnalyzeError(null);
      // Reasoning feedback belongs to one conversation at a time — never carry
      // a stale "no suggestion"/error message over to the next one opened.
      setReasonFeedback(null);
      setDetailState("loading");
      void (async () => {
        try {
          const found = await api.get<ConversationDetail>(
            `/v1/workspaces/${workspace.id}/conversations/${id}`,
          );
          setDetail(found);
          setDetailState("ready");
        } catch {
          setDetailState("error");
        }
      })();
      void loadActions(id);
    },
    [workspace, loadActions],
  );

  // Today's "Needs your attention" links here as `?conversation=<id>`; open that
  // conversation directly instead of leaving the visitor to find it themselves.
  useEffect(() => {
    const conversationId = searchParams.get("conversation");
    if (!conversationId) return;
    void Promise.resolve().then(() => selectConversation(conversationId));
  }, [searchParams, selectConversation]);

  const refreshPendingSignals = async () => {
    if (!workspace) return;
    try {
      const signals = await api.get<BusinessSignal[]>(
        `/v1/workspaces/${workspace.id}/signals?status=pending`,
      );
      setPendingSignals(signals);
    } catch {
      // Non-critical: a freshly created signal simply will not appear until the
      // next successful load. The analysis itself already succeeded.
    }
  };

  const analyze = async () => {
    if (!workspace || !selectedId || analyzing) return;
    setAnalyzing(true);
    setAnalyzeError(null);
    try {
      const result = await api.post<ConversationAnalysis>(
        `/v1/workspaces/${workspace.id}/conversations/${selectedId}/analyze`,
      );
      setDetail((current) => (current ? { ...current, analysis: result } : current));
      await refreshPendingSignals();
    } catch (err) {
      setAnalyzeError(
        err instanceof ApiError ? err.message : "Could not analyze this conversation.",
      );
    } finally {
      setAnalyzing(false);
    }
  };

  const confirmSignal = async (signal: BusinessSignal) => {
    if (!workspace) return;
    await api.post(`/v1/workspaces/${workspace.id}/signals/${signal.id}/confirm`);
    setPendingSignals((current) => current.filter((s) => s.id !== signal.id));
  };

  const dismissSignal = async (signal: BusinessSignal) => {
    if (!workspace) return;
    await api.post(`/v1/workspaces/${workspace.id}/signals/${signal.id}/dismiss`);
    setPendingSignals((current) => current.filter((s) => s.id !== signal.id));
  };

  // Approve/reject only ever call the backend's own `/approve`/`/reject`
  // endpoints with the action's id — never `/execute`, never `/reason`, and
  // never a payload. The updated row in the response (never a locally-guessed
  // status) is what replaces this action in state; a failure leaves the
  // previous authoritative action untouched (the `catch` in
  // `SuggestedAction` is what actually shows the error and re-enables its
  // buttons — this function simply lets that rejection propagate).
  const decideAction = async (action: Action, decision: "approve" | "reject") => {
    if (!workspace) return;
    const updated = await api.post<Action>(
      `/v1/workspaces/${workspace.id}/actions/${action.id}/${decision}`,
    );
    setActions((current) => current.map((a) => (a.id === updated.id ? updated : a)));
  };

  // Send email: calls only `/execute` with the action's id, nothing else —
  // never `/reason`, never `/approve`/`/reject`, and never the recipient/
  // subject/body (the backend's own stored, already-approved payload is the
  // only thing ever sent). Synchronous: the single response already carries
  // the terminal `completed`/`failed` state (see `nerva_actions.engine`), so
  // this never polls or optimistically marks anything "sent" beforehand.
  const executeAction = async (action: Action) => {
    if (!workspace) return;
    try {
      const updated = await api.post<Action>(
        `/v1/workspaces/${workspace.id}/actions/${action.id}/execute`,
      );
      setActions((current) => current.map((a) => (a.id === updated.id ? updated : a)));
    } catch (err) {
      // A 409 means this component's local Action state is stale (someone/
      // something else already moved it out of `approved`) — refetch the
      // authoritative list for this conversation rather than guessing.
      // `SuggestedAction` still shows a safe message from the error below;
      // this call never retries `/execute` itself.
      if (err instanceof ApiError && err.status === 409 && selectedId) {
        void loadActions(selectedId);
      }
      throw err;
    }
  };

  const suggestReply = async () => {
    if (!workspace || !selectedId || reasonPending) return;
    setReasonPending(true);
    setReasonFeedback(null);
    try {
      const result = await api.post<ReasonConversationRead>(
        `/v1/workspaces/${workspace.id}/conversations/${selectedId}/reason`,
      );
      if (result.action) {
        const created = result.action;
        setActions((current) =>
          current.some((a) => a.id === created.id)
            ? current.map((a) => (a.id === created.id ? created : a))
            : [created, ...current],
        );
      }
      if (result.outcome === "no_action") {
        setReasonFeedback("Nerva doesn’t recommend an action right now.");
      } else if (result.outcome === "policy_rejected") {
        setReasonFeedback("Nerva couldn’t create a safe suggestion.");
      }
      // action_created / action_duplicate: the proposal itself, now visible
      // in the list above, is the feedback — no extra banner needed.
    } catch (err) {
      setReasonFeedback(reasonErrorMessage(err));
    } finally {
      setReasonPending(false);
    }
  };

  if (!workspace) return null;

  if (state === "error") {
    return (
      <PageContainer width="wide">
        <PageHeader
          title="Inbox"
          description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
        />
        <LoadError onRetry={() => void load()} />
      </PageContainer>
    );
  }

  if (state === "loading") {
    return (
      <PageContainer width="wide">
        <PageHeader
          title="Inbox"
          description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
        />
        <p className="py-14 text-center text-muted-foreground">Loading…</p>
      </PageContainer>
    );
  }

  if (!hasGmail) {
    return (
      <PageContainer width="narrow">
        <PageHeader
          title="Inbox"
          description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
        />
        <div className="rounded-xl border border-dashed">
          <EmptyState
            icon={Mail}
            title="Connect Gmail to see conversations here"
            description="Nerva reads your email to find people worth following up with. Connect your Gmail account in Settings to get started."
          >
            <Button asChild>
              <Link href="/settings">Go to Settings</Link>
            </Button>
          </EmptyState>
        </div>
      </PageContainer>
    );
  }

  if (conversations.length === 0) {
    return (
      <PageContainer width="narrow">
        <PageHeader
          title="Inbox"
          description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
        />
        <div className="rounded-xl border">
          <EmptyState
            icon={Mail}
            title="No conversations yet"
            description="Gmail is connected, but nothing has been synced yet. Sync it from Settings to bring in your recent conversations."
          >
            <Button variant="outline" asChild>
              <Link href="/settings">Go to Settings</Link>
            </Button>
          </EmptyState>
        </div>
      </PageContainer>
    );
  }

  const currentSignal = pendingSignals.find((s) => s.conversation_id === selectedId) ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b px-4 py-4 sm:px-8 sm:py-6">
        <PageHeader
          title="Inbox"
          description="The conversations Nerva is watching for leads, follow-ups and anything waiting on you."
        />
      </div>
      <div className="flex min-h-0 flex-1">
        <ConversationList
          conversations={conversations}
          selectedId={selectedId}
          onSelect={(id) => void selectConversation(id)}
        />
        {selectedId ? (
          <ConversationDetailPane
            state={detailState}
            detail={detail}
            signal={currentSignal}
            analyzing={analyzing}
            analyzeError={analyzeError}
            onAnalyze={() => void analyze()}
            onConfirmSignal={confirmSignal}
            onDismissSignal={dismissSignal}
            onBack={() => setSelectedId(null)}
            onRetry={() => void selectConversation(selectedId)}
            actions={actions}
            actionsState={actionsState}
            onApproveAction={(action) => decideAction(action, "approve")}
            onRejectAction={(action) => decideAction(action, "reject")}
            onExecuteAction={executeAction}
            onRetryActions={() => void loadActions(selectedId)}
            canExecute={workspace.role === "owner"}
            reasonPending={reasonPending}
            reasonFeedback={reasonFeedback}
            onSuggestReply={() => void suggestReply()}
          />
        ) : (
          <div className="hidden min-w-0 flex-1 items-center justify-center text-muted-foreground sm:flex">
            Select a conversation to read it.
          </div>
        )}
      </div>
    </div>
  );
}
