/**
 * Types mirroring the API's response schemas (see apps/api/src/nerva/schemas/).
 * The two apps share no code, so these are kept in sync by hand.
 */

export type MemberRole = "owner" | "admin" | "member";

export interface User {
  id: string;
  email: string;
  name: string;
  created_at: string;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  /** The calling user's role in this workspace. */
  role: MemberRole;
}

export interface Member {
  user_id: string;
  email: string;
  name: string;
  role: MemberRole;
  joined_at: string;
}

export interface Me {
  user: User;
  workspaces: Workspace[];
}

export type OpportunityStage = "new" | "qualified" | "proposal" | "won" | "lost";

export interface Opportunity {
  id: string;
  workspace_id: string;
  contact_id: string | null;
  company_id: string | null;
  conversation_id: string | null;
  title: string;
  summary: string | null;
  stage: OpportunityStage;
  estimated_value_cents: number | null;
  currency: string;
  created_at: string;
  updated_at: string;
}

export type ActionStatus =
  "proposed" | "approved" | "executing" | "completed" | "failed" | "rejected";
export type ActorType = "user" | "nerva";

export interface Action {
  id: string;
  workspace_id: string;
  opportunity_id: string | null;
  /** The conversation this proposal was reasoned from. `null` for a
   * human-proposed action, or for an automatic action type that is not
   * conversation-sourced (see `GET .../actions?conversation_id=`). */
  conversation_id: string | null;
  type: string;
  status: ActionStatus;
  summary: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown> | null;
  created_by_type: ActorType;
  created_by_user_id: string | null;
  approved_by_user_id: string | null;
  approved_at: string | null;
  rejected_by_user_id: string | null;
  rejected_at: string | null;
  rejection_reason: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export type ReasonOutcome = "no_action" | "policy_rejected" | "action_created" | "action_duplicate";

/** The response of `POST .../conversations/{id}/reason`. Deliberately
 * narrower than the backend's internal reasoning result: no provider, model,
 * prompt, fingerprint, memory ids, or policy-reason code ever reach the
 * frontend — `detail` is the backend's own fixed, safe explanation text
 * only, never raw/model-authored output. */
export interface ReasonConversationRead {
  outcome: ReasonOutcome;
  action: Action | null;
  detail: string | null;
}

export interface ActivityEvent {
  id: string;
  workspace_id: string;
  actor_type: ActorType;
  actor_user_id: string | null;
  event_type: string;
  entity_type: string;
  entity_id: string;
  summary: string;
  data: Record<string, unknown>;
  created_at: string;
}

// --- Nerva Connect: Gmail integration -----------------------------------------------

export type IntegrationProvider = "google";
export type IntegrationStatus = "connected" | "needs_reauth" | "error" | "disconnected";

export interface Integration {
  id: string;
  workspace_id: string;
  provider: IntegrationProvider;
  status: IntegrationStatus;
  external_account_email: string;
  last_synced_at: string | null;
  last_error: string | null;
  created_at: string;
  /** Whether the stored OAuth grant includes `gmail.send`, not just read access.
   * An integration connected before this field existed, or under an older scope
   * list, can be `status: "connected"` with `can_send: false` — it needs to be
   * reconnected before Nerva can send through it. */
  can_send: boolean;
}

export interface GoogleConnectResponse {
  authorization_url: string;
}

export interface SyncResult {
  threads_seen: number;
  conversations_created: number;
  conversations_updated: number;
  messages_created: number;
}

// --- Nerva Inbox: conversations and messages ----------------------------------------

export type ConversationChannel = "email";

export interface Conversation {
  id: string;
  workspace_id: string;
  contact_id: string | null;
  integration_id: string | null;
  channel: ConversationChannel;
  subject: string | null;
  last_message_at: string | null;
  preview: string | null;
  created_at: string;
  updated_at: string;
}

export type MessageDirection = "inbound" | "outbound";

export interface Message {
  id: string;
  conversation_id: string;
  direction: MessageDirection;
  sender_email: string;
  sender_name: string | null;
  recipients: string;
  body_text: string;
  sent_at: string;
}

// --- Nerva AI: conversation analysis and business signals ---------------------------

export type AnalysisStatus = "pending" | "completed" | "failed";

export type ConversationCategory =
  | "sales_lead"
  | "customer_support"
  | "billing"
  | "project"
  | "internal"
  | "vendor"
  | "recruiting"
  | "spam"
  | "other";

export type ConversationPriority = "low" | "medium" | "high" | "urgent";

export type SentimentLabel = "positive" | "neutral" | "negative" | "mixed";

export interface ConversationAnalysis {
  id: string;
  status: AnalysisStatus;
  schema_version: number;
  prompt_version: string;
  provider: string;
  model_id: string;
  attempts: number;
  category: ConversationCategory | null;
  priority: ConversationPriority | null;
  sentiment: SentimentLabel | null;
  intent: string | null;
  summary: string | null;
  requires_response: boolean | null;
  commercial_signal: boolean | null;
  confidence: number | null;
  estimated_value_cents: number | null;
  value_currency: string | null;
  value_is_explicit: boolean | null;
  deadline_at: string | null;
  deadline_is_explicit: boolean | null;
  suggested_next_step: string | null;
  mentioned_people: string[];
  mentioned_companies: string[];
  /** A short, safe classification — never raw provider output. */
  last_error: string | null;
  analyzed_at: string | null;
  updated_at: string;
}

/** One row of `GET /conversations/attention` — a conversation whose completed
 * analysis flagged it as needing a reply, or as high/urgent priority. */
export interface ConversationAttention {
  conversation_id: string;
  subject: string | null;
  last_message_at: string | null;
  category: ConversationCategory | null;
  priority: ConversationPriority | null;
  requires_response: boolean | null;
  summary: string | null;
  analyzed_at: string | null;
}

export type BusinessSignalType = "potential_opportunity";
export type BusinessSignalStatus = "pending" | "confirmed" | "dismissed";

export interface BusinessSignal {
  id: string;
  conversation_id: string;
  analysis_id: string;
  type: BusinessSignalType;
  status: BusinessSignalStatus;
  source_fingerprint: string;
  title: string | null;
  summary: string | null;
  estimated_value_cents: number | null;
  currency: string | null;
  value_is_explicit: boolean | null;
  confidence: number | null;
  confirmed_by_user_id: string | null;
  confirmed_at: string | null;
  dismissed_by_user_id: string | null;
  dismissed_at: string | null;
  created_opportunity_id: string | null;
  created_at: string;
  updated_at: string;
}

/** The result of confirming a signal: the response of `POST .../confirm`. */
export interface SignalConfirmation {
  signal: BusinessSignal;
  opportunity: Opportunity;
}

/** The result of `POST /analysis/backfill`. */
export interface AnalysisBackfillResult {
  considered: number;
  analyzed: number;
  cached: number;
  failed: number;
  signals_created: number;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: Message[];
  analysis: ConversationAnalysis | null;
}
