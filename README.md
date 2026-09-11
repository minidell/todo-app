# Todo App

A personal todo application built with a Python FastAPI backend and a React TypeScript
frontend. Iteration 1 established the core skeleton (add, remove, toggle completion, display
todos) with an in-memory store. **Iteration 2 adds PostgreSQL persistence** (async SQLAlchemy +
Alembic), Docker/Compose for `db`, `backend` and `frontend`, auth with multiple lists,
priorities/tags/due-dates/subtasks with filtering and search, **real-time updates over
Server-Sent Events**, and **optional AI features** (todos from natural language, subtask
suggestions, priority/tag suggestions, a daily summary, and editing a todo by typing a free-text
instruction) backed by a small Ollama model running in its own Docker profile — see
`docs/specs/`.

## Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/) — the Python package/project manager
- Node 22+ with npm
- Docker 29+ and Docker Compose 2.40+ (only needed for the containerized run mode, or to run a
  local Postgres for the non-Docker backend/E2E workflow)

## Run with Docker

```bash
cp .env.example .env
# Generate the required secrets — JWT_SECRET (always) and AI_AGENT_TOKEN (only
# needed if you'll ever run the ai profile; harmless to generate either way):
sed -i "s/^JWT_SECRET=.*/JWT_SECRET=$(openssl rand -hex 32)/; s/^AI_AGENT_TOKEN=.*/AI_AGENT_TOKEN=$(openssl rand -hex 32)/" .env
# Edit POSTGRES_PASSWORD etc. in .env if you like, then:
docker compose up -d --build
```

The backend **fails closed**: it refuses to start with an empty or missing `JWT_SECRET`
(`docker compose` itself errors out with "set JWT_SECRET in .env" before the container even
builds), and — when `AI_ENABLED=true` (the default) — with an empty, missing, or placeholder-
looking `AI_AGENT_TOKEN`. Set `AI_ENABLED=false` in `.env` to skip the AI token requirement
entirely (see **AI features** below).

- Frontend (SPA, served by nginx): **http://localhost:5173**
- Backend API: **http://localhost:8010/api** (health: `GET /api/health`)
- Postgres: **localhost:5433** (also reachable in-network as `db:5432`)

`docker compose down -v` stops the stack and removes the `pgdata` volume (all data). Use
`docker compose down` (no `-v`) to stop while keeping the data.

The AI services (`ai-agent`, `ollama`, `ollama-pull`) never start with a plain
`docker compose up -d` — they live under the compose `ai` profile. See **AI features** below.

## Run locally without Docker

Start only the database in Docker, then run backend and frontend natively:

```bash
docker compose up -d db

cd backend
uv sync
DATABASE_URL=postgresql+asyncpg://todo:<POSTGRES_PASSWORD from .env>@localhost:5433/todo \
  uv run alembic upgrade head
DATABASE_URL=postgresql+asyncpg://todo:<POSTGRES_PASSWORD from .env>@localhost:5433/todo \
  uv run uvicorn app.main:app --port 8010 --reload
```

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173, proxies /api to http://localhost:8010
```

## Ports

Other projects on this machine already own `5432`, `11434`, `8000`, `3000` and `8080` — this
project's host ports are chosen to avoid every one of them. **Do not assume the defaults below
are free to reuse for anything else on this host.**

| Service | Host port | In-network address |
|---|---|---|
| frontend (nginx, Docker, or Vite dev server) | **5173** | `frontend:80` |
| backend | **8010** | `backend:8000` |
| db (PostgreSQL) | **5433** | `db:5432` |
| ai-agent (`ai` profile) | **8020**, debugging only | `ai-agent:8000` |
| ollama (`ai` profile) | *not published* (host 11434 belongs to another project) | `ollama:11434` |

`DATABASE_URL` is never defaulted to `localhost:5432` anywhere in this repo — always `:5433` for
host access, or `db:5432` inside the compose network.

## Testing

### Backend

```bash
cd backend
uv run pytest                              # SQLite lane (aiosqlite, no external DB needed)
```

Postgres lane (same suite, run against a real Postgres — requires `db` up):

```bash
docker compose up -d db
cd backend
TEST_DATABASE_URL=postgresql+asyncpg://todo:<POSTGRES_PASSWORD from .env>@localhost:5433/todo_test \
  uv run pytest
```

The `db` service creates both `todo` and `todo_test` databases on first start via
`docker/db/init/01-create-test-db.sql`.

### Frontend

```bash
cd frontend
npm test                                    # Vitest unit/component tests
npm run build                               # TypeScript check + Vite build

# E2E (Playwright) — requires a reachable Postgres (docker compose up -d db) since the
# backend the E2E harness spawns talks to a real database, not a mock.
docker compose up -d db
PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npx playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e
```

`PLAYWRIGHT_BROWSERS_PATH` must be exported for every Playwright install/run in this repo (keeps
the browser cache inside the project root, out of any shared/global location).

The harness runs the backend with AI disabled unless you export `AI_ENABLED=true` plus
`AI_AGENT_URL`/`AI_AGENT_TOKEN` and `E2E_AI=1` (the last one also un-skips `e2e/ai.spec.ts`).

## Migrations (Alembic)

```bash
cd backend
uv run alembic upgrade head        # apply all pending migrations
uv run alembic downgrade -1        # roll back the most recent migration
uv run alembic downgrade base      # roll back everything
```

Migrations are immutable once applied: a change is always a new `NNNN_*.py` file, never an edit
to an existing one, and every migration ships a tested `downgrade()`.

## Accounts and sign-in

The app uses **JWT bearer-token authentication**. Each user registers with an email and password,
and subsequent requests include a `Authorization: Bearer <token>` header. Tokens are stored
client-side in `localStorage` (with a noted security/convenience tradeoff: tokens are readable by
any script on the same domain, and a lost device can access the account without password reset).

### Token lifecycle
- **Tokens expire after 60 minutes** — after expiry, sign in again.
- **Logout is client-side only** — it clears the stored token, but the token remains cryptographically
  valid until its 60-minute expiry (e.g., a lost device could still be used within this window if the
  token was compromised).
- **Every user has one or more named todo lists** (starting with a default `Inbox` on registration).
- **All todos and lists are per-user** — sign-out and back in with a different account to switch contexts.

### Rate limiting
- **Registration and login** are rate-limited to protect against brute force: **10 attempts per
  15 minutes per IP+email** (e.g., 10 failed login attempts for `user@example.com` from a single IP).
- **Across both auth endpoints** (register and login): **30 attempts per 15 minutes per IP**.
- Rate limit status is returned as HTTP 429 with a `Retry-After` header indicating seconds to wait.
- Rate limit detection is based on the client IP. Behind the compose/nginx proxy, with
  `TRUST_PROXY_HEADERS=true` (the Docker default; see `.env.example`), the backend reads only the
  **last hop of `X-Forwarded-For`** — the one nginx itself appended, which cannot be spoofed by
  the client. (nginx also sets `X-Real-IP`, but the backend does not read it.) A planned
  `TRUSTED_PROXY_CIDRS` setting (default: loopback + the private ranges) will further restrict
  which upstream sockets the backend accepts forwarded headers from, so a request never reaches
  the backend directly and skips its own connecting proxy. In local (non-Docker) development, the
  rate limiter uses the direct connection IP.

### Migration from slice 1
If you previously ran iteration 2 slice 1 (before auth), your dev data was stored under a
bootstrap user that no longer exists. **Reset your data with `docker compose down -v`** to remove
the old volume and start fresh.

## Realtime

The backend pushes live updates over **Server-Sent Events** at `GET /api/events`
(`text/event-stream`) — todo/list creates, updates, deletes, from any tab or device signed in as
the same user show up in every other open tab within a couple of seconds, without a manual
refresh. SSE was chosen over WebSocket because the traffic is one-way (server → client): it rides
plain HTTP/1.1, needs no `Upgrade` handling in nginx (just `proxy_buffering off` and friends,
already configured in `docker/nginx.conf`), and reconnects automatically.

- **Polling fallback:** if the stream can't be established (proxy issue, browser blocking
  long-lived connections, three failed reconnect attempts), the frontend falls back to polling
  the current view every 15 s and keeps retrying the stream in the background. The user is never
  shown a connection error — realtime is an enhancement, not a feature gate.

### Scaling out (not supported)

The event broker lives in the backend process's memory (`EventBroker`, in-process
`dict[UUID, set[asyncio.Queue]]`). This means **exactly one `backend` replica may run.**
`REALTIME_BACKEND=memory` (the only accepted value — the app fails to start with any other) makes
this explicit rather than a silent trap:

- **What breaks:** running more than one replica (`docker compose up --scale backend=N`, or adding
  a `deploy.replicas` block) silently splits SSE traffic across replicas by whichever one accepts
  each connection. A client subscribed on one replica never sees an event published from another —
  some tabs simply stop receiving updates, with no error anywhere.
- **Future options:** Postgres `LISTEN/NOTIFY` (no new service, reuses the existing database) or a
  Redis pub/sub broker (a new dependency, but a well-trodden pattern for this exact problem). Both
  are out of scope for this iteration — `REALTIME_BACKEND` reserves the name so adding one later is
  an additive config change, not a new name to invent under pressure.
- **The only two seams a future implementation has to replace:** `EventBroker.subscribe(user_id)`
  and `EventBroker.publish(user_id, event)` (`backend/app/events.py`). Everything else — the SSE
  route, reconnect/backoff, the polling fallback — is agnostic to how fan-out happens underneath.

## AI features

Optional, natural-language todo creation, subtask suggestions, priority/tag suggestions, a
daily summary and edit-by-instruction — backed by a small local model served through
[Ollama](https://ollama.com). The app is **fully usable with the AI profile never started**:
every AI action has a manual equivalent, and the UI simply hides AI affordances when the service
is unavailable.

- **Edit with AI** (iteration 5) — pick `Edit with AI` in any todo's AI menu and type a free-text
  instruction (*"rename it to Call the dentist and add a step to find the number"*, *"tag it work
  and drop the second step"*). The AI answers with a **draft change set**: one checkbox per
  proposed change, all of them deselectable, and nothing is written until you press `Apply`. The
  confirmation step is the feature rather than a formality — a small local model can misread an
  unusual instruction, and the draft is where you catch that. Applying goes through the ordinary
  todo endpoints, so an apply still works if the AI stack died while you were reading the draft.

The `ai-agent` microservice (see `ai-agent/README.md`) is private to the compose network; the
frontend never talks to it directly — the backend proxies every call, so there is one auth
surface (the user's JWT) and one place that reads a user's todos to build a prompt.

### Running it

```bash
docker compose --profile ai up -d --build
```

This starts three extra services beyond the base stack:

| Service | Role |
|---|---|
| `ollama` | Serves the model on the compose network only (not published on the host); keeps a loaded model resident for `OLLAMA_KEEP_ALIVE` (default 24h) so it survives idle periods between requests |
| `ollama-pull` | One-shot: pulls `${OLLAMA_MODEL:-qwen2.5:3b}` if not already present, then makes one throwaway generation call to warm it into memory before exiting |
| `ai-agent` | FastAPI service that prompts Ollama and returns validated, clamped JSON; waits for `ollama-pull` to finish (but starts anyway if the pull failed) |

**`docker compose --profile ai up` takes a minute or two the first time** — `ollama-pull`
downloads the model (a few minutes on a fresh volume, `qwen2.5:3b` is roughly 2 GB) and then
warms it into memory with a throwaway generation call, so the *first real user request* hits an
already-loaded model instead of paying the cold-load cost itself (measured 45-90s for
`qwen2.5:3b` on CPU on this host — comfortably over the default 45s `OLLAMA_TIMEOUT_SECONDS` /
50s `AI_TIMEOUT_SECONDS`, which is exactly why the warm-up exists). `ai-agent` waits for
`ollama-pull` to reach a terminal state before starting, but **the whole stack still comes up
even if the pull or warm-up fails** (no internet, disk full) — with no model pulled, `ai-agent`
reports `model_present: false` on `GET /health` and AI calls return 503 until the model is
available. Retry the pull (and warm-up) any time:

```bash
docker compose --profile ai run --rm ollama-pull
```

`ollama` uses the named volume `ollama_models` (fixed Docker volume name `todo-app-ollama`, set
via `volumes: ollama_models: { name: todo-app-ollama }` in `docker-compose.yml`) to persist
pulled models across restarts. This host already has that volume, pre-seeded with `qwen2.5:3b` —
compose reuses it as-is on `up`, nothing to create by hand. On a fresh machine, compose creates
an empty volume with that same fixed name on the first `--profile ai up`, and `ollama-pull`
populates it.

### Turning it off

Set `AI_ENABLED=false` in `.env` (backend env var) to hide every AI feature in the UI regardless
of whether the `ai` profile is running, or simply never start the profile — there is no AI
dependency anywhere else in the stack.

### Status and unavailability reasons

`GET /api/ai/status` never fails — it always returns 200, so the frontend can hide or disable AI
controls instead of showing an error. Since iteration 4 it also reports **why** AI is unavailable
via a closed `reason` enum (`null` when `available` is `true`):

| `reason` | Meaning | What to do |
|---|---|---|
| `null` | AI is available | — nothing, normal operation |
| `disabled` | `AI_ENABLED=false` in `.env` | Set `AI_ENABLED=true` and restart the `backend` service if AI should be on |
| `unreachable` | The backend could not reach `ai-agent` at all (connect error, timeout, non-2xx, non-JSON) | Confirm the `ai` profile is up: `docker compose --profile ai ps` |
| `auth_failed` | `ai-agent` answered but rejected the backend's `X-Internal-Token` | `AI_AGENT_TOKEN` differs between the `backend` and `ai-agent` environment — usually a stale `.env` on one service after rotating the secret; set the same value for both and restart both services |
| `model_unavailable` | `ai-agent` is reachable and authenticated, but Ollama is down or `OLLAMA_MODEL` was never pulled | Check `docker compose --profile ai logs ollama ollama-pull`, or retry the pull: `docker compose --profile ai run --rm ollama-pull` |

`reason` is a machine value, never a prose string or an echoed error body, and every value other
than `null` maps to the same 503 `ai_unavailable` on the POST endpoints — nothing about error
*bodies* changes, this is purely additional diagnostic detail on the status check.

### Choosing a model

Override `OLLAMA_MODEL` in `.env` (e.g. `qwen2.5:7b`, `llama3.1:8b`) for better structured-JSON
behaviour at the cost of memory and latency; see `ai-agent/README.md` for the tradeoffs measured
against the default model. On a host with a capable GPU, `OLLAMA_MODEL=qwen2.5:7b` measurably
improves relative-date resolution in AI-drafted todos over the default 3B model — this is a
deliberate manual opt-in, not something compose or the app detects or switches automatically (a
GPU host cannot portably prove it has room for a 7B model, and CPU-only hosts should stay on the
lighter default).

### GPU acceleration

The default `docker-compose.yml` runs Ollama on CPU so the stack works on any machine. On a host
with an NVIDIA GPU and the [`nvidia` container runtime](https://github.com/NVIDIA/nvidia-container-toolkit)
installed, apply the GPU override to give Ollama one GPU:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai up -d
```

Verify the GPU is in use with `docker compose --profile ai logs ollama` (it logs the detected
GPU on startup) or `nvidia-smi` while a request is in flight. This host's GTX 1060 (6 GB) fits
the default `qwen2.5:3b` comfortably. The GPU override also pins `CUDA_VISIBLE_DEVICES=0` on the
`ollama` service, removing one source of discovery ambiguity on a single-GPU host.

### Troubleshooting: GPU discovery

GPU discovery inside the `ollama` container has been observed to be flaky on this host — a
discovery timeout on some starts, success on others — with the container silently falling back to
CPU rather than failing loudly. Since CPU and GPU both "work" (CPU is just much slower), this is
easy to miss.

**Symptom:** AI responses take the CPU-scale time (tens of seconds) even though the GPU override
was applied and `nvidia-smi` shows the GPU otherwise idle.

**Detection:**
```bash
docker compose --profile ai logs ollama | grep -i -e gpu -e cuda -e "looking for compatible"
nvidia-smi   # while an AI request is in flight — no `ollama` process listed means it fell back to CPU
```

**Remedy:** restart just the `ollama` service with the GPU override reapplied — this re-runs GPU
discovery without disturbing `ai-agent`'s already-warmed connection or losing the loaded model in
`ollama-pull`'s work:
```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai restart ollama
```
Then re-check with the detection commands above. `OLLAMA_KEEP_ALIVE` (default 24h) is unaffected
by the restart, so once discovery succeeds the model stays resident.

**Reassurance:** CPU fallback is degraded, not broken — every AI request still completes
correctly, just slower. The 45 s / 50 s / 60 s timeout ladder (`OLLAMA_TIMEOUT_SECONDS` →
`AI_TIMEOUT_SECONDS` → the frontend's own `AbortController`, master §8.3) is deliberately sized
for CPU-speed generation, so a CPU fallback should not surface as a user-visible error — only as
latency.

### Debugging

`ai-agent` is **not published on the host by default** — it's reachable only on the compose
network as `ai-agent:8000`, and is protected by a shared secret header (`X-Internal-Token`,
value from `AI_AGENT_TOKEN` in `.env`) on every route except `/health`. To debug it directly,
uncomment the `ports:` line on the `ai-agent` service in `docker-compose.yml`
(`127.0.0.1:${AI_AGENT_HOST_PORT:-8020}:8000` — **for debugging only**) and rebuild:

```bash
docker compose --profile ai up -d --build ai-agent
curl localhost:8020/health
curl -X POST localhost:8020/ai/parse-todo \
  -H "X-Internal-Token: $AI_AGENT_TOKEN" -H 'content-type: application/json' \
  -d '{"text":"call the dentist tomorrow morning, urgent","today":"2026-09-02","known_tags":[]}'
```

**Never point `OLLAMA_BASE_URL` at host port 11434** — it belongs to a different project's
Ollama instance on this machine. Our `ollama` container is reachable only on the compose network
as `ollama:11434` (or on `11435` if you uncomment the debug port mapping in
`docker-compose.yml`).

## Docs

See `docs/specs/` for the frozen API contract, data model, and the per-slice implementation
specs that drove this codebase.
