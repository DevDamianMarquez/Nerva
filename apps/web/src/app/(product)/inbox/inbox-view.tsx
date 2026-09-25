"use client";

import { Mail } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { EmptyState } from "@/components/empty-state";
import { NervaUnderstanding } from "@/components/inbox/nerva-understanding";
import { LoadError } from "@/components/load-error";
import { PageContainer, PageHeader } from "@/components/page-header";
import { PotentialOpportunityCard } from "@/components/signals/potential-opportunity-card";
import { Button } from "@/components/ui/button";
import { api, ApiError } from "@/lib/api-client";
import { useAuth } from "@/lib/auth-context";
import type {
  BusinessSignal,
  Conversation,
  ConversationAnalysis,
  ConversationDetail,
  Integration,
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
  const [pendingSignals, setPendingSignals] = useState<BusinessSignal[]>([]);
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState<string | null>(null);

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

  const selectConversation = useCallback(
    async (id: string) => {
      if (!workspace) return;
      setSelectedId(id);
      setAnalyzeError(null);
      setDetailState("loading");
      try {
        const found = await api.get<ConversationDetail>(
          `/v1/workspaces/${workspace.id}/conversations/${id}`,
        );
        setDetail(found);
        setDetailState("ready");
      } catch {
        setDetailState("error");
      }
    },
    [workspace],
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
