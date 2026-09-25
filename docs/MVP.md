# Nerva MVP v0.1

> The operating system for AI-powered companies.

## The product loop

The MVP proves one loop end to end:

```
Connect Gmail
  → analyze conversations
  → identify leads / opportunities
  → propose actions
  → human approves
  → Nerva executes
  → activity and memory are updated
```

We are building it **incrementally**. v0.1 is the **technical foundation** underneath
that loop: the data model, the tenancy and approval rules, and the application shell.
It deliberately contains none of the intelligence and none of the integrations.

## What v0.1 includes

### Platform

- Monorepo: `apps/web` (Next.js) and `apps/api` (FastAPI), clearly separated.
- PostgreSQL via Docker Compose, schema managed by Alembic migrations.
- Environment configuration through a single `.env` (see `.env.example`).
- Linting, formatting, strict type checking and automated tests for both apps.

### Backend (`apps/api`)

- **Multi-tenancy from day one**: every business record belongs to a workspace, access
  is granted only through workspace membership, and the database itself rejects
  cross-workspace references. See [ARCHITECTURE.md](ARCHITECTURE.md).
- **Domain model**: `User`, `Workspace`, `WorkspaceMember`, `Contact`, `Company`,
  `Conversation`, `Message`, `Opportunity`, `Action`, `ActivityEvent`, `Memory`.
- **Action lifecycle** with enforced transitions:
  `proposed → approved → executing → completed | failed`, or `proposed → rejected`.
- **Audit trail**: every important operation records an append-only `ActivityEvent`
  in the same transaction as the change it describes.
- **A small REST API** that proves the model works:
  - `GET /health`
  - authentication: `POST /v1/auth/signup`, `/login`, `/logout`, `GET`/`PATCH /v1/auth/me`
  - workspaces and members
  - companies, contacts and opportunities (create, list, get)
  - actions: propose, list, get, **approve**, **reject**
  - the activity feed (read-only)
- Validation and a single consistent error format on every endpoint.

### Frontend (`apps/web`)

- The application shell: sidebar, responsive layout, light and dark themes.
- Pages: **Today**, **Inbox**, **Opportunities**, **Nerva**, **Activity**, **Settings**.
- All content is static, and pages that show sample data label it as a preview.
  The frontend does not call the API yet.

## Milestone 2: real authentication, onboarding, frontend integration

Turns the technical foundation into a usable product: sign up, sign in, create a
workspace, and see your own data.

- **Real authentication** replaces the `X-User-Id` header: email/password signup and
  login, bcrypt password hashing, and server-side sessions delivered as an `httpOnly`
  cookie. See [ARCHITECTURE.md § Authentication](ARCHITECTURE.md#authentication).
- **Onboarding**: a new user with no workspace is guided through a two-step flow
  (welcome, then create a workspace) before reaching the product.
- **Frontend ↔ API integration**: `/login`, `/signup` and `/onboarding` pages; route
  protection based on session and workspace membership; the sidebar, Today,
  Opportunities, Activity and Settings pages now load and act on real workspace data
  instead of static samples. Inbox and the Nerva chat remain visual-only, since Gmail
  and an LLM are still not connected.
- Production (`ENVIRONMENT=production`) is no longer refused at startup — the only
  reason it was blocked (the spoofable dev header) no longer exists — though it is
  still not deployed anywhere.

## Milestone 3: Nerva Connect — Google OAuth + Gmail ingestion

Nerva can now **observe** Gmail. It cannot yet understand, classify, reply to, modify,
archive, delete or send email — that boundary is deliberate and enforced by the OAuth
scope requested (`gmail.readonly` only).

- **Google OAuth 2.0**: an authenticated workspace owner connects a Google account from
  Settings → Integrations. A cryptographically secure, single-use, server-bound `state`
  value protects the flow against OAuth CSRF, callback replay, and connecting an
  integration to a workspace the caller does not belong to.
- **Encrypted credential storage**: only a refresh token is ever stored, encrypted at
  rest (Fernet); no access token is persisted, and no schema returns a token to the
  frontend.
- **A bounded, idempotent sync**: `POST .../integrations/{id}/sync` imports at most the
  last 30 days of mail, capped at 200 messages per run, and running it twice never
  duplicates a conversation or message.
- **Normalized data**: Gmail threads and messages become `Conversation` / `Message`
  rows — the same tables a future connector (e.g. Outlook) would also fill — with HTML
  bodies converted to safe plain text, never rendered as HTML.
- **Nerva Inbox** shows the result: a real conversation list and detail view, replacing
  the placeholder.

See [ARCHITECTURE.md §10](ARCHITECTURE.md#10-nerva-connect-google-oauth-and-gmail-import)
for the full design, including the security decisions (state handling, token
encryption, content-safety treatment of email) and what is deliberately deferred (write
access, background/scheduled sync, key rotation tooling).

## What v0.1 (+ Milestones 2–3) intentionally excludes

| Excluded | Why it waits |
| --- | --- |
| Sending, replying to, deleting, archiving or labeling email | Gmail access is deliberately read-only in this milestone; a write-access milestone needs its own consent scope and review. |
| LLM providers and prompts | The chat UI is visual only; nothing typed in it leaves the browser. Imported email is already treated as untrusted input for when this lands (see ARCHITECTURE.md §10). |
| Autonomous agents, planning, tool use | Requires an executor to be meaningful, not just a connector. |
| Action **execution** | Only the lifecycle exists. Nothing is ever sent, created or changed outside Nerva. |
| Lead/opportunity detection from email | Nerva imports conversations; it does not yet read them. |
| Vector database, embeddings, pgvector | Memory is a plain relational table. |
| Invitations, roles-based permissions | Roles are stored; only membership (and, for integrations, ownership) is enforced. Members are created by owning a workspace, via onboarding. |
| Password reset, email verification | The session/token design does not make either hard to add later (see ARCHITECTURE.md). |
| Multi-workspace switching | A user's first workspace is used; the frontend state already separates "all workspaces" from "current one" for when this is added. |
| Background jobs and queues | Sync is user-triggered; no scheduler exists yet. |
| Billing, SSO, notifications, mobile app | Not part of proving the loop. |
| Production deployment, CI/CD | The API supports `ENVIRONMENT=production`; nothing is actually deployed. |

If a change needs something from this list, it is a **new milestone**, not part of this one.

## Definition of done for v0.1

- [x] `docker compose up` gives a working PostgreSQL; migrations apply cleanly and are
      verified to match the models.
- [x] A workspace can exist, and contacts, companies and opportunities can belong to it.
- [x] Actions can be proposed, approved and rejected; illegal transitions return `409`.
- [x] One workspace can never read, write or reference another workspace's data.
- [x] Every state change is recorded in the activity feed.
- [x] The web app runs with all six pages and builds for production.
- [x] Backend tests, frontend tests, lint, type checking and builds all pass.

## Definition of done for Milestone 2

- [x] Sign up, sign in, sign out; sessions persist across a page reload.
- [x] A new user without a workspace is routed through onboarding, not the product.
- [x] An existing user's workspace is restored on login; onboarding is not repeated.
- [x] Protected product routes are inaccessible while signed out.
- [x] The sidebar, Today, Opportunities, Activity and Settings pages show real
      workspace data; empty workspaces show honest empty states, not sample data.
- [x] Tenant isolation (§4 of ARCHITECTURE.md) holds under the new auth system.
- [x] Backend tests, frontend tests, lint, type checking and builds all pass.

## Definition of done for Milestone 3

- [x] Google OAuth state is unpredictable, single-use, and bound server-side to the
      user/workspace that started the connection; CSRF, replay and cross-workspace
      connection attempts are all rejected and covered by tests.
- [x] Gmail refresh tokens are encrypted at rest; no API response ever includes a
      token; production refuses to start without a valid encryption key.
- [x] Sync is bounded (30 days / 200 messages) and idempotent (verified by tests that
      run it twice); a provider failure marks the integration `error` or
      `needs_reauth` and never reports success it did not achieve.
- [x] Settings → Integrations and Inbox show real Gmail state (disconnected /
      connected / needs reauth) and real synchronized conversations, with correct
      empty states.
- [x] Tenant isolation extends to integrations and imported conversations/messages: a
      workspace can never see another's.
- [x] Backend tests, frontend tests, lint, type checking, mypy strict and builds all
      pass, entirely against fakes — no automated test needs a real Google account.
- [x] **A real Gmail account has been connected and synced end to end in the browser.**
      Verified live against a real Google Cloud OAuth client and a real Gmail mailbox:
      the full consent flow, an initial sync (192 messages into 188 conversations), a
      second sync proving idempotency (0 new records created), Inbox and persistence
      across a page refresh, and clean backend/browser logs throughout.

## What comes next (not started)

1. **Workspace invitations** and enforced, role-based permissions.
2. **Analysis**: identify leads and opportunities from conversations Nerva has imported.
3. **Propose**: Nerva creates `Action`s (e.g. a drafted reply) for human approval.
4. **Execute**: run approved actions through Gmail (write access); record results and activity.
5. **Memory**: write and recall workspace memory from what Nerva learns.
