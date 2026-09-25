# Nerva

**The operating system for AI-powered companies.**

Nerva is a workspace where AI agents understand a company's context, coordinate work,
and carry out business operations, with a human approving what matters. The MVP proves
one loop:

> Connect Gmail → analyze conversations → identify leads → propose actions →
> human approves → Nerva executes → activity and memory are updated.

**This repository is through Milestone 3: real authentication, onboarding, a frontend
wired to the API, and read-only Gmail import.** Nerva can observe a connected Gmail
account (Settings → Integrations → Connect Gmail); it cannot yet send, reply to,
delete or otherwise act on email, and there is still no LLM or agent. See
[docs/MVP.md](docs/MVP.md) for exactly what is in and out of scope, and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how it is built.

```
nerva/
  apps/
    web/        Next.js (App Router), TypeScript strict, Tailwind CSS, shadcn/ui
    api/        FastAPI, Pydantic v2, SQLAlchemy 2, Alembic
  docs/         ARCHITECTURE.md, MVP.md
  scripts/      cross-platform task runner for the API
  docker-compose.yml   PostgreSQL for local development
```

## Prerequisites

| Tool | Version | Used for |
| --- | --- | --- |
| Node.js | 20.9 or newer (developed on 24) | web app and the root scripts |
| Python | 3.12 or newer (developed on 3.13) | API |
| Docker | with Compose v2 | PostgreSQL |

## Run it locally

All commands are run from the repository root. They work the same on Windows, macOS and
Linux.

**1. Configure the environment**

```bash
cp .env.example .env
```

On Windows PowerShell: `Copy-Item .env.example .env`. The defaults work as they are.

**2. Install dependencies** (creates `apps/api/.venv` and installs the web packages)

```bash
npm run setup
```

**3. Start PostgreSQL and create the schema**

```bash
npm run db:up
npm run api:migrate
```

**4. Start the API** (terminal 1)

```bash
npm run api:dev
```

The API listens on <http://127.0.0.1:8000>. Check <http://127.0.0.1:8000/health> and
browse the interactive docs at <http://127.0.0.1:8000/docs>.

**5. Start the web app** (terminal 2)

```bash
npm run web:dev
```

Open **<http://127.0.0.1:3000>** (not `localhost:3000` — see
[Authentication](docs/ARCHITECTURE.md#authentication) for why the host matters). Sign
up, create a workspace, and you land on **Today**.

## Everything you can run

| Command | What it does |
| --- | --- |
| `npm run setup` | Create the API virtualenv and install all dependencies |
| `npm run db:up` / `db:down` | Start / stop PostgreSQL (data is kept in a Docker volume) |
| `npm run db:reset` | Delete the local database volume, recreate it and migrate |
| `npm run db:logs` | Follow PostgreSQL logs |
| `npm run api:dev` | API with auto-reload on port 8000 |
| `npm run api:migrate` | Apply database migrations |
| `npm run api:revision -- "message"` | Generate a new migration from model changes |
| `npm run api:test` | Backend tests (pytest) |
| `npm run api:lint` / `api:format` | Ruff lint + format check / apply fixes |
| `npm run api:typecheck` | mypy (strict) |
| `npm run web:dev` | Web app on port 3000 |
| `npm run web:test` | Frontend tests (Vitest + Testing Library) |
| `npm run web:lint` / `web:format` | ESLint / Prettier |
| `npm run web:typecheck` | TypeScript in strict mode |
| `npm run web:build` | Production build |
| `npm run check` | **Everything**: lint, typecheck, all tests and the production build |

Tests need the database: run `npm run db:up` first. The backend tests use a separate
`nerva_test` database, created automatically, and refuse to run against any database
whose name does not end in `_test`.

## Try the API

The API uses real sessions: sign up, and the response sets an `httpOnly` cookie that
authenticates every request after it. `curl -c`/`-b` persists that cookie across calls,
the same way a browser would.

```bash
# 1. Sign up. -c saves the session cookie to a file.
curl -s -c cookies.txt -X POST http://127.0.0.1:8000/v1/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"alex@northwind.example","name":"Alex Morgan","password":"correct horse battery staple"}'

# 2. Create a workspace. You become its owner. -b sends the saved cookie.
curl -s -b cookies.txt -X POST http://127.0.0.1:8000/v1/workspaces \
  -H "Content-Type: application/json" -d '{"name":"Northwind Studio"}'

# 3. Propose an action in it.
curl -s -b cookies.txt -X POST http://127.0.0.1:8000/v1/workspaces/<workspace id>/actions \
  -H "Content-Type: application/json" \
  -d '{"type":"send_email","summary":"Follow up with Priya about pricing"}'

# 4. Approve it (or POST .../reject).
curl -s -b cookies.txt -X POST \
  http://127.0.0.1:8000/v1/workspaces/<workspace id>/actions/<action id>/approve

# 5. Log out. Invalidates the session server-side and clears the cookie.
curl -s -b cookies.txt -c cookies.txt -X POST http://127.0.0.1:8000/v1/auth/logout
```

Every request under `/v1/workspaces/{id}/…` requires that the caller is a member of that
workspace; anyone else gets a `404`. See
[Authentication](docs/ARCHITECTURE.md#authentication) for the full session lifecycle.

## Connect Gmail (optional)

Nerva runs fine without this — Settings → Integrations just shows "Connect Gmail" as
unavailable until it's configured. To enable it locally:

**1. Create or select a Google Cloud project**

Go to [console.cloud.google.com](https://console.cloud.google.com/), and create a new
project (or pick an existing one) from the project selector at the top of the page.

**2. Enable the Gmail API**

APIs & Services → Library → search "Gmail API" → Enable.

**3. Configure the OAuth consent screen**

APIs & Services → OAuth consent screen. Choose **External** (unless you have a Google
Workspace organization and want **Internal**). Fill in an app name and your email as
the support/developer contact. You do not need to submit this for verification to use
it in development — see step 7.

**4. Create OAuth Web Application credentials**

APIs & Services → Credentials → Create Credentials → OAuth client ID → Application
type **Web application**.

**5. Add the authorized redirect URI**

Under that client's "Authorized redirect URIs", add exactly:

```
http://127.0.0.1:8000/v1/integrations/google/callback
```

This must match `GOOGLE_REDIRECT_URI` below byte-for-byte, including the port.

**6. Put the client ID, secret, and an encryption key in `.env`**

```bash
GOOGLE_CLIENT_ID=<the client ID Google gave you>
GOOGLE_CLIENT_SECRET=<the client secret Google gave you>
GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/v1/integrations/google/callback
INTEGRATION_ENCRYPTION_KEY=<generate one, see below>
```

Generate the encryption key once per checkout (it encrypts stored Gmail refresh
tokens; never share or commit it):

```bash
cd apps/api && .venv/Scripts/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

(On macOS/Linux: `.venv/bin/python` instead of `.venv/Scripts/python`.) Restart
`npm run api:dev` after editing `.env`.

**7. Add yourself as a test user**

While the OAuth consent screen is in **Testing** (the default, and fine for local
development — publishing it is not necessary), Google only allows sign-in from
accounts you explicitly list: OAuth consent screen → Audience → **Test users** → add
the Gmail address you'll connect with. Without this, Google's consent screen refuses
the login before Nerva ever sees it.

Only `https://www.googleapis.com/auth/gmail.readonly` (plus `openid email` to identify
the account) is ever requested — Nerva cannot send, delete, or modify anything in a
connected mailbox. See [ARCHITECTURE.md §10](docs/ARCHITECTURE.md#10-nerva-connect-google-oauth-and-gmail-import).

## Configuration

The API and Docker Compose are configured through `.env` in the repository root (see
`.env.example`). The frontend reads its own `apps/web/.env.local` (see
`apps/web/.env.example`); its one setting has a working default, so this is optional
for local development.

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `nerva` | Credentials for the Docker database |
| `POSTGRES_PORT` | `5433` | Host port for PostgreSQL |
| `DATABASE_URL` | `postgresql+psycopg://nerva:nerva@127.0.0.1:5433/nerva` | API database connection |
| `TEST_DATABASE_URL` | same server, database `nerva_test` | Used by the backend tests |
| `ENVIRONMENT` | `local` | `local`, `test`, or `production` |
| `CORS_ORIGINS` | `http://127.0.0.1:3000` | Comma-separated browser origins allowed to call the API, with credentials |
| `FRONTEND_BASE_URL` | `http://127.0.0.1:3000` | Where the API redirects the browser after the Google OAuth callback |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | blank | Google OAuth Web Application credentials; see [Connect Gmail](#connect-gmail-optional) |
| `GOOGLE_REDIRECT_URI` | `http://127.0.0.1:8000/v1/integrations/google/callback` | Must match the redirect URI configured in Google Cloud exactly |
| `INTEGRATION_ENCRYPTION_KEY` | blank | Fernet key encrypting stored Gmail refresh tokens; required to connect Gmail |
| `NEXT_PUBLIC_API_URL` (`apps/web/.env.local`) | `http://127.0.0.1:8000` | Where the browser sends API requests |

## Troubleshooting

- **Signed in, but immediately redirected back to `/login`, or the page stays blank.**
  You most likely opened the app at `localhost:3000` instead of `127.0.0.1:3000`. The
  session cookie is same-site only (see [Authentication](docs/ARCHITECTURE.md#authentication)),
  and `localhost` and `127.0.0.1` are different sites to the browser even though they
  both mean "this machine". Use `127.0.0.1` for both the web app and the API.
- **Port 5432 is already in use.** Nerva's database deliberately uses **5433** so it does
  not clash with a PostgreSQL installed on your machine. Change `POSTGRES_PORT` and the
  two URLs in `.env` together if 5433 is taken too.
- **Database connections hang for several seconds on Windows.** Use `127.0.0.1`, not
  `localhost`, in database URLs. `localhost` resolves to IPv6 first, which Docker does
  not publish. The defaults already do this.
- **`docker compose` cannot connect.** Start Docker Desktop and wait until it says the
  engine is running.
- **`npm run api:*` says the virtualenv is missing.** Run `npm run api:setup`.
- **Backend tests cannot connect.** Run `npm run db:up` and wait for it to report healthy.
- **A migration test says models and migrations have drifted.** You changed a model:
  run `npm run api:revision -- "describe the change"` and review the generated file.
- **Web build fails while fetching fonts.** The build downloads Geist from Google Fonts
  and needs internet access.
- **"Google has not verified this app" / "access_denied" when connecting Gmail.** If
  the OAuth consent screen is still in **Testing**, only accounts added under **Test
  users** may sign in — see [Connect Gmail, step 7](#connect-gmail-optional).
- **Connecting Gmail fails immediately with an integration error.** Check that
  `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `INTEGRATION_ENCRYPTION_KEY` are all
  set in `.env` and that the API was restarted after editing it.

## What is not here yet

Sending, replying to, deleting or modifying email (Gmail access is read-only), LLM
providers, agents, action execution, embeddings, invitations and roles-based
permissions are intentionally left for later milestones. See [docs/MVP.md](docs/MVP.md).
