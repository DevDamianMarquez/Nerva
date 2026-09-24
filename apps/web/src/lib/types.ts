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
