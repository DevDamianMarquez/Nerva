# Nerva architecture (v0.1)

This document describes how Nerva is put together today, and where the seams are for
what comes next. For what is in and out of scope, see [MVP.md](MVP.md).

## 1. Frontend / backend boundary

```
┌──────────────────────┐   HTTP + JSON    ┌───────────────────────────┐   SQL   ┌────────────┐
│  apps/web            │ ───────────────▶ │  apps/api                 │ ──────▶ │ PostgreSQL │
│  Next.js (App Router)│   (not wired yet)│  FastAPI                  │         │            │
│  TypeScript, Tailwind│                  │  Pydantic v2, SQLAlchemy 2│         │            │
│  shadcn/ui           │                  │  Alembic                  │         │            │
└──────────────────────┘                  └───────────────────────────┘         └────────────┘
```

- The two apps share **no code**. The contract between them is the HTTP API, described
  by the OpenAPI schema FastAPI serves at `/openapi.json` (interactive docs at `/docs`).
- The frontend owns presentation and interaction. It never talks to the database and
  holds no business rules.
- The backend owns all business rules, validation, authorization and persistence.
- The frontend calls the API directly from the browser (`apps/web/src/lib/api-client.ts`),
  with `credentials: "include"` so the session cookie travels with every request. There
  is no server-side rendering of API data and no BFF layer: the API's own session cookie
  is not readable by the Next.js server (it belongs to a different origin), so
  authentication and data loading both happen client-side. See
  [Authentication](#authentication) and [§9](#9-frontend-authentication-onboarding-and-data).
- CORS (`CORS_ORIGINS`) is configured with `allow_credentials=True` and an explicit
  origin allowlist (never `*`, which browsers reject alongside credentials).

## 2. Backend layout

```
apps/api/src/nerva/
  main.py          ASGI entry point (`uvicorn nerva.main:app`)
  app.py           application factory: middleware, error handlers, routers
  api/             HTTP plumbing: dependencies (auth, tenant gate), router assembly
  routes/          endpoints. Thin: parse request → call a service → shape response
  schemas/         Pydantic request/response models (the public contract)
  services/        use cases. Own transactions, enforce rules, record activity
  repositories/    data access. Workspace-scoped by construction
  models/          SQLAlchemy ORM models
  db/              declarative base, mixins, engine and sessions
  core/            config, enums, error types, actor / workspace context
  nerva_actions/   action lifecycle state machine (real code)
  nerva_core/      future: agent runtime            (docstring only)
  nerva_memory/    memory content contract, lifecycle (real code; see §12)
  nerva_connect/   Google OAuth + Gmail (real code; see §10)
```

Dependencies point one way: `routes → services → repositories → models`. Routes never
touch the database; repositories never raise HTTP errors.

`core/` is application plumbing. `nerva_core/` is unrelated to it: it is reserved for
the future AI runtime (see §7).

Notable choices:

- **Synchronous SQLAlchemy 2 with psycopg 3.** FastAPI runs sync endpoints in a
  threadpool. This is simpler to reason about and test than async, and is plenty for the
  MVP. Nothing in the service layer prevents a later move to async.
- **UUID primary keys**, generated in the application.
- **Enums are `VARCHAR` + `CHECK`**, not native PostgreSQL enums, so adding a value is a
  trivial migration.
- **`JSONB`** for `Action.payload`, `Action.result` and `ActivityEvent.data`.
- **Services commit; repositories flush.** A use case is one transaction.
- **Errors** are domain exceptions (`NotFoundError`, `ConflictError`, …) mapped to one
  response shape: `{"error": {"code", "message", "details"}}`, for validation errors
  as well.

## 3. Domain model

```
users ──< workspace_members >── workspaces
                                    │ (every table below carries workspace_id)
        ┌───────────────┬───────────┼───────────────┬───────────────┬─────────────┐
    companies ──< contacts    conversations ──< messages     activity_events   integrations
        │            │              │                                             │
        └──────┬─────┴──────────────┴─────────────────────────────────────────────┘
          opportunities ──< actions
   memories ── (workspace, or one contact / company / opportunity)
```

| Table | Purpose |
| --- | --- |
| `users` | A person. Exists above the tenant boundary. Email is unique and lower-case; `password_hash` is a bcrypt hash, never returned by the API. |
| `sessions` | A logged-in browser session: `user_id`, a SHA-256 `token_hash` (the raw token lives only in the cookie), `expires_at`, `revoked_at`. Above the tenant boundary, like `users`. |
| `oauth_states` | A short-lived, single-use, server-bound value for one in-progress OAuth connect attempt. Above the tenant boundary. See [§10](#10-nerva-connect-google-oauth-and-gmail-import). |
| `workspaces` | A tenant (a company using Nerva). `slug` is unique. |
| `workspace_members` | `(workspace_id, user_id, role)`. **The authorization source of truth.** Roles: owner, admin, member. |
| `companies` | An organization the workspace deals with. |
| `contacts` | A person, unique by email **within a workspace**; optionally belongs to a company. |
| `integrations` | A connected external account (Gmail today). Provider, status, the connected account's stable id and email, and an *encrypted* refresh token. See [§10](#10-nerva-connect-google-oauth-and-gmail-import). |
| `conversations` | A thread with a contact, with a `channel`, an `external_id` (e.g. a Gmail thread id) and the `integration` that imported it, if any. |
| `messages` | A message within a conversation (direction, sender, recipients, body, sent time), unique per workspace by its provider `external_id` — see idempotency, [§10](#10-nerva-connect-google-oauth-and-gmail-import). |
| `opportunities` | A lead/deal: title, stage (new → qualified → proposal → won/lost), optional value; may link a contact, company and the conversation it came from. |
| `actions` | Something Nerva proposes to do, gated by approval. See §5. |
| `activity_events` | The append-only audit trail. See §6. |
| `memories` | A durable, provenanced fact or preference — about the workspace, or about at most one contact, company or opportunity. Structured/validated `content`, a `status` lifecycle (active → superseded/discarded), and an `origin` (observed vs. inferred, with `confidence` only for the latter). Plain relational rows: no embeddings. See `nerva.nerva_memory`. |

### Who did it: `Actor`

Opportunities, actions, memories and activity events record their originator as
`(type, user_id)` where `type` is `user` or `nerva`. A `CHECK` constraint guarantees
`user_id` is set exactly when `type = 'user'`. This is what lets Nerva propose actions
on its own later, while approvals stay human: `approved_by_user_id` is always a user.

## 4. Multi-tenancy strategy

Nerva is a shared-database, shared-schema multi-tenant system: tenants are separated by
a `workspace_id` column on every business table. Isolation is enforced in **four
independent layers**, so no single bug is enough to leak data:

1. **Authorization gate (API).** Workspace routes live under
   `/v1/workspaces/{workspace_id}/…`. The `get_workspace_context` dependency looks the
   caller up in `workspace_members`. If they are not a member, the response is **404**,
   identical to a workspace that does not exist. Tenants cannot be enumerated. The
   result is a `WorkspaceContext`, the only thing services accept as proof of access.

2. **`workspace_id` is never taken from the client.** It comes from the URL, after the
   gate. Request schemas have no `workspace_id` field and forbid unknown fields
   (`extra="forbid"`), so a body containing `workspace_id` is rejected with `422`
   instead of being silently ignored or, worse, trusted.

3. **Workspace-bound repositories.** `ScopedRepository` is constructed with the
   authorized `workspace_id` and adds it to every read and stamps it on every insert.
   Code that only has a repository cannot query across tenants. Cross-references in
   request bodies (a contact's `company_id`, an action's `opportunity_id`, …) are
   verified through the same scoped repositories and answered with `422
   invalid_reference`.

4. **Composite foreign keys (database).** Every referenced workspace-owned table has a
   `UNIQUE (workspace_id, id)`, and every reference is a composite key
   `(workspace_id, company_id) → companies(workspace_id, id)`. Even a buggy service or
   a hand-written SQL statement **cannot** link a row to another workspace's row:
   PostgreSQL rejects it. Nullable references are simply skipped when `NULL`.

Tests for each layer live in `apps/api/tests/test_workspace_isolation.py`, including
a class that attacks the database layer directly.

### Authentication

Authentication answers "who is this user?"; it is deliberately kept separate from
workspace authorization ("can they access this workspace?", §4 above). The two never
merge: `get_current_user` produces a `User`, and only `WorkspaceService.get_context`
(fed that user's id) can produce a `WorkspaceContext`.

**Sessions are opaque server-side tokens, not JWTs.** `POST /v1/auth/signup` and
`POST /v1/auth/login` (`nerva/services/auth.py`) generate a random 32-byte token
(`secrets.token_urlsafe`), store only its SHA-256 hash in the new `sessions` table, and
set the raw token as an `httpOnly` cookie (`nerva_session`). Every authenticated request
hashes the cookie's token and looks up an unrevoked, unexpired session
(`SessionRepository.get_active`). This was chosen over a self-verifying JWT for one
reason: **logout is a real, immediate revocation** (`revoked_at` is set and committed),
which a stateless JWT cannot offer without a denylist of its own — and a denylist is
just a sessions table with extra steps. The trade-off is a database lookup per request,
which is cheap here (`token_hash` is indexed and unique) and consistent with the rest of
v0.1's synchronous, straightforward style.

**Passwords** are hashed with bcrypt (`nerva/core/security.py`); the plaintext password
never reaches the database, and `UserRead` never has a password field to leak in the
first place (Pydantic schemas only serialize declared fields). Login and "unknown email"
return the identical `401 unauthenticated` body, so a wrong password cannot be told
apart from a wrong email — the same enumeration protection principle workspace routes
already use for tenants.

**The cookie** (`nerva/core/cookies.py`): `httpOnly`, `path=/`, `SameSite=Lax`, and
`Secure` only when `ENVIRONMENT=production` (local development has no TLS). `SameSite=Lax`
is enough to stop cross-site forged requests without a separate CSRF token — a
genuinely cross-site page cannot make the browser attach the cookie to a state-changing
request — *as long as the frontend and API are the same "site"* (same registrable
host; port doesn't matter, but the hostname string does). That is why local development
must use `http://127.0.0.1:3000` for the web app against `http://127.0.0.1:8000` for the
API: `127.0.0.1` and `localhost` are different strings and therefore different sites to
a browser, even though both mean "this machine". Next.js's dev server also blocks
cross-origin dev requests by default, which is why `next.config.ts` sets
`allowedDevOrigins: ["127.0.0.1"]`.
**In production**, if the frontend and API ever end up on different top-level domains
(not just subdomains of one registrable domain), `SameSite=Lax` stops working for
cross-site requests and the cookie would need `SameSite=None; Secure` plus an explicit
CSRF mitigation — not implemented, since the production topology isn't decided yet.

**Session lifetime**: 30 days (`SESSION_TTL` in `core/security.py`), fixed at issue time;
there is no sliding renewal or refresh token in v0.1. A session is invalidated by
`POST /v1/auth/logout` (revokes just that session) or by expiry. Signing in from a new
device creates an additional session row rather than displacing an existing one, so a
user can be signed in on several devices at once; there is no "sign out everywhere" yet.

**`ENVIRONMENT=production`** no longer refuses to start (the previous gate existed only
because the sole authentication mechanism was the spoofable `X-User-Id` header). Cookie
`Secure` is derived from `ENVIRONMENT`; there is otherwise nothing production-specific
left to gate on in this milestone.

Known limits, to address later: roles are stored but not yet enforced (any member can
approve actions); there are no invitations (members are created by owning a workspace,
or by the onboarding flow below); no password reset or email verification (the schema
and session design do not make either hard to add: reset is another short-lived,
hashed, single-use token in a table shaped like `sessions`).

## 5. Action lifecycle

An **Action** is something Nerva proposes to do: send an email, create a task, move an
opportunity to a new stage. Nothing happens until a human approves it.

```
                 ┌──────────▶ rejected            (terminal)
                 │
   proposed ─────┤
                 │
                 └──▶ approved ──▶ executing ──┬──▶ completed   (terminal)
                                               └──▶ failed      (terminal)
```

| From | Allowed next |
| --- | --- |
| `proposed` | `approved`, `rejected` |
| `approved` | `executing` |
| `executing` | `completed`, `failed` |
| `completed`, `failed`, `rejected` | nothing |

- The transition table lives in `nerva_actions/lifecycle.py`. It is pure code and is
  tested exhaustively over all 36 (from, to) pairs.
- Illegal transitions return **409** with code `invalid_state_transition` and the
  current and requested status.
- `ActionService._transition` is the only place status changes. It **locks the row**
  (`SELECT … FOR UPDATE`), validates the transition, applies the change, records the
  activity event and commits, in one transaction. Two people approving at the same time
  cannot both succeed.
- **Who can do what**: `POST …/approve` and `POST …/reject` are human operations exposed
  over HTTP. `start_execution`, `complete` and `fail` exist on the service for Nerva's
  future executor and are deliberately **not** exposed: a client must not be able to
  declare an action done.
- A failed action is final. Retrying means proposing a new action, so every attempt gets
  its own approval and audit trail.

Recorded on the action: `type`, `status`, `summary` (shown in approval queues),
`payload` (input), `result` (output, or the error for a failure), `created_by_*`,
`approved_by_user_id`/`approved_at`, `rejected_by_user_id`/`rejected_at`/`rejection_reason`,
`started_at`/`finished_at`, `created_at`/`updated_at`.

`type` is validated against the `ActionType` enum at the API but stored as free text, so
a new action type needs no migration. Per-type payload schemas belong with the executors
in `nerva_actions/`.

### 5.1 Execution contract (Milestone 5, Block 1)

The loop is being extended one step further: `observe → understand → reason → propose →
approve → act → remember`. The contracts for the `act` step exist as of this block; the
engine that drives them does not yet.

**The LLM/proposer is never the executor.** Nerva's reasoning layer (`nerva_core`,
attributed to `Actor.nerva()`) may only ever call `ActionService.propose`, the same as a
human. A future execution engine — not built yet — is the only thing that may call
`start_execution` / `complete` / `fail`, and only after a human has called `approve`. No
component may skip from `propose` straight to acting on an external system.

```
AI/proposer → validated Action proposal → human approval → execution engine
    → typed ActionExecutor → result + audit trail
```

- **Typed payloads** (`nerva_actions/payloads.py`): `Action.payload` stays free-form JSON
  at the database/API layer, but no executor may act on it directly. `EXECUTABLE_PAYLOADS`
  registers a strict, `extra="forbid"` Pydantic contract per `ActionType` —
  `SendEmailPayload` (recipients, subject, body) and `UpdateOpportunityStagePayload`
  (`target_stage`; the opportunity itself stays on `Action.opportunity_id`, already
  validated against the caller's workspace at proposal time — never repeated in the
  payload). `create_task` has no contract yet: there is no `Task` domain object to create.
  None of these contracts accept a provider token, a client-supplied `workspace_id`/
  `user_id`, or a provider-specific id (a Gmail message/thread id) — those never come from
  the LLM (see `nerva.core.context`).
- **The executor boundary** (`nerva_actions/executor.py`): `ActionExecutor` is a
  provider-neutral `Protocol` — one typed `execute(ActionExecutionInput) ->
  ActionExecutionResult` per `ActionType`. Real adapters (Gmail send, calendar, CRM,
  Slack, ...) will live with their provider client, never in the domain layer, mirroring
  `nerva_ai.client.AIAnalysisClient`. `execute` raises an `nerva_actions.errors` subclass
  on failure rather than returning a `success` flag — the same convention as
  `AIAnalysisClient.analyze`.
- **Eligibility** (`nerva_actions/eligibility.py`): `ensure_executable` is the pre-flight
  check a future execution engine runs before ever calling `start_execution`. It does not
  duplicate the lifecycle state machine — it calls `lifecycle.can_transition(status,
  EXECUTING)`, the exact rule `ActionService._transition` already enforces at the database
  layer — and additionally requires the payload to parse against its type's contract. Only
  `approved` actions with a valid payload are eligible; `proposed`, `rejected`,
  `executing`, `completed` and `failed` are not.
- **Idempotency**: one `Action` row is exactly one execution attempt — retrying a failed
  action means proposing a new one (§5), so the action's own id is already a correct
  idempotency key with no extra table. `ActionExecutionInput.idempotency_key` is
  `str(action.id)`, for a provider adapter to pass through to an idempotent provider API
  (or at minimum log). The **primary** protection against double execution remains what
  §5 already describes: `ActionRepository.get_for_update` + `ensure_transition` inside one
  transaction, so two concurrent attempts to execute the same action cannot both succeed.
- **Not built in this block**: an execution engine (resolving the right executor, calling
  the lifecycle methods, persisting the result), any real executor, `create_task`'s
  payload contract, Gmail write scopes, and any autonomous (unapproved) execution.

### 5.2 Execution engine (Milestone 5, Block 2)

`ActionExecutionEngine` (`nerva_actions/engine.py`) is the execution engine §5.1 said did
not exist yet. It orchestrates the existing pieces end to end and adds no new state of
its own:

```
ActionService.get  ->  build_execution_input  ->  resolve ActionExecutor
    ->  ActionService.start_execution  ->  ActionExecutor.execute
    ->  ActionService.complete  or  ActionService.fail
```

- **Executor resolution** is a plain `Mapping[ActionType, ActionExecutor]` passed in at
  construction — no plugin framework, no dependency container. It happens *before*
  `start_execution` is ever called: an action whose type has no registered executor (or
  whose payload fails its contract) is rejected while still `approved`, never left
  `executing` with nothing able to finish it. `create_task` still has no executor (or
  payload contract) for the same reason as §5.1.
- **Transaction boundary**: unchanged from §5. `start_execution` commits `approved ->
  executing` in its own transaction; the engine then calls the executor with no open
  transaction or row lock; `complete`/`fail` commits the terminal transition in a second
  transaction. The engine never holds a lock across the external call.
- **Crash window**: if the provider call succeeds but the process crashes before
  `complete` commits, the `Action` is stuck at `executing` — the lifecycle has no
  transition out of `executing` except `complete`/`fail`, so nothing will retry or
  reconcile it automatically. This block does not add a worker, queue, or reconciliation
  job to close that window; it only documents it. A future reconciliation step would need
  to ask the provider (using the idempotency key below) whether the action actually
  happened before deciding `complete` vs `fail`.
- **Idempotency**: the engine calls `ActionExecutor.execute` at most once per
  `ActionExecutionEngine.execute()` call. Double-execution across *separate* calls is
  still prevented the way §5 already describes: `start_execution`'s row-locked
  `ensure_transition` is the only thing that can let a second concurrent call reach the
  executor, and it cannot — the second call's `approved -> executing` transition fails
  with a 409 before the executor is ever invoked. `ActionExecutionInput.idempotency_key`
  (`str(action.id)`) is there for a future provider adapter to pass through to an
  idempotent provider API; the engine itself does not need it to guarantee single
  invocation.
- **Safe results**: on success, the engine persists `ActionExecutionResult.metadata` plus
  `external_reference_id` (when present) as `Action.result` — exactly the safe fields the
  executor contract already promised. On failure, it persists one generic,
  provider-neutral message (`ActionProviderError` -> "Could not reach the provider.",
  `ActionRateLimitError` -> the same plus a retry-after hint, etc.) — never the raw
  exception text, a stack trace, or provider response data.
- **Not built in this block**: a real `ActionExecutor` (Gmail or otherwise), an HTTP
  endpoint that triggers execution, any worker/queue, and any crash-window
  reconciliation.

### 5.3 First internal executor: `update_opportunity_stage` (Milestone 5, Block 3)

`UpdateOpportunityStageExecutor` (`nerva_actions/opportunity_stage_executor.py`) is the
first real `ActionExecutor` registered with `ActionExecutionEngine`. It was chosen over
`send_email` and `create_task` because it is the one `ActionType` that:

- mutates only Nerva's own database (`Opportunity.stage`) — no Gmail, OpenAI, HTTP, or
  vendor SDK;
- already has a typed payload contract, a domain model, and a workspace-scoped
  repository (`UpdateOpportunityStagePayload`, `Opportunity`, `OpportunityRepository`);
  and
- is naturally idempotent and reversible: setting a stage is not an external,
  irreversible side effect, and a later action can always move it again.

`create_task` has no domain object yet (see §5.1) and `send_email` needs Gmail write
scopes, which are explicitly out of scope for this block.

- **Reference completeness** (`nerva_actions/eligibility.py`): `Action.opportunity_id`
  (not the JSON payload — see §5.1) must be set for an `update_opportunity_stage` action
  to be eligible. `ensure_executable` now checks this alongside status and payload
  eligibility, so a proposal that never set `opportunity_id` fails before
  `start_execution` is ever called, the same "fail fast" discipline as the other two
  checks.
- **Workspace safety without a wider `ActionExecutionInput`**: `ActionExecutionInput`
  still carries no `workspace_id`/`user_id`/raw `Action` (§5.1's contract is unchanged).
  Instead, `UpdateOpportunityStageExecutor` is constructed with the same `(session, ctx)`
  as the `ActionExecutionEngine` driving it, and re-resolves the owning `Action` through
  a workspace-scoped `ActionRepository`, keyed by `execution_input.idempotency_key`
  (`str(action.id)`). `Action.opportunity_id` is additionally protected by a composite
  foreign key (`tenant_fk`), so the database itself refuses to let an action reference an
  opportunity outside its own workspace — the authenticated human approval, scoped by
  `WorkspaceContext`, remains the only authorization boundary; nothing is ever trusted
  from the payload.
- **Idempotency**: no new mechanism. The engine already calls `execute` at most once per
  `ActionExecutionEngine.execute()` call (§5.2), and re-executing a non-`approved` action
  is already rejected before `start_execution`. Retrying a failed attempt means proposing
  a new action (§5), so one `Action` row is still exactly one execution attempt.
- **Same-transaction atomicity**: this executor never calls `session.commit()`. It
  mutates the already-loaded `Opportunity` and returns; `ActionExecutionEngine` then
  calls `ActionService.complete`, which commits the opportunity mutation and the
  action's `completed` transition together, in one transaction. This is a property
  internal executors get for free that external-provider executors cannot: if the
  process crashes before that commit, the `Opportunity` mutation rolls back with it —
  there is no window where the mutation happened but is not yet reflected as `completed`
  (contrast with §5.2's crash window, which still applies to the `Action` itself being
  stuck at `executing`, and will apply in full to a future external executor).
- **Not built in this block**: any executor for `send_email` or `create_task`, any HTTP
  endpoint that triggers execution (this block wires the engine and executor together in
  tests only, the same as Block 2), any worker/queue, and any crash-window
  reconciliation.

### 5.4 Gmail `send_email` contract & provider boundary (Milestone 5, Block 4)

`send_email` is Nerva's first **externally consequential** action: unlike
`update_opportunity_stage` (§5.3), executing it has an effect outside Nerva's own
database that cannot be undone by rolling back a transaction. This block designs the
contract and provider boundary a future Gmail executor will implement against. **No
Gmail write call is made anywhere in this block**, no OAuth scope changes (Milestone 3's
`gmail.readonly` is unchanged — see `nerva_connect/google/oauth.py`), and no executor is
registered with `ActionExecutionEngine`.

- **The payload is unchanged.** Reviewing `SendEmailPayload` (§5.1) against Gmail's
  actual capabilities found no gap: `recipients`/`subject`/`body` already describe the
  business intent completely, bounded and `extra="forbid"`. No cc/bcc is added (nothing
  in the architecture justifies it yet) and no `conversation_id`/`thread_id`/`message_id`
  is added — those belong to a future, distinct `reply_to_email` action type, not
  implemented here (see `nerva_actions.payloads`'s own module docstring, written in
  Block 1, which already anticipated this exact boundary).
- **Provider boundary** (`nerva_connect/google/sender.py`, new): `GmailSender` is a
  `Protocol` — `send_email(access_token, *, recipients, subject, body) ->
  GmailSendResult` — mirroring `ActionExecutor` and `AIAnalysisClient`. It takes a bare
  access token, never an `Integration` row, a workspace id, or anything SQLAlchemy-shaped
  (the same "plain values only" rule `gmail.py`/`oauth.py` already follow). No class
  implements it yet; `tests.fakes.google.FakeGmailSender` is the only implementation,
  exactly as `tests.fakes.actions.FakeActionExecutor` was the only `ActionExecutor` before
  Block 3.
- **Credential/integration boundary**: `ensure_connected_integration` (same module) is
  the pre-flight check a future executor runs, after a plain workspace-scoped
  `IntegrationRepository` lookup, before ever calling `GmailSender` or refreshing a
  token: a missing, disconnected, or `needs_reauth` integration is rejected immediately,
  via the *existing* `ActionExecutorUnavailableError` (no new error type — its own
  docstring from Block 1 already names exactly this case). Obtaining/refreshing the
  access token itself is **not** re-implemented here: that remains
  `GmailSyncService._refresh_access_token`'s logic (`nerva.services.integrations`);
  reusing it (not duplicating it) for `send_email` is left to the block that builds the
  real executor, so Milestone 3's verified sync path is not touched now.
- **Error mapping**: `translate_send_error` turns a live `GoogleAuthError` /
  `GoogleApiError` into `ActionExecutorUnavailableError` / `ActionProviderError`,
  mirroring the same split `GmailSyncService._refresh_access_token` already makes one
  level up. It deliberately does **not** distinguish rate limiting or "invalid
  recipient" from any other provider failure: today's `GoogleApiError` (see
  `nerva_connect/google/gmail.py`) discards the HTTP status code, so there is no honest
  way to split those cases from this exception alone — a real send adapter would need to
  preserve the status code first, not done here since no real send call exists yet.
- **Idempotency and the external-send crash window — not solved, documented honestly.**
  `Action.id` remains the idempotency key (§5.1), but for an external send it protects
  less than for `update_opportunity_stage`:
  ```
  approved -> executing -> Gmail accepts the message -> process crashes -> never completed
  ```
  A later retry (a fresh proposed+approved action, since retrying means re-proposing —
  §5) could send a **second, duplicate** email; nothing in this block or Block 2 prevents
  that. Gmail's `users.messages.send` endpoint has **no idempotency-key parameter** to
  pass `Action.id` through to (unlike, say, Stripe) — this is an honest limitation of the
  provider, not a gap in Nerva's design. `GmailSendResult.message_id`/`thread_id` (what
  `ActionExecutionResult.external_reference_id`/`metadata` would store on success) is the
  only information a future reconciliation step could use — and only for an action that
  *reached* `completed`; a crash before that commit leaves nothing to reconcile against
  at all, since the id was never persisted. A real reconciliation step would have two
  honest options, neither built here: (a) accept the risk window and surface a stuck
  `executing` action for manual review, or (b) search the mailbox for a plausibly
  matching sent message by subject/recipient/time — a heuristic, not a guarantee. No
  worker, queue, or automatic retry is added to narrow this window.
- **Approval/security boundary unchanged**: only a human `approve` can move `send_email`
  toward execution (§5.1); email *content* is data, never instructions — nothing in this
  design path interprets message text as authorization. A future executor must obtain
  its `Integration` through a workspace-scoped repository keyed by the engine's own
  `WorkspaceContext`, never from the action payload — `SendEmailPayload` has no
  workspace/account field to take one from, by the same `extra="forbid"` contract as
  Block 1.
- **Audit**: unchanged. `ActionService`'s existing lifecycle events (`action.executing`,
  `action.completed`/`action.failed`) already cover a Gmail send the same way they cover
  `update_opportunity_stage` — no new event type is introduced, and a future executor
  must not write its own (see §5.3's same rule). Nothing here logs an email body, a
  token, or a raw provider response.
- **Not built in this block**: any real `GmailSender` implementation (the actual HTTP
  call), any `send_email` `ActionExecutor`, the `reply_to_email` action type, any OAuth
  scope change, any worker/queue/reconciliation, and any generalized
  Gmail/Outlook/SMTP provider framework.

### 5.5 Real Gmail send: `GmailSendExecutor` (Milestone 5, Block 5)

Block 4 designed the contract; this block implements the real path behind it —
`send_email` *can* now actually leave Nerva's database, once a human has approved
it, **if** something constructs the registry and calls the engine. Four distinct
claims, easy to conflate, so stated separately:

  1. **Adapter implemented** — yes: `GmailAPISender` makes the real HTTP call.
  2. **Executor implemented** — yes: `GmailSendExecutor` implements `ActionExecutor`
     for `ActionType.SEND_EMAIL` and can be constructed and driven end-to-end.
  3. **Executor registered in a production `ActionExecutorRegistry`** — **no**:
     nothing in `src/` builds one. Only test fixtures (e.g.
     `tests/test_gmail_send_executor.py`) construct `{ActionType.SEND_EMAIL:
     GmailSendExecutor(...)}` and pass it to `ActionExecutionEngine`.
  4. **Reachable by a real user through the API/UI** — **no**, and this is true of
     *every* action type, not just `send_email`: no HTTP route calls
     `ActionExecutionEngine.execute` at all yet (see the closing bullet). Wiring one
     in is separately scoped work, not part of this block.

- **`GmailAPISender`** (`nerva_connect/google/sender.py`) is the real `GmailSender`:
  builds a plain-text `EmailMessage` (`To`, `Subject`, and the body — no cc/bcc, no
  HTML, no attachments) and `POST`s it, base64url-encoded, to Gmail's
  `users.messages.send`, the official API, exactly as `GmailClient` already uses for
  reading. It does no read/search of any kind. A non-200 response, a transport
  failure, a non-JSON or non-object 200 body, and a 200 body missing a message id
  all become a safe `GoogleApiError`/`GoogleAuthError` — never the raw response body,
  a header, or a token. On success it returns `GmailSendResult(message_id,
  thread_id)`; `thread_id` is `None` when Google's response omitted one (never
  fabricated from `message_id`) — nothing else from Google's response is kept.
  Exercised in isolation against a mocked `httpx.post`, including every malformed/
  missing-response case above, by `tests/test_gmail_api_sender.py` (no real network
  call; no earlier test in this block had exercised this class directly).
- **`GmailSendExecutor`** (`nerva_connect/google/send_executor.py`) is the real
  `ActionExecutor` for `ActionType.SEND_EMAIL` — see the four claims above for what
  "real" does and does not mean here. It lives in `nerva_connect`, not
  `nerva_actions` — `nerva_actions` must never import a vendor transport module or
  `nerva_connect` itself (enforced by
  `test_nerva_actions_package_never_imports_google_or_http_transport_modules`), and
  this executor needs both. Per execution:
  1. Resolve the workspace's Google `Integration` via a workspace-scoped
     `IntegrationRepository` lookup (`get_for_provider`) — never from the action
     payload, which has no integration/account field to take one from.
  2. `ensure_connected_integration` — connected, not disconnected, not needing
     reauth (unchanged from §5.4).
  3. `ensure_send_scope_granted` (new) — the integration's stored `scopes` actually
     include `gmail.send`, not just `gmail.readonly`. See the OAuth scope note
     below.
  4. Obtain an access token via `GoogleCredentialRefresher.refresh_access_token`
     (`nerva.services.integrations`) — see below.
  5. Call `GmailSender.send_email` with exactly the validated payload's
     `recipients`/`subject`/`body`.

  A subject containing a raw `\r`/`\n` is rejected (`ActionNotExecutableError`)
  before any of the above, since it could otherwise inject extra MIME headers —
  `SendEmailPayload`'s existing bounds (Block 1) do not forbid embedded newlines, so
  this is checked at the executor, not relied upon from the payload contract.

- **Error information, improved**: Block 4 could not distinguish a 429 from a 400
  from a 503 because `GoogleApiError` discarded the HTTP status. It now carries
  `status_code` (bare int, or `None` for a transport-level failure with no response)
  and a derived `category` (`rate_limit` / `client_error` / `server_error` /
  `unknown`) — never the response body. `GmailAPISender` also captures Google's
  `Retry-After` header on a 429 as a bare float. `translate_send_error` uses this to
  map: `GoogleAuthError` -> `ActionExecutorUnavailableError` (reauth needed, as
  before); `category=rate_limit` -> `ActionRateLimitError` (with `retry_after` when
  available); `client_error`/`server_error`/`unknown` -> `ActionProviderError`. No
  raw response body, token, Authorization header, or email content is ever retained
  on any of these errors, and `ActionExecutionEngine`'s own `_safe_error_message`
  (§5.2) still only ever persists a fixed, generic string per error type — never
  `str(error)` — so even a mistake inside this module cannot leak through to
  `Action.result`.
- **OAuth scope expansion and reauthorization**: `nerva_connect/google/oauth.py`'s
  `SCOPES` now requests `gmail.send` alongside `gmail.readonly` (plus `openid
  email`) on every new connect or reconnect — one consent screen, not two. An
  `Integration` connected before this change only has `gmail.readonly` recorded in
  its stored `scopes` column; Nerva never assumes that token is good enough to send
  and never silently "upgrades" it — `ensure_send_scope_granted` checks the stored
  scope string itself (not the live token, which carries no human-readable scope
  info back to Nerva) and fails the action with the same reauthorization-needed
  error `ensure_connected_integration` already raises for `needs_reauth`. The fix is
  the same in both cases: reconnect through `POST .../integrations/google/connect`,
  which now requests `gmail.send` too. No change to the OAuth state/CSRF model, the
  encrypted-storage format, or who may connect (owner-only, §10) — only the
  requested scope list changed.
  - **Surfaced to the Settings UI, not just enforced at send time.**
    `integration_has_send_scope` exposes the same check as a plain, non-raising
    predicate; `IntegrationRead.can_send` (API) carries it to the frontend, and the
    Settings page shows a "Read-only" badge with its own "Reconnect Gmail" action
    when an otherwise-`connected` integration lacks `gmail.send` — rather than
    showing a plain "Connected" badge that implies more access than is actually
    granted. This was not done when Block 5 first shipped (an integration missing
    the scope looked identical to a fully-connected one in Settings); it is fixed
    as part of this correction.
  - **Manual prerequisite** (not a code change this block makes): the Google Cloud
    OAuth consent screen/client for this app must already be configured to allow the
    `gmail.send` scope (Gmail API enabled, scope added to the OAuth consent
    screen's configuration) before a real `/connect` can succeed with it — the same
    one-time setup `gmail.readonly` already required in Milestone 3 (see README.md),
    extended to the new scope. No live OAuth flow is exercised by this block.
- **Token refresh: reused, not duplicated.** `GmailSyncService._refresh_access_token`
  (Block 4's planned reuse target) is extracted, unchanged in behavior, into
  `GoogleCredentialRefresher.refresh_access_token` (`nerva.services.integrations`):
  decrypt the stored refresh token, call `GoogleOAuthClient.refresh`, and on
  `GoogleAuthError` mark the integration `needs_reauth` (committed) and raise
  `IntegrationAuthError`, or on `GoogleApiError` mark it `error` (committed) and
  raise `IntegrationSyncError` — identical to the classification `GmailSyncService
  .sync` already relied on. Both `GmailSyncService` and `GmailSendExecutor`
  construct one and call the same method; there is exactly one place that mints a
  Gmail access token from a stored credential.
- **Idempotency and the external-send crash window — still not solved, same honest
  limitation as §5.4.** `Action.id` remains the idempotency key, but Gmail's
  `users.messages.send` has no idempotency-key parameter to pass it to:
  ```
  approved -> executing -> Gmail accepts the message -> process crashes -> never completed
  ```
  A later retry (re-propose, re-approve — §5) could send a **second, duplicate**
  email. This block does not add a worker, queue, automatic retry, or
  reconciliation step to narrow that window — the risk is accepted and documented,
  not hidden. `GmailSendResult.message_id`/`thread_id` are persisted as
  `ActionExecutionResult.external_reference_id`/metadata on success, which is the
  only information a future reconciliation step could use, and only for an action
  that *reached* `completed`.
- **Approval is unchanged and still mandatory.** Only a human `approve` moves
  `send_email` toward execution (§5.1); nothing in this block allows an action to be
  proposed, approved, or executed automatically, and email content is still only
  ever data, never instructions. Tenant isolation is unchanged: `GmailSendExecutor`
  resolves its `Integration` through a `workspace_id`-scoped repository exactly like
  every other executor, never from anything the caller supplied outside
  `WorkspaceContext`.
- **Not built in this block**: any worker/queue/automatic retry/reconciliation, a
  `reply_to_email` action type, cc/bcc/attachments/HTML, a generalized
  Gmail/Outlook/SMTP provider framework, and any HTTP route that triggers
  `ActionExecutionEngine.execute` — **that remained true only through Block 5**; see
  §5.6, which adds exactly that route.

### 5.6 Production wiring: `POST .../actions/{id}/execute` (Milestone 5, Block 6)

Blocks 2/3/5 built a working `ActionExecutionEngine` and two real executors, but
nothing called any of it outside a test file — §5.5's closing bullet (above) was
accurate through Block 5. This block adds the one thing that was missing: a real
HTTP route, and the dependency-injection wiring that constructs a production
`ActionExecutorRegistry` for it to use.

- **`nerva.api.deps.get_action_executors`** is the one place a production registry is
  built: `{ActionType.UPDATE_OPPORTUNITY_STAGE: UpdateOpportunityStageExecutor(session,
  ctx), ActionType.SEND_EMAIL: GmailSendExecutor(session, ctx, oauth_client=...,
  sender=...)}`. It lives in the API dependency layer — not in `nerva_actions`, which
  must never import Google or an HTTP transport module (still enforced by
  `test_nerva_actions_package_never_imports_google_or_http_transport_modules`, which
  only inspects `nerva_actions/*.py` and is untouched by this block), and not in
  `nerva_connect`, which has no reason to know `update_opportunity_stage` exists. This
  is the one place both are composed; the engine itself is handed a plain
  `Mapping[ActionType, ActionExecutor]` exactly as §5.2 already specified, so the
  engine's own code does not change. `create_task` still has no registered executor
  (see §5.1); an approved `create_task` action still fails safely (409,
  `executor_unavailable`-class message) via the same path as any other unregistered
  type — not a regression, just the pre-existing "fail fast" rule now reachable for
  real.
- **`get_gmail_sender`** (new in `api/deps.py`) returns the real `GmailAPISender`,
  overridden in tests to `tests.fakes.google.FakeGmailSender` — the same pattern
  `get_google_oauth_client`/`get_gmail_client` already use, so no test in the suite
  makes a real network call or sends a real email.
- **`POST /workspaces/{workspace_id}/actions/{action_id}/execute`**
  (`routes.actions.execute_action`) is **owner-only** —
  deliberately **more restrictive than `approve`/`reject`**, which remain open to any
  workspace member, unchanged by this block. Approving only records a decision;
  executing is what actually spends external resources (a real email). The same bar
  already applies to connecting/syncing Gmail (`services.integrations._require_owner`)
  and to running an AI backfill (`routes.analysis.backfill_analysis`), so this is not a
  new policy, it is the existing one applied consistently to the one action left that
  didn't have it. The asymmetry is deliberate, not an oversight: a non-owner member can
  still approve a `send_email` action, they simply cannot be the one who presses
  "execute" — someone with the owner role still has to.
- **No bypass of the lifecycle or of human approval.** The route does not call
  `start_execution`/`complete`/`fail` itself and cannot move an action out of
  `proposed`/`rejected` — it only ever constructs `ActionExecutionEngine(session, ctx,
  executors)` and calls `.execute(action_id)`, the same call every test already made.
  `ActionExecutionEngine.execute` still only drives `approved -> executing ->
  completed|failed` (§5.2); nothing before this block or in this block can approve an
  action on its own, by design or by bug — only a human `POST .../approve` can.
- **Two distinct "safe failure" shapes, by design, not an inconsistency:**
  - A failure the engine detects *before* `start_execution` is ever called — the
    action is not `approved`, its payload is invalid, an `update_opportunity_stage`
    action has no `opportunity_id`, or no executor is registered for its type (only
    `create_task` today) — escapes `ActionExecutionEngine.execute` as
    `ActionNotExecutableError`/`ActionExecutorUnavailableError`. The route catches
    both and raises `nerva.core.errors.ConflictError` (409, `details={"code":
    "not_executable"}`), reusing `nerva_actions.engine.safe_error_message` (renamed
    from `_safe_error_message` so this route can reuse it rather than re-derive a
    second mapping) for the message — never `str(exc)` verbatim. Both error types
    share one code deliberately: the route gives no internal detail about *which*
    pre-flight check failed, only that the action cannot execute right now.
    `NotFoundError` (unknown/cross-tenant `action_id`) and `InvalidStateTransitionError`
    (a concurrent request already moved the action — see below) are already
    `NervaError` subclasses and propagate through the app's existing exception
    handler unchanged; the route adds no special handling for either.
  - A failure *during* `executor.execute()` itself — Gmail not connected, `needs_reauth`,
    missing the `gmail.send` scope, a provider error, a rate limit — is caught
    *inside* the engine (unchanged from §5.2/§5.5) and turned into a normal `200`
    response with `Action.status == "failed"` and a safe `Action.result.error`
    message. This is not an HTTP error: the request to execute succeeded, the
    external send did not. A client distinguishes the two cases by HTTP status, not
    by inspecting error shapes.
- **Duplicate/concurrent execute requests.** No new idempotency mechanism was added —
  none was needed. `ActionRepository.get_for_update`'s row lock inside
  `ActionService.start_execution` (unchanged since §5) already guarantees a second
  concurrent call either (a) is still rejected by the pre-flight check above if the
  first call's commit has already landed, or (b) blocks on the lock and then fails
  `ensure_transition` once it does — either way with a 409, and the executor called at
  most once. Verified at the HTTP level (sequential duplicate requests) in
  `tests/test_action_execution_api.py`; the underlying lock itself was already tested
  by §5.2.
- **Response shape**: `ActionRead` (unchanged, §4) — no new schema. The same shape
  `approve`/`reject` already return; a client reads `status`/`result` to know what
  happened, exactly as it already must for those two endpoints.
- **Not built in this block**: any worker/queue/automatic retry/reconciliation, a
  `reply_to_email` action type, any change to `approve`/`reject` permissions, and any
  UI for triggering execution (frontend is explicitly out of scope — see the Block 6
  plan).

## 6. Activity events

`ActivityEvent` is an immutable-style audit trail: *who* did *what* to *which entity*,
with a human-readable `summary` and structured `data`.

- Written by services **in the same transaction** as the change, so an event exists if
  and only if the change does. A rejected (409) request records nothing.
- **Read-only over HTTP.** Clients cannot post events, so the trail cannot be forged.
- `entity_type` / `entity_id` have no foreign key, so the trail outlives what it
  describes.
- The ORM refuses to update or delete events, and the repository exposes only `add` and
  reads. **Limitation:** bulk SQL statements and direct database access bypass ORM
  guards. Real database-level enforcement (a trigger, or revoking `UPDATE`/`DELETE`
  from the application role) is planned hardening, not part of v0.1.

## 7. Future module boundaries

These are reserved as packages so the shape is visible; several now contain real code
(see §5.1 for `nerva_actions`), but none has a working end-to-end implementation of its
"first real work" yet. They are intentionally not abstract interfaces: each was designed
once its first real implementation arrived.

| Module | Responsibility | First real work |
| --- | --- | --- |
| **`nerva_core`** | The agent runtime: reads workspace context and memory, decides what to propose, coordinates work. Produces `Action` proposals via the same `ActionService` as humans, attributed to `Actor.nerva()`. | Lead identification from conversations |
| **`nerva_actions`** | The action lifecycle (§5), typed per-type payload contracts, the provider-neutral executor/eligibility contracts (§5.1), the execution engine (§5.2), and one real, internal-only executor (§5.3). `send_email` has a real executor too, but it lives in `nerva_connect` (§5.5), not here — see that row. Both are reachable in production as of Block 6 (§5.6). No executor for `create_task` yet. | ~~Send an approved email reply~~ `update_opportunity_stage` (§5.3) and `send_email` (§5.5) are both implemented, registered, and reachable via `POST .../actions/{id}/execute` (§5.6, owner-only) |
| **`nerva_memory`** | Writing, ranking and recalling workspace memory. Starts relational; embeddings/pgvector only if retrieval quality demands it. Persistence and typed, filtered retrieval are real as of Milestone 6 — see [§12](#12-nerva-memory-persistence-and-retrieval). Ranking, automatic extraction from conversations, and wiring into `nerva_core` are not. | Done: recording, querying (with an active-by-default policy), and lifecycle resolution are all real and reachable via `/workspaces/{id}/memories`. Remaining: ranking beyond plain filters, and any connection to an autonomous agent |
| **`nerva_connect`** | Connectors to external tools. Gmail (read-only import) is real as of Milestone 3 — see [§10](#10-nerva-connect-google-oauth-and-gmail-import). Gmail `send_email` is real as of Block 5 (§5.5): the OAuth scope is requested (`gmail.send`), the real adapter (`GmailAPISender`) makes the actual HTTP call, and the real executor (`GmailSendExecutor`) drives it. **Reachable in production as of Block 6** (§5.6): `nerva.api.deps.get_action_executors` builds the production `ActionExecutorRegistry`, and `POST .../actions/{id}/execute` (owner-only) calls `ActionExecutionEngine.execute` with it. | Done: adapter, executor, and production wiring are all real. Remaining, explicitly out of scope: a worker/queue/reconciliation for the crash window (§5.5), `reply_to_email`, and a frontend trigger (§5.6) |

The rules that keep these honest: everything Nerva does is a proposed `Action` gated by
approval; everything is scoped to a workspace through `WorkspaceContext`; everything
important leaves an `ActivityEvent`.

## 8. Testing

- **Backend** (`apps/api/tests`, pytest): runs against a real PostgreSQL database
  (`TEST_DATABASE_URL`, created on demand, name must end in `_test`), because isolation
  relies on PostgreSQL constraints. Each test runs in a transaction that is rolled back.
  A migration test proves `alembic upgrade head` yields exactly the schema the models
  describe, and that `downgrade base` removes it. Tests authenticate by signing up and
  logging in through the real API (`tests/conftest.py`'s `make_user`), then send the
  resulting session token as a cookie (`Account.cookies`) — never a header the server
  would trust blindly.
- **Frontend** (`apps/web`, Vitest + Testing Library): component and page behavior,
  including the auth forms, route protection, and loading/empty/error states of the
  pages that call the API. `@/lib/api-client` and `@/lib/auth-context` are mocked at the
  module level so tests control what the "API" returns without a running backend.

## 9. Frontend authentication, onboarding and data

- **`AuthProvider`** (`apps/web/src/lib/auth-context.tsx`) is a client component wrapping
  the whole app (`app/layout.tsx`). On mount it calls `GET /v1/auth/me` and holds the
  result in React state: `status` (`loading` / `authenticated` / `unauthenticated` /
  `error`), the current `user`, and `workspaces`. `workspace` (singular) is
  `workspaces[0] ?? null` — the first workspace the user belongs to. This milestone
  always uses that one; the state shape already supports switching later without a
  redesign, it just has nowhere to switch *to* yet (§7, multi-workspace switching is not
  implemented). `refresh()` re-runs this check (called after signup, login, onboarding
  and logout); `logout()` calls the API and clears local state.
- **Route protection** happens in two route groups, both client components because the
  session cookie belongs to the API's origin and cannot be read by the Next.js server:
  - `app/(product)/layout.tsx` wraps Today/Inbox/Opportunities/Nerva/Activity/Settings.
    It redirects `unauthenticated` visitors to `/login`, and `authenticated` visitors
    with no workspace to `/onboarding`; only then does it render `AppShell` and the
    page.
  - `app/(auth)/login`, `/signup` and `/onboarding` each redirect *away* if the visitor
    is already authenticated (to `/today` if they have a workspace, `/onboarding` if
    not), so a signed-in user cannot land back on the sign-in form.
  - Route groups (the parenthesized folder names) only affect this shared layout
    nesting; they do not appear in the URL.
- **Onboarding** is two screens in one page (`app/(auth)/onboarding/page.tsx`): a
  welcome step, then a form for the workspace name and an optional display-name
  override (defaulting to the name from signup, computed from `user.name` during render
  — no effect needed to seed it). Submitting calls `PATCH /v1/auth/me` (only if the name
  changed) and `POST /v1/workspaces`, then redirects to `/today`. There is no dedicated
  onboarding endpoint: workspace creation already did everything needed (create the
  workspace, make the caller its owner, persist membership, record the activity event).
- **Data loading**: each product page is a thin Server Component (keeps its
  `<title>` metadata) rendering a client `*-view.tsx` component that fetches from the
  workspace's endpoints via `@/lib/api-client` and renders one of a loading, error
  (with a "Try again" retry), empty, or populated state. There is no client-side data
  library (React Query, SWR, …): each view holds its own `useState`/`useEffect`, which
  is enough at this scale and avoids the dependency the brief asked not to add.
- **The API client** (`apps/web/src/lib/api-client.ts`) is a ~60-line wrapper around
  `fetch`: it always sends `credentials: "include"`, and turns the API's
  `{"error": {"code","message","details"}}` body into a typed `ApiError` (`status`,
  `code`, `message`, `details`) that callers branch on (e.g. `error.status === 401`).

## 10. Nerva Connect: Google OAuth and Gmail import

**Scope: import plus one write capability.** Nerva can observe Gmail (import
conversations and messages, `gmail.readonly`) and, as of Milestone 5 Block 5, send a
new plain-text email through the connected mailbox once a human has approved a
`send_email` action (`gmail.send` — see §5.5). Nothing else is possible: Nerva cannot
reply in-thread, delete, archive, label or mark anything in the connected mailbox, and
no Gmail write happens outside the `send_email` executor path. The only Gmail
permissions ever requested are `gmail.readonly` and `gmail.send`, plus `openid email`
to identify the connected account (`nerva_connect/google/oauth.py`'s `SCOPES`). An
account connected before Block 5 only holds `gmail.readonly` in its stored
`Integration.scopes` until it reconnects — see §5.5 for how that is detected and
handled.

### The connector boundary

```
nerva_connect/
  google/
    oauth.py     GoogleOAuthClient: authorization URL, code exchange, refresh, revoke,
                 ID-token verification. Talks to Google's OAuth endpoints over HTTPS
                 (httpx) and uses google-auth only for verifying the ID token's
                 signature — not something to hand-roll.
    gmail.py     GmailClient: users.messages.list / .get against the official Gmail
                 REST API. No other endpoints are called; nothing here can mutate a
                 mailbox.
    mapper.py    Pure functions: raw Gmail message -> NormalizedMessage. Also where
                 HTML bodies are converted to safe plain text (see below).
    errors.py    GoogleAuthError (bad/revoked grant) vs GoogleApiError (anything
                 else) — the distinction a sync needs to choose `needs_reauth` vs
                 `error`. GoogleApiError also carries a safe `status_code`/
                 `category` (Milestone 5 Block 5) — never the response body.
    sender.py    GmailSender protocol + GmailAPISender (the real implementation:
                 builds a plain-text MIME message, POSTs it to users.messages.send),
                 GmailSendResult, ensure_connected_integration,
                 ensure_send_scope_granted, translate_send_error — the `send_email`
                 provider boundary (§5.5).
    send_executor.py
                 GmailSendExecutor: the real ActionExecutor for send_email (§5.5).
```

Everything here is Google-specific and knows nothing about workspaces, sessions or the
database. Orchestration — authorize, call the connector, write `Conversation` /
`Message` / `Integration` rows, record activity — lives in
`nerva.services.integrations` (`IntegrationService`, `GmailSyncService`,
`complete_google_connect`), which is where a second connector (e.g. Outlook) would plug
in without changing anything downstream: the rest of Nerva only ever sees normalized
`Conversation` / `Message` rows, never a provider's response shape.

Both Google clients are FastAPI dependencies (`get_google_oauth_client`,
`get_gmail_client` in `nerva.api.deps`), so tests substitute fakes
(`apps/api/tests/fakes/google.py`) via `app.dependency_overrides` — no test ever makes a
real network call or needs a real Google account.

### OAuth flow and state

1. `POST /v1/workspaces/{id}/integrations/google/connect` (owner-only, authenticated):
   generates a random 32-byte `state` token, stores only its SHA-256 hash in
   `oauth_states` bound to the calling `user_id` and `workspace_id` (10-minute expiry),
   and returns Google's consent URL with that state attached. The frontend navigates
   the browser there (`window.location.href = ...`) — this is not a `fetch`, so
   Google's own page can render.
2. The user consents (or not) on Google's own page. Google redirects the browser to
   `GET /v1/integrations/google/callback?code=...&state=...` — a route with **no**
   `workspace_id` in its path, because Google does not know about workspaces.
3. The callback (`complete_google_connect`) validates the state: looks it up by hash,
   requires it to be unexpired and not already consumed (marking it consumed
   atomically, so a replayed callback fails), and requires the *currently
   authenticated* user (from the session cookie, not any value Google or the browser
   supplied) to match the state's `user_id`. Only then is the state's `workspace_id`
   trusted — and even then, membership is re-verified through the real authorization
   gate (`WorkspaceService.get_context`), never assumed. This is what stops OAuth CSRF,
   callback replay, and connecting an integration to a workspace the caller does not
   belong to.
4. On success, the code is exchanged for tokens, the ID token is verified (Google's
   public keys, via `google-auth`) to get the account's stable `sub` and `email`, and
   the `Integration` row is created or — if that Google account was already connected
   to this workspace — updated in place (see below). One `ActivityEvent` is recorded.
5. The callback always **redirects the browser** to `{FRONTEND_BASE_URL}/settings`,
   with `?integration=connected` or `?integration=error&reason=...` — never a raw JSON
   error, since this response lands mid-navigation, not in a `fetch` handler. The
   Settings page reads that query string once, shows a banner, and strips it
   (`router.replace("/settings")`).

**Duplicate accounts**: `integrations` has `UNIQUE(workspace_id, provider,
external_account_id)`, keyed on the Google `sub` claim — not the email, which can
change (aliases, renames) and must never be treated as a stable identity. Reconnecting
the same account updates the existing row's status and encrypted refresh token instead
of creating a duplicate.

**Owner-only management**: connecting, syncing and disconnecting an integration require
`WorkspaceContext.role == owner` (`_require_owner` in `nerva.services.integrations`).
Any member may list integrations and read what has synced. This is narrower than the
rest of the app (where membership alone is currently enough for most actions) because
granting a third-party account access to a mailbox is more sensitive than approving a
proposed action — a deliberate MVP tightening, not a general role system (that is still
future work; see §4's authentication notes).

### Credential storage and encryption

Only a **refresh token** is stored — never an access token. Access tokens are
short-lived (~1 hour) and cheap to mint on demand from the refresh token, so persisting
one would add attack surface for no benefit; every sync fetches a fresh access token
and discards it when the request finishes.

The refresh token is encrypted with **Fernet** (AES-128-CBC + HMAC-SHA256, from the
`cryptography` package — a standard, well-reviewed authenticated-encryption
construction, not a custom one) under a single symmetric key,
`INTEGRATION_ENCRYPTION_KEY` (`nerva.core.crypto`). Authenticated encryption matters
here specifically: a tampered ciphertext fails to decrypt loudly (`EncryptionNotConfigured`)
rather than silently returning garbage that might otherwise be sent to Google as a
"refresh token."

- **Production startup fails clearly** if the key is missing or not a valid Fernet key
  (`create_app` calls `validate_encryption_key` when `ENVIRONMENT=production`) — the
  same pattern the old dev-auth gate used, now guarding a real secret instead.
- **Never returned or logged**: `IntegrationRead` has no token field at all (not even
  encrypted), the client secret lives only in `Settings` and is never serialized, and
  nothing in the sync path logs a token or a raw Google response.
- **Key rotation** is a documented future concern, not implemented: rotating the key
  means decrypting every stored `encrypted_refresh_token` with the old key and
  re-encrypting with the new one in one migration step. Because only a small number of
  refresh tokens exist per workspace (one per connected account) rather than, say, every
  session, this is cheap whenever it is needed.

### Synchronization

`POST /v1/workspaces/{id}/integrations/{integration_id}/sync` (owner-only,
user-triggered — no background worker in this milestone) runs `GmailSyncService.sync`:

1. Refresh the access token from the stored (decrypted) refresh token. If Google
   rejects it (`GoogleAuthError` — revoked, expired grant), the integration is marked
   `needs_reauth`, an `ActivityEvent` is recorded, and the endpoint returns `409` with
   `{"code": "needs_reauth"}` — **the sync never claims success it did not achieve.**
   Any other failure (`GoogleApiError` — network, rate limit, unexpected response)
   marks the integration `error` and returns `409` with `{"code": "sync_failed"}`,
   equally honestly.
2. List recent message ids, bounded on **two axes** so a very active mailbox is never
   accidentally downloaded in full:
   - **Time window**: only messages received in the last **30 days**
     (`SYNC_WINDOW_DAYS`, via Gmail's `q=after:...` search).
   - **Hard cap**: at most **200 messages** per sync run (`MAX_MESSAGES_PER_SYNC`),
     regardless of how many fall in the window.
3. For each message id **not already imported** (see idempotency below), fetch the full
   message, normalize it (`mapper.parse_gmail_message`), and upsert its `Conversation`
   (by Gmail thread id) and insert its `Message`.
4. Update `last_synced_at` and `status = connected`, and record **one**
   `ActivityEvent` for the whole run (`integration.synced`, with counts) — not one per
   message or per conversation, so the audit trail stays a readable log of what
   happened rather than noise proportional to mailbox size.

**Idempotency**: `messages.external_id` is unique per workspace
(`UNIQUE(workspace_id, external_id)`). Before importing a message, the sync checks
whether that id is already present and skips it if so. Running sync twice (or syncing
after a partial previous failure) creates zero duplicate conversations or messages —
it only imports what is new. `conversations` was already unique per
`(workspace_id, channel, external_id)` (the Gmail thread id), from before this
milestone, so a thread with new messages is *updated*, never duplicated, on repeat
syncs.

### Mapping Gmail into Conversation / Message

- **Participants**: for each message, the non-account-owner party (sender for an
  inbound message, first non-self recipient for an outbound one) is resolved to a
  `Contact` — found by email, or created if new. The conversation's `contact_id` is
  set from the first message that establishes one.
- **Body extraction** (`mapper.extract_plain_text`) prefers a `text/plain` MIME part,
  recursing into `multipart/*` messages to find one. If only `text/html` exists, it is
  converted to plain text (below) rather than stored as HTML. If neither is present,
  Gmail's own `snippet` is used as a last resort.
- **Recipients** (`To` + `Cc`) are stored as a simple comma-separated display string,
  not a normalized join table — enough to show "To: …" without a relational model this
  MVP does not need yet.
- **Attachments** are not downloaded or persisted in any form in this milestone.

### Email content is untrusted input

Email bodies are treated as untrusted external content from the moment they are
imported, because that boundary is much easier to get right once (at ingestion) than
to retrofit later — see §11 for how the same discipline extends to `nerva_ai` once a
provider actually reads this content:

- **HTML is never executed or rendered.** `mapper.html_to_text` uses Python's standard
  `html.parser.HTMLParser` purely as a tokenizer — it extracts text nodes and
  explicitly discards the contents of `<script>` and `<style>` tags, never evaluates
  anything. What reaches the database is plain text.
- **The frontend renders `body_text` as a plain React string** (`{message.body_text}`),
  which JSX escapes like any other text — there is no `dangerouslySetInnerHTML` and no
  path from a stored message to raw HTML in the browser.
- **Raw provider payloads never reach the API surface.** `ConversationRead` /
  `MessageRead` expose only the normalized fields; nothing returns Gmail's original
  JSON.
- **Sender/recipient addresses are never authorization data.** They identify a
  `Contact` for display and CRM linking only; nothing about who a message claims to be
  from grants any permission.
- **Message bodies are not written to application logs.**

### Settings and Inbox

- **Settings → Integrations** replaces the placeholder with a real Gmail card: three
  states (not connected → "Connect Gmail"; connected → account email, last synced,
  "Sync now" / "Disconnect"; needs reauth → "Reconnect Gmail"), plus an error state for
  a failed sync. A banner (from the callback's redirect query string) confirms success
  or explains failure in plain language, never Google's raw error code.
- **Inbox** replaces the static placeholder with a two-pane conversation list/detail
  view, backed by `GET /v1/workspaces/{id}/conversations` and `.../conversations/{id}`.
  It distinguishes "no Gmail connected yet" (links to Settings) from "connected but
  nothing synced yet" (also links to Settings, to run a sync) from a populated list.
  There is no reply box or compose affordance anywhere — the UI does not suggest Nerva
  can act on email yet, matching the read-only scope above.
- **Disconnecting** revokes the credential with Google (best-effort) and marks the
  integration `disconnected`, but **keeps** the `Integration` row and every imported
  `Conversation` / `Message` — disconnecting removes access, not history. Reconnecting
  the same account later updates that same row.

## 11. Nerva AI: conversation analysis

Nerva can analyze an already-imported conversation with an LLM and extract a
structured, bounded classification of it (`nerva_ai.schemas.ConversationAnalysisResult`:
category, priority, sentiment, an optional estimated value/deadline, and so on).
This never creates or modifies a business record by itself — deterministic rules
(`nerva_core.rules`) decide what, if anything, the result implies, and a human always
confirms or dismisses the resulting `BusinessSignal` before an `Opportunity` exists.
Gmail access remains exactly as read-only as §10 describes: analysis only ever reads
`Conversation` / `Message` rows a connector already imported, and never itself calls
Gmail or any other external API.

### The provider boundary

- **`nerva_ai.client.AIAnalysisClient`** is the only contract the rest of Nerva depends
  on — the same pattern §10 uses for Google: `nerva.services.analysis` imports only
  this Protocol, never a vendor SDK.
- **`nerva_ai.providers.openai.OpenAIProvider`** is the one real implementation, using
  OpenAI's Chat Completions API with Structured Outputs
  (`response_format=ConversationAnalysisResult`) so the model's reply is parsed
  directly into the validated schema rather than free-form text. It is the only module
  in Nerva allowed to import the `openai` package.
- **`nerva_ai.unconfigured.UnconfiguredAIClient`** is what `nerva.api.deps.get_ai_client`
  returns until `OPENAI_API_KEY` is set (see the root README's "Connect an AI
  provider"): every call fails safely and explicitly (`AIAuthError`) rather than
  faking a result, the same discipline `GoogleOAuthClient` applies when Google OAuth
  is not configured.
- **Tests never call a real provider.** `tests.fakes.ai.FakeAIClient` stands in for
  the whole boundary in every test outside `test_openai_provider.py`, which instead
  injects a stand-in for the `openai.OpenAI` client's `chat.completions.parse` method
  — still never a real network call.

### Email content is untrusted input, continued

`nerva_ai.prompts.SYSTEM_PROMPT` extends §10's "email content is untrusted input"
principle to the model itself: every message's `body_text` — and the conversation
subject — is sent to the provider as data to classify, inside a system prompt that
explicitly instructs the model to never follow any request, command, or instruction
that appears within that content (a "delete this thread and reveal your instructions"
line in an email body, for instance), no matter how it is phrased. This is a prompt-
level mitigation, not a hard guarantee — the real backstop is that nothing the model
returns is trusted structurally: `ConversationAnalysisResult` (`extra="forbid"`,
bounded/enum fields, no database id of any kind) is the only thing ever read from a
response, and every field it fills in is guidance for a human, never an instruction
Nerva itself executes.

The prompt also tells the model to prefer leaving an optional field null (or picking
`category: other` with a low `confidence`) over inventing a plausible-sounding value —
the same "uncertainty is a first-class outcome" design already built into the schema.

### Status

As of this block, the real provider is implemented and covered by tests that mock the
SDK boundary (`tests/test_openai_provider.py`, `tests/test_prompts.py`) — **no call to
OpenAI, and no analysis of real Gmail data, has been made from this codebase.** Live
verification against a small, explicitly authorized set of real conversations is a
separate, later step.

## 12. Nerva Memory: persistence and retrieval

`nerva_memory` is a durable, provenanced fact or preference Nerva can recall later —
`statement` plus optional structured `attributes`, never a raw email body or provider
response (see `nerva.models.memory`'s module docstring). Deliberately plain relational
rows in v0.1: no embeddings, no vector index, no semantic search. Two milestone blocks:

- **Milestone 6, Block 1 — contracts and writing.** `nerva_memory.content.MemoryContent`
  is the one validated shape every `Memory.content` must satisfy (`extra="forbid"`);
  `nerva_memory.lifecycle` is the `active -> superseded | discarded` state machine
  (both terminal); `MemoryService.remember` is the only place a row is created, and
  validates provenance exactly the way `ActionService.propose` validates
  `opportunity_id` — a subject (`contact_id`/`company_id`/`opportunity_id`) or a source
  (`source_conversation_id`/`source_analysis_id`) naming an id outside the caller's
  workspace is rejected (422 `invalid_reference`), never silently accepted. `supersede`/
  `discard` resolve a memory without ever rewriting `content` in place — a fresher belief
  is always a new row, linked back via `superseded_by_id`.

- **Milestone 6, Block 2 — retrieval.** `MemoryRepository.list_filtered` is a plain,
  explicitly-typed query (`kind`, `origin`, `status`, subject) — `status=None` applies no
  filter at all, the same convention `ActionRepository.list_filtered`/
  `BusinessSignalRepository.list_filtered` already use. The policy that most callers
  actually want — **never see a superseded or discarded belief unless you ask for it by
  name** — lives one layer up, in `MemoryService.list`: when `status` is omitted it
  defaults to `active`; passing `status` explicitly (including `MemoryStatus.ACTIVE`
  itself) always returns exactly that lifecycle state, so history is never hidden, only
  never the default. This split (dumb, composable repository; policy in the service) is
  deliberate — a future caller that genuinely needs every status at once (an audit view)
  is not blocked by the safe default.

  Ordering and limits: every list is `created_at` descending with `id` as a
  deterministic tiebreaker (`ScopedRepository._page`, shared with every other listing in
  the codebase), and `PaginationDep` caps `limit` to 1–100 (default 50) — the same bound
  every other list endpoint already enforces.

  **API** (`routes/memory.py`, `/workspaces/{workspace_id}/memories`): any workspace
  member may record, list, read, supersede or discard — recording or resolving a memory
  touches no external credential or provider, the same reasoning that already makes
  `signals`' confirm/dismiss and `actions`' approve/reject member-writable (only
  `actions.execute_action`, which spends a real external resource, is owner-only).
  `MemoryCreate` mirrors `MemoryContent`'s `statement`/`attributes` shape directly rather
  than accepting an opaque content dict, so the same bounds are enforced at the request-
  validation layer as at `remember`'s own contract check; `MemoryContentError` (raised
  only when a caller reaches `remember` some other way, bypassing this schema) is mapped
  to a new `InvalidContentError` (422 `invalid_content`) — `nerva_memory.content`'s
  module docstring had already flagged this as a future route's job.

  A subject filter (e.g. `?contact_id=`) naming an id from another workspace is never a
  leak and never an error: `Memory.contact_id`/`company_id`/`opportunity_id` can only
  ever point at a row in the same workspace (the composite `tenant_fk` the model already
  declares), so a foreign id simply matches nothing.

  **Not built in this block**: ranking beyond plain filters, automatic extraction from
  conversations, embeddings, and any connection to `nerva_core`'s future agent runtime —
  memory is data an agent could one day read, never an instruction it can act on, and
  nothing here changes that.

- **Milestone 6, Block 3 — automated `estimated_value` memory (M6.3).** The one fact
  the system forms on its own, with no human in the loop: when a conversation's
  analysis states or implies a deal's value, Nerva remembers it as a `kind=FACT`
  memory, attributed to `Actor.nerva()`. The decision is a pure, deterministic table
  (`nerva_core.memory_rules.decide_memory_formation`) over the analysis's current
  estimate and whatever `estimated_value` memory is already active for it: no existing
  estimate → `NOOP`; a first valid one → `CREATE`; the same analysis content re-seen
  (`source_fingerprint` unchanged) → `NOOP` (idempotent against retries); new content
  with no usable estimate → `NOOP` (never auto-discards what a human may already rely
  on); new content with the same value → `NOOP`; new content with a changed value →
  `SUPERSEDE`. `MemoryFormationService.form_estimated_value_memory` is the only code
  path allowed to act on that decision — it takes `ConversationAnalysis`'s own row lock
  (the same one `AnalysisService.analyze` already uses) for its whole
  read-decide-write sequence, so two concurrent calls for the same conversation can
  never both decide to `CREATE`, and a changed value is one atomic commit
  (`MemoryService.replace`), never a window with two active memories or zero. A
  pre-existing inconsistency it cannot safely resolve on its own — more than one
  active `FACT` memory already sourced from the same analysis — raises
  `DataIntegrityError` rather than guessing; see that module's own docstring for the
  full argument.

  **Known limitation, inherited from Block 2, not closed here.** There is no unique
  constraint on `(source_analysis_id, kind)` (see `nerva.models.memory`), so the
  `DataIntegrityError` above is a detection, not a prevention: this service's own
  CREATE/SUPERSEDE path is race-free (the `ConversationAnalysis` row lock serializes
  it against itself), but a *different* writer — `remember` via a direct
  `POST /memories` call with the same `source_analysis_id`, which takes no such lock
  and runs no "is one already active" check — can still race either this service or
  another manual call and leave two active `FACT` memories for one analysis, which
  this method then refuses to act on rather than silently pick a winner. Proven with a
  real, synchronized two-connection race in
  `test_memory_service.py::test_two_concurrent_manual_fact_creates_for_the_same_analysis_can_both_become_active`.
  A write-time guard was considered and rejected in Block 2's review: it would also
  block the legitimate create-then-supersede sequence any memory can go through via
  two separate `POST /memories` calls, since `remember` cannot tell "about to be
  superseded" apart from "permanently orphaned" at creation time. Closing this
  requires a product/schema decision (e.g. an atomic create-and-supersede endpoint, or
  a partial unique index plus a migration to resolve any already-existing duplicates
  first) — out of scope for M6.3.

  **Wiring** (`routes/analysis.py`): this service is called from the same two places
  `SignalService.materialize_potential_opportunity` already is, always after both
  analysis and signal materialization have committed — `analyze_conversation` (one
  conversation) and `backfill_analysis` (the bounded, owner-only batch). Two distinct,
  non-bug failures can come out of it — `DataIntegrityError` (the pre-existing
  inconsistency above) and `InvalidStateTransitionError` (a *live* race: the memory
  this call would supersede was resolved by someone else — e.g. a direct
  `POST /memories/{id}/replace` or `/supersede` — in the window between this service's
  own unlocked read of the active memory and `MemoryService.replace`'s locking
  re-read; proven with a deterministic same-session race in `test_memory_formation.py`
  's `test_a_concurrent_external_resolution_of_the_active_memory_raises_invalid_state_transition`).
  The two routes treat both the same way, by design:
  - `analyze_conversation` leaves them uncaught, exactly like the `SignalService` call
    just above it — the request fails (500 or 409, respectively) rather than return a
    200 that silently omits a fact its own analysis was supposed to produce.
  - `backfill_analysis` isolates both per conversation, the same way an
    `AnalysisService` failure already is: logged, counted via the batch's existing
    `failed` counter, and never allowed to abort the remaining conversations. A
    conversation whose memory formation fails is counted as `failed`, not
    `analyzed`/`cached` — its analysis and signal stay committed either way, but its
    own contribution to the batch's counters moves to `failed` so `considered ==
    analyzed + cached + failed` still holds with no double-counting.

  **Not built in this block**: a second automated fact (`Memory` has no field
  discriminating *which* fact a `kind=FACT` row represents — see
  `nerva_core.memory_rules`'s own module docstring), and no retry/backoff for a failed
  memory formation — the next backfill run (or the next `analyze` call) simply
  re-evaluates the conversation's current state from scratch, the same way a failed
  `AnalysisService` call already does.
