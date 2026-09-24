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
  nerva_memory/    future: memory read/write        (docstring only)
  nerva_connect/   future: external connectors      (docstring only)
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
        ┌───────────────┬───────────┼───────────────┬───────────────┐
    companies ──< contacts    conversations ──< messages     activity_events
        │            │              │
        └──────┬─────┴──────────────┘
          opportunities ──< actions
   memories ── (workspace, or one contact / company / opportunity)
```

| Table | Purpose |
| --- | --- |
| `users` | A person. Exists above the tenant boundary. Email is unique and lower-case; `password_hash` is a bcrypt hash, never returned by the API. |
| `sessions` | A logged-in browser session: `user_id`, a SHA-256 `token_hash` (the raw token lives only in the cookie), `expires_at`, `revoked_at`. Above the tenant boundary, like `users`. |
| `workspaces` | A tenant (a company using Nerva). `slug` is unique. |
| `workspace_members` | `(workspace_id, user_id, role)`. **The authorization source of truth.** Roles: owner, admin, member. |
| `companies` | An organization the workspace deals with. |
| `contacts` | A person, unique by email **within a workspace**; optionally belongs to a company. |
| `conversations` | A thread with a contact, with a `channel` and an `external_id` (e.g. a Gmail thread id). Filled by connectors later. |
| `messages` | A message within a conversation (direction, sender, body, sent time). |
| `opportunities` | A lead/deal: title, stage (new → qualified → proposal → won/lost), optional value; may link a contact, company and the conversation it came from. |
| `actions` | Something Nerva proposes to do, gated by approval. See §5. |
| `activity_events` | The append-only audit trail. See §6. |
| `memories` | Durable facts/preferences, either about the workspace or about at most one contact, company or opportunity. Plain relational rows: no embeddings. |

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

These are reserved as packages so the shape is visible, but they contain no code (apart
from `nerva_actions/lifecycle.py`). They are intentionally not abstract interfaces:
each will be designed when its first real implementation arrives.

| Module | Responsibility | First real work |
| --- | --- | --- |
| **`nerva_core`** | The agent runtime: reads workspace context and memory, decides what to propose, coordinates work. Produces `Action` proposals via the same `ActionService` as humans, attributed to `Actor.nerva()`. | Lead identification from conversations |
| **`nerva_actions`** | Executors that carry out approved actions, per-type payload schemas, retry policy. Uses `start_execution` / `complete` / `fail`. | Send an approved email reply |
| **`nerva_memory`** | Writing, ranking and recalling workspace memory. Starts relational; embeddings/pgvector only if retrieval quality demands it. | Persist facts learned from conversations |
| **`nerva_connect`** | Connectors to external tools (Gmail first): OAuth, sync into `Conversation`/`Message`, and the API calls that executors need. | Read-only Gmail import |

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
