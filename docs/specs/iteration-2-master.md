# Iteration 2 — Master Specification

Status: **frozen** (data model, API contract v2, error contract, env vars and Docker topology
are binding for every slice)
Date: 2026-09-02
Supersedes: `docs/specs/iteration-1-todo-core.md` §2 (API contract) once slice 1 merges.
Iteration-1 decisions D1–D12 remain in force **except** where explicitly reversed in §12.

Slice specs (each self-contained, implementers read master §1–§11 + their slice file):

| # | File | Theme |
|---|---|---|
| 1 | `docs/specs/iteration-2-slice-1-persistence-foundation.md` | Postgres + SQLAlchemy + Alembic + Docker base + carry-over fixes |
| 2 | `docs/specs/iteration-2-slice-2-auth-and-lists.md` | JWT auth, users, multiple lists |
| 3 | `docs/specs/iteration-2-slice-3-todo-enrichment.md` | priority, due date, description, tags, subtasks, filter/sort/search |
| 4 | `docs/specs/iteration-2-slice-4-realtime-and-ai.md` | SSE realtime, `ai-agent` service, Ollama, AI UI |

---

## 0. Goals and non-goals

### Goals
1. **PostgreSQL persistence** — SQLAlchemy 2.x async ORM + Alembic reversible migrations.
2. **Auth / multiple users** — every user sees only their own data; enforced at the repository
   layer, not only in routers.
3. **Priorities, due dates, tags, descriptions.**
4. **Filtering, sorting, search** on the todo list endpoint.
5. **Subtasks (exactly one level deep) *and* multiple todo lists** — both, not either.
6. **Tests everywhere + Docker**: `Dockerfile` for backend, frontend and ai-agent;
   `docker-compose.yml` covering `db`, `backend`, `frontend`, `ai-agent`, `ollama`,
   `ollama-pull`; `docker-compose.gpu.yml` override.
7. **Real-time updates** — a second browser tab reflects changes without reload.
8. **AI features** via a small Ollama model in Docker, behind a separate `ai-agent`
   FastAPI microservice: natural-language todo creation, split-into-subtasks,
   suggest priority/tags, daily summary.

### Non-goals for iteration 2 (do not build)
Password reset / email sending · OAuth / social login · refresh tokens · sharing lists between
users · attachments · recurring todos · reminders/notifications · manual drag-and-drop ordering ·
i18n · dark mode · pagination UI (the API has `limit`/`offset`, the UI does not use it) ·
horizontal scaling of the backend · production deployment · nested subtasks deeper than one
level · multi-replica realtime fan-out.

### Invariants across all slices
- **Every slice is independently shippable to `main`** and leaves all iteration-1 capabilities
  (add / remove / toggle / display) working through the UI.
- `GET /api/health` stays `{"status":"ok"}` and stays DB-free (it is the compose and
  Playwright readiness probe).
- The `TodoResponse` JSON shape (§3.3) is emitted in full **from slice 1 onwards**, with default
  values for fields whose write-path lands later. The frontend type never changes shape between
  slices — only new fields get used.
- Todo/list/tag ids are UUIDs serialized as strings; path parameters are typed `str` and a
  malformed id yields **404**, never 422 (iteration-1 D1 preserved).

---

## 1. Environment facts (the design must respect these)

### 1.1 Host port map — other projects own 5432, 11434, 8000, 3000, 8080

| Service | Host port | In-container / in-network address |
|---|---|---|
| frontend (nginx, compose) | **5173** | `frontend:80` |
| frontend (Vite dev, no Docker) | **5173** | — |
| backend | **8010** | `backend:8000` |
| db (PostgreSQL 17) | **5433** | `db:5432` |
| ai-agent | **8020** | `ai-agent:8000` |
| ollama | **not published** (uncomment `11435:11434` only for debugging) | `ollama:11434` |

**Decision D-P1:** local (non-Docker) backend development also moves from 8000 to **8010**, so
one port means one thing everywhere. `vite.config.ts` proxy default target becomes
`http://localhost:8010`; `E2E_BACKEND_PORT` defaults to `8010`. Slice 1 makes this change.

**Decision D-P2:** a host-level Ollama already listens on 11434. Our `ollama` container is
reachable **only** on the compose network as `ollama:11434`; nothing on the host may be
assumed to be ours. `OLLAMA_BASE_URL` always points at `http://ollama:11434` inside compose.

### 1.2 GPU
NVIDIA GTX 1060 6 GB, `nvidia` docker runtime available.
- **Default `docker-compose.yml` runs Ollama on CPU** — it must work on any machine.
- `docker-compose.gpu.yml` is an override granting the `ollama` service one GPU:
  ```yaml
  services:
    ollama:
      deploy:
        resources:
          reservations:
            devices:
              - driver: nvidia
                count: 1
                capabilities: [gpu]
  ```
  Usage: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai up -d`.
- Default model **`qwen2.5:3b`** (~2 GB quantized — comfortable in 6 GB, good structured-JSON
  behaviour), overridable via `OLLAMA_MODEL`.

### 1.3 Tooling
uv · Node 22.22.1 · Docker 29 + Compose 2.40 · host Python 3.14 (containers pin **3.12**;
`requires-python = ">=3.12"` stays — do not raise it).
Playwright browsers live at `PLAYWRIGHT_BROWSERS_PATH=frontend/node_modules/.cache/ms-playwright`
(relative to the project root). Every agent that runs or installs Playwright must export it:
```bash
cd frontend
PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npx playwright install chromium
PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e
```

---

## 2. Test-database strategy (decided — do not re-litigate)

**Decision D-T1: the default backend test database is in-memory SQLite via `aiosqlite`; the
identical suite also runs against PostgreSQL when `TEST_DATABASE_URL` is set.**

Rationale: `testcontainers` needs Docker, which implementation worktrees and CI lanes may not
have; requiring a live Postgres would make `uv run pytest` fail on a clean checkout, which
breaks the "every change ships with passing tests" rule. SQLite is therefore the *always-works*
lane and Postgres is the *fidelity* lane. To make this safe the schema is kept **portable by
construction**:

| Rule | Why |
|---|---|
| UUID columns use `sqlalchemy.Uuid` (generic) | native `uuid` on PG, `CHAR(32)` on SQLite |
| Enums are `Enum(..., native_enum=False, length=N, validate_strings=True)` → `VARCHAR` + `CHECK` | no PG `CREATE TYPE`, no painful enum migrations, works on SQLite |
| Tags are a real many-to-many join table, **never** a PG array | portable + queryable |
| Timestamps use a project `UtcDateTime` `TypeDecorator` (§4.1) | guarantees tz-aware UTC on both engines |
| `due_date` is `sa.Date` (no time component) | no timezone semantics to diverge |
| Search is `.ilike('%q%')` on `title`/`description` — **no** `tsvector`, no trigram index | SQLAlchemy compiles `ILIKE` on PG and `lower() LIKE lower()` on SQLite |
| Email/tag case-insensitivity is achieved by **storing normalized (lowercase) values**, not by `CITEXT` or functional indexes | plain `UNIQUE` works on both |
| No partial indexes, no `NULLS LAST`; null-ordering is expressed as `ORDER BY (col IS NULL), col` | SQLite lacks both |
| SQLite test engine: `StaticPool`, `check_same_thread=False`, and a `PRAGMA foreign_keys=ON` `connect` event listener | otherwise FK cascades silently do nothing |

Consequences and guards:
- The SQLite test schema is built with `Base.metadata.create_all`, **not** by running Alembic.
  The drift risk between models and migrations is closed by a **Postgres CI job** that runs
  `alembic upgrade head`, `alembic downgrade base`, `alembic upgrade head`, then
  `alembic check` (autogenerate must report no diff), then the full pytest suite with
  `TEST_DATABASE_URL` pointing at the service container.
- Any test that genuinely needs PG behaviour is marked `@pytest.mark.postgres` and skipped
  when `TEST_DATABASE_URL` is unset. Keep this set as close to empty as possible.
- Local Postgres run without the full stack:
  ```bash
  docker compose up -d db
  cd backend && TEST_DATABASE_URL=postgresql+asyncpg://todo:todo@localhost:5433/todo_test uv run pytest
  ```
  (the `db` service creates `todo` and `todo_test` via an init script — §9.2).
- **Ollama-backed AI calls are always mocked in unit tests.** Exactly one integration test per
  AI endpoint may hit a real Ollama and is skipped unless `AI_AGENT_LIVE=1`.

---

## 3. Target data model (full, final for iteration 2)

Created in **one baseline Alembic migration in slice 1** (`0001_initial_schema`).

**Decision D-M1: the baseline migration creates the FULL target schema**, including tables and
columns that only slices 2–4 write to. Rationale: no table rewrites between slices, the response
shape is stable from slice 1, and each slice's migration risk drops to zero. Unused-yet columns
are inert. If a later slice genuinely needs a schema change, it adds `0002_*`, `0003_*` — every
migration must implement a tested `downgrade()`.

```
users ──1:N──▶ todo_lists ──1:N──▶ todos ──self 1:N (one level)──▶ todos (subtasks)
  │                                   │
  └──1:N──▶ tags ◀──N:M (todo_tags)───┘
```

### 3.1 Tables

**`users`**
| Column | Type | Constraints |
|---|---|---|
| `id` | `Uuid` | PK |
| `email` | `String(320)` | NOT NULL, `UNIQUE`, stored **lowercased + trimmed** |
| `password_hash` | `String(255)` | NOT NULL |
| `display_name` | `String(100)` | NULL |
| `created_at` | `UtcDateTime` | NOT NULL |
| `updated_at` | `UtcDateTime` | NOT NULL |

**`todo_lists`**
| Column | Type | Constraints |
|---|---|---|
| `id` | `Uuid` | PK |
| `user_id` | `Uuid` | FK → `users.id` `ON DELETE CASCADE`, NOT NULL, indexed |
| `name` | `String(100)` | NOT NULL, trimmed, 1..100 |
| `is_default` | `Boolean` | NOT NULL, default `false` |
| `created_at` / `updated_at` | `UtcDateTime` | NOT NULL |

`UNIQUE (user_id, name)`. "Exactly one default list per user" is enforced in the application
layer + repository tests (a partial unique index is not portable — D-T1).

**`todos`**
| Column | Type | Constraints |
|---|---|---|
| `id` | `Uuid` | PK |
| `user_id` | `Uuid` | FK → `users.id` `ON DELETE CASCADE`, NOT NULL — **owner denormalized onto every row** so ownership checks never need a join |
| `list_id` | `Uuid` | FK → `todo_lists.id` `ON DELETE CASCADE`, NOT NULL |
| `parent_id` | `Uuid` | FK → `todos.id` `ON DELETE CASCADE`, NULL |
| `title` | `String(200)` | NOT NULL, trimmed, 1..200 |
| `description` | `Text` | NULL, ≤ 2000 chars (app-validated) |
| `completed` | `Boolean` | NOT NULL, default `false` |
| `completed_at` | `UtcDateTime` | NULL |
| `priority` | `Enum('low','medium','high', native_enum=False, length=6)` | NOT NULL, default `'medium'` |
| `due_date` | `Date` | NULL |
| `created_at` / `updated_at` | `UtcDateTime` | NOT NULL |

Indexes: `ix_todos_user_id_list_id (user_id, list_id)` · `ix_todos_parent_id (parent_id)` ·
`ix_todos_user_id_due_date (user_id, due_date)` · `ix_todos_user_id_created_at (user_id, created_at)`.

Application-enforced invariants (each has a repository unit test):
- **I1 — one level deep**: if `parent_id` is set, the parent's `parent_id` must be `NULL`.
- **I2 — same owner**: a subtask's `user_id` equals its parent's.
- **I3 — same list**: a subtask's `list_id` equals its parent's; moving a parent to another list
  moves its subtasks.
- **I4 — `completed_at`** is set to `now(UTC)` when `completed` flips `false→true` and cleared on
  `true→false`.

**`tags`**
| Column | Type | Constraints |
|---|---|---|
| `id` | `Uuid` | PK |
| `user_id` | `Uuid` | FK → `users.id` `ON DELETE CASCADE`, NOT NULL |
| `name` | `String(30)` | NOT NULL, **normalized**: trimmed, lowercased, inner whitespace collapsed to a single space; must match `^[a-z0-9][a-z0-9 _-]{0,29}$` |
| `created_at` | `UtcDateTime` | NOT NULL |

`UNIQUE (user_id, name)`.

**`todo_tags`** (association)
| Column | Type | Constraints |
|---|---|---|
| `todo_id` | `Uuid` | FK → `todos.id` `ON DELETE CASCADE` |
| `tag_id` | `Uuid` | FK → `tags.id` `ON DELETE CASCADE` |

PK `(todo_id, tag_id)`, plus index on `tag_id`.

### 3.2 Layering (closes the iteration-1 carry-over "split record model from response model")
```
app/db/models.py      SQLAlchemy ORM entities  (User, TodoList, Todo, Tag)   ← persistence only
app/schemas/*.py      Pydantic v2 request/response models                     ← wire only
app/repositories/*.py Protocols + SQLAlchemy implementations                  ← take user_id
app/routers/*.py      FastAPI routers, convert entity → schema
```
Repositories return **ORM entities**; routers convert with `XResponse.model_validate(entity)`
(`model_config = ConfigDict(from_attributes=True)`). Sessions use `expire_on_commit=False` and
all relationships needed for a response are eager-loaded with `selectinload` — a lazy load in an
async context raises, so this is mandatory, not stylistic.

### 3.3 `TodoResponse` — the wire shape, stable from slice 1
```jsonc
{
  "id": "3f1b2c4e-9d7a-4f0b-8a11-6c2e5f0d9b31",
  "list_id": "0a5c...",
  "parent_id": null,                       // uuid string when this is a subtask
  "title": "Buy milk",
  "description": null,
  "completed": false,
  "completed_at": null,                    // ISO-8601 UTC "…Z" when completed
  "priority": "medium",                    // "low" | "medium" | "high"
  "due_date": null,                        // "2026-09-05" (date only, no time)
  "tags": ["errand"],                      // array of normalized names, sorted asc
  "subtasks": [],                          // TodoResponse[]; always [] inside a subtask
  "created_at": "2026-09-02T11:22:33.123456Z",
  "updated_at": "2026-09-02T11:22:33.123456Z"
}
```
- Slice 1 emits this exact shape with `description=null, priority="medium", due_date=null,
  tags=[], subtasks=[], parent_id=null`.
- `created_at`/`updated_at`/`completed_at` keep the iteration-1 format contract:
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$` (backend test asserts it).
- The frontend must never assert on object key counts.

`ListResponse`:
```jsonc
{ "id": "uuid", "name": "Inbox", "is_default": true,
  "todo_count": 12, "active_count": 5,      // top-level todos only, subtasks excluded
  "created_at": "…Z", "updated_at": "…Z" }
```

`UserResponse`: `{ "id": "uuid", "email": "a@b.c", "display_name": null, "created_at": "…Z" }`
(never contains `password_hash` — security-reviewer checklist item).

`TagResponse`: `{ "id": "uuid", "name": "errand", "todo_count": 3 }`

---

## 4. Backend architecture

```
                      ┌─────────────────────────────────────────────┐
  browser ──/api──▶   │ FastAPI (backend)                            │
   (nginx or Vite     │  middleware: CORS · BodySizeLimit            │
    proxies /api)     │  Depends(get_current_user) → JWT verify      │
                      │  routers: auth · lists · todos · tags        │
                      │           events (SSE) · ai (proxy)          │
                      │  repositories(session, user_id) ──▶ asyncpg ─┼──▶ db (PostgreSQL)
                      │  EventBroker (in-process, per-user queues)   │
                      └───────────────┬─────────────────────────────┘
                                      │ httpx + X-Internal-Token
                                      ▼
                          ┌────────────────────────┐        ┌──────────┐
                          │ ai-agent (FastAPI)     │──http──▶│ ollama   │
                          │ prompt + JSON-schema   │         │ qwen2.5  │
                          │ validation + retry     │         └──────────┘
                          └────────────────────────┘
```

### 4.1 Key backend modules (final layout after slice 4)
```
backend/
├── alembic.ini
├── migrations/
│   ├── env.py                     # async engine, run_sync(do_run_migrations)
│   └── versions/0001_initial_schema.py
├── app/
│   ├── main.py                    # app factory, lifespan, middleware, routers
│   ├── config.py                  # pydantic-settings Settings (§9.1)
│   ├── middleware.py              # BodySizeLimitMiddleware (fixed, §12 C4)
│   ├── errors.py                  # AppError hierarchy + exception handlers (§5)
│   ├── security.py                # argon2 hashing, JWT encode/decode
│   ├── deps.py                    # get_session, get_current_user, repositories, client id
│   ├── ratelimit.py               # in-memory sliding window
│   ├── events.py                  # EventBroker + event payload builders
│   ├── ai_client.py               # httpx client for the ai-agent service
│   ├── db/
│   │   ├── base.py                # DeclarativeBase + MetaData(naming_convention=…)
│   │   ├── types.py               # UtcDateTime TypeDecorator
│   │   ├── models.py              # ORM entities
│   │   └── session.py             # engine + async_sessionmaker factories
│   ├── schemas/                   # auth.py, lists.py, todos.py, tags.py, ai.py, common.py
│   ├── repositories/              # protocols.py, users.py, lists.py, todos.py, tags.py
│   └── routers/                   # auth.py, lists.py, todos.py, tags.py, events.py, ai.py
└── tests/                         # see per-slice test groups
```

`UtcDateTime` (`app/db/types.py`) — a `TypeDecorator(DateTime(timezone=True))` that on bind
rejects naive datetimes (raises `ValueError`) and converts to UTC, and on result attaches
`timezone.utc` when the driver returns a naive value (SQLite). This single class is what makes
D-T1 safe.

Metadata naming convention (required so Alembic `downgrade()` can drop constraints by name):
```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

### 4.2 Repository protocols (owner parameter — closes an iteration-1 carry-over)
Every method takes `user_id: UUID` as its first argument. There is no method that can reach a row
without it; cross-user access is impossible by construction, not by router discipline.

```python
class TodoRepository(Protocol):
    async def list_todos(self, user_id: UUID, query: TodoQuery) -> tuple[list[Todo], int]: ...
    async def get(self, user_id: UUID, todo_id: UUID) -> Todo | None: ...
    async def create(self, user_id: UUID, data: TodoCreateData) -> Todo: ...
    async def update(self, user_id: UUID, todo_id: UUID, data: TodoUpdateData) -> Todo | None: ...
    async def delete(self, user_id: UUID, todo_id: UUID) -> bool: ...
    def take_created_tags(self) -> list[Tag]: ...      # iteration 4 (IT3-1), see TagRepository

class ListRepository(Protocol):
    async def list_lists(self, user_id: UUID) -> list[TodoList]: ...
    async def get(self, user_id: UUID, list_id: UUID) -> TodoList | None: ...
    async def get_default(self, user_id: UUID) -> TodoList: ...           # creates "Inbox" if none
    async def create(self, user_id: UUID, name: str) -> TodoList: ...
    async def rename(self, user_id: UUID, list_id: UUID, name: str) -> TodoList | None: ...
    async def delete(self, user_id: UUID, list_id: UUID) -> bool: ...

class TagRepository(Protocol):
    async def list_tags(self, user_id: UUID) -> list[Tag]: ...
    async def get_or_create_many(self, user_id: UUID, names: Sequence[str]) -> list[Tag]: ...
    async def rename(self, user_id: UUID, tag_id: UUID, name: str) -> Tag | None: ...
    async def delete(self, user_id: UUID, tag_id: UUID) -> bool: ...
    # iteration 4 (IT3-1): drains the tags this *request* actually inserted, so the
    # router can stage one `tag.created` frame each. Returns [] and is idempotent
    # when nothing was created. TodoRepository gets the same method, delegating to
    # the tag repository it owns — tags are created inside the todo write path.
    def take_created_tags(self) -> list[Tag]: ...

class UserRepository(Protocol):
    async def get_by_email(self, email: str) -> User | None: ...
    async def get(self, user_id: UUID) -> User | None: ...
    async def create(self, email: str, password_hash: str, display_name: str | None) -> User: ...
```

`TodoQuery` is a frozen dataclass carrying every filter/sort field from §6.3. It exists with
default values from **slice 1**; slice 3 only populates it. This keeps the protocol stable
across slices — no rework.

`TodoCreateData` / `TodoUpdateData` are dataclasses in `app/repositories/protocols.py`.
`TodoUpdateData` uses a sentinel (`UNSET`) per field so "set to null" and "don't touch" are
distinguishable.

### 4.3 Sessions and DI
- Engine created in `lifespan`, `pool_size=5, max_overflow=5, pool_pre_ping=True`, stored on
  `app.state.engine`; disposed on shutdown.
- `async_sessionmaker(engine, expire_on_commit=False, autoflush=False)`.
- `get_session()` yields a session, commits on clean exit, rolls back on exception, always closes.
- `get_todo_repository(session=Depends(get_session)) -> TodoRepository` etc. Tests override the
  session dependency, not the repository, so the real SQL is exercised.
- **All route handlers stay `async def`** (iteration-1 R8 still applies).

### 4.4 Health endpoints
- `GET /api/health` → `200 {"status":"ok"}`. **No DB access** — compose/Playwright probe.
- `GET /api/health/ready` → `200 {"status":"ok","database":"ok"}` or `503
  {"detail":"Database unavailable","code":"database_unavailable"}` (runs `SELECT 1`).

---

## 5. Error contract v2 (frozen)

**Decision D-E1:** keep FastAPI's `detail` key as a **human-readable string** (so iteration-1
clients and tests keep working) and add a sibling machine-readable `code`:

```json
{ "detail": "Todo not found", "code": "todo_not_found" }
```

Implemented by an `AppError(Exception)` hierarchy in `app/errors.py` plus an exception handler
registered for it, and a handler for `HTTPException` that adds `code` when the raiser supplied
one. Pydantic validation errors keep FastAPI's default 422 body **unchanged** and carry **no**
`code` — the frontend never parses the 422 structure (iteration-1 decision preserved).

| Situation | Status | `code` | `detail` |
|---|---|---|---|
| Unknown/malformed todo id, or a todo owned by someone else | 404 | `todo_not_found` | `Todo not found` |
| Unknown/malformed list id, or another user's list | 404 | `list_not_found` | `List not found` |
| Unknown/malformed tag id | 404 | `tag_not_found` | `Tag not found` |
| Missing / malformed / expired bearer token | 401 | `unauthorized` | `Not authenticated` |
| Bad email or password on login | 401 | `invalid_credentials` | `Invalid email or password` |
| Registering an already-used email | 409 | `email_taken` | `That email is already registered` |
| List name already used by this user | 409 | `list_name_taken` | `A list with that name already exists` |
| Tag name already used by this user | 409 | `tag_name_taken` | `A tag with that name already exists` |
| Deleting the user's only list | 409 | `cannot_delete_last_list` | `You must keep at least one list` |
| Adding a subtask to a subtask | 400 | `subtask_depth_exceeded` | `Subtasks can only be one level deep` |
| `PATCH` body with no updatable field | 400 | `empty_update` | `No fields to update` |
| `due` preset combined with `due_from`/`due_to` | 400 | `conflicting_due_filters` | `Use either a due preset or a due date range, not both` |
| `parent_id` with a `list_id` that differs from the parent's list | 400 | `subtask_list_mismatch` | `A subtask always belongs to its parent's list` |
| Body larger than 64 KiB | 413 | `request_too_large` | `Request body too large` |
| Rate limit hit (auth or AI) | 429 + `Retry-After` | `rate_limited` | `Too many requests. Please wait and try again.` |
| Schema validation failure | 422 | *(none)* | FastAPI/Pydantic default array |
| AI disabled by configuration | 503 | `ai_disabled` | `AI features are disabled` |
| Ollama/ai-agent unreachable or model missing | 503 | `ai_unavailable` | `AI service is unavailable` |
| AI request exceeded its timeout | 504 | `ai_timeout` | `AI request timed out` |
| DB unreachable on `/api/health/ready` | 503 | `database_unavailable` | `Database unavailable` |

**Decision D-E2 (security):** a resource owned by another user is reported as **404**, never 403.
No enumeration oracle. There is no `forbidden` code in iteration 2.

**Decision D-E3:** all request bodies use `model_config = ConfigDict(extra="forbid")`. This
**reverses iteration-1 D4** — unknown keys now yield 422. Slice 1 makes the change and updates
the iteration-1 test that asserted extra keys are ignored.

### 5.1 Iteration-4 delta — how `GET /api/ai/status.reason` maps onto this table

Added in **iteration 4** (`docs/specs/iteration-4-backlog-sweep.md`, IT3-2). `/api/ai/status`
still never fails; it now also says *why* it is unavailable. The `reason` value is a closed
machine enum, and each value corresponds to what the AI **POST** endpoints return in the same
situation — the mapping is normative:

| `status.reason` | Meaning | What `POST /api/ai/*` returns in that state |
|---|---|---|
| `null` | `available` is `true` | — (normal operation) |
| `disabled` | `AI_ENABLED=false` | 503 `ai_disabled` |
| `unreachable` | ai-agent did not answer (connect error, timeout, non-2xx, non-JSON) | 503 `ai_unavailable` |
| `auth_failed` | ai-agent answered `/health` but rejected our `X-Internal-Token` on `GET /ai/ping` — the two services disagree about `AI_AGENT_TOKEN` | 503 `ai_unavailable` |
| `model_unavailable` | ai-agent is reachable and authenticated, but Ollama is down or `OLLAMA_MODEL` was never pulled | 503 `ai_unavailable` |

`reason` is **never** a prose string and never quotes an ai-agent response body; three of the four
values collapse to the same 503 `ai_unavailable` on the POST endpoints, exactly as before —
nothing in §5 changes for error *bodies*. A client that receives an unrecognised `reason` must
treat it as `null` (generic copy), so adding a value later is not a breaking change.

Frontend counterpart (`src/api/errors.ts`, added in slice 1):
```ts
export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string | null, readonly detail: string) { … }
}
```
`ApiError.code` is the only thing UI logic branches on; `detail` is never rendered raw to the
user (all user-facing copy is defined per slice).

---

## 6. Frozen API contract v2

Base path `/api`. The frontend always uses **relative** URLs (Vite proxy in dev, nginx in
compose). Auth: `Authorization: Bearer <jwt>` on every endpoint except
`/api/health`, `/api/health/ready`, `/api/auth/register`, `/api/auth/login`.

Every **mutating** request (POST/PATCH/DELETE) should carry `X-Client-Id: <uuid>` — the sending
tab's id, used for realtime echo suppression (§7). The header is optional; when absent, `origin`
in the broadcast event is `null`.

### 6.1 Auth (slice 2)

#### `POST /api/auth/register`
```json
{ "email": "me@example.com", "password": "correct horse battery", "display_name": "Me" }
```
- `email`: `EmailStr`, ≤ 320 chars, normalized to lowercase+trimmed before storage and lookup.
- `password`: 8..128 characters, must contain at least one non-whitespace character. No
  composition rules (NIST SP 800-63B guidance).
- `display_name`: optional, trimmed, 1..100 when present.
- **201** → `UserResponse`. Also creates the user's default list named **`Inbox`**
  (`is_default=true`).
- **409** `email_taken` · **422** validation · **429** `rate_limited`.
- Registration does **not** return a token; the client calls login next (keeps one code path for
  session establishment).

#### `POST /api/auth/login`
```json
{ "email": "me@example.com", "password": "correct horse battery" }
```
- **200** →
  ```json
  { "access_token": "eyJ…", "token_type": "bearer", "expires_in": 3600,
    "user": { "id": "…", "email": "…", "display_name": null, "created_at": "…Z" } }
  ```
- **401** `invalid_credentials` — identical response and comparable timing for "unknown email"
  and "wrong password" (always run the argon2 verify against a dummy hash when the user is
  absent).
- **429** `rate_limited`.

#### `GET /api/auth/me`
- **200** → `UserResponse` · **401** `unauthorized`.

### 6.2 Lists (slice 2)
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| GET | `/api/lists` | — | 200 `ListResponse[]`, ordered `created_at ASC` | 401 |
| POST | `/api/lists` | `{"name":"Work"}` | 201 `ListResponse` | 409 `list_name_taken`, 422, 401 |
| PATCH | `/api/lists/{id}` | `{"name":"Work stuff"}` | 200 `ListResponse` | 404 `list_not_found`, 409, 422, 401 |
| DELETE | `/api/lists/{id}` | — | 204 (cascades its todos) | 404, 409 `cannot_delete_last_list`, 401 |

- `name`: trimmed, 1..100. Uniqueness per user is checked **case-insensitively** in the
  application (`.ilike()`), with `UNIQUE (user_id, name)` as the DB backstop.
- Deleting the default list is allowed when another list exists; the oldest remaining list
  becomes the default.

### 6.3 Todos

**Decision D-A1: todos stay at `/api/todos`, scoped by a `list_id` query parameter (reads) or
body field (writes) — no `/api/lists/{id}/todos` nesting.** Rationale: iteration-1 URLs survive
untouched (nothing to migrate in slice 1), cross-list views ("everything due today") are
expressible, and the id never appears twice in one request.

#### `GET /api/todos`
Returns **top-level todos only** (`parent_id IS NULL`); each carries its full `subtasks` array.
Filters apply to the parents; a parent's subtasks are always included regardless of filters.

| Param | Type | Default | Notes | Slice |
|---|---|---|---|---|
| `list_id` | uuid | *(all lists)* | 404 `list_not_found` if it is not the caller's | 2 |
| `status` | `all\|active\|completed` | `all` | | 3 |
| `priority` | repeatable or comma-separated `low,medium,high` | *(all)* | OR within the set | 3 |
| `tag` | repeatable tag name | *(none)* | **AND** semantics: the todo must have all of them; names are normalized before matching; an unknown tag name yields an empty result, not 404 | 3 |
| `due` | `any\|overdue\|today\|week\|none` | `any` | `week` = `today` ≤ due ≤ `today+6d`; `none` = `due_date IS NULL`; `overdue` = `due_date < today AND completed = false` | 3 |
| `due_from`, `due_to` | date | — | inclusive; 400 `conflicting_due_filters` if combined with `due != any` | 3 |
| `today` | date | server UTC date | the caller's *local* date, drives `due` presets | 3 |
| `q` | string 1..100 | — | case-insensitive substring over `title` OR `description`; matched against parents only | 3 |
| `sort` | `created_at\|updated_at\|due_date\|priority\|title` | `created_at` | | 3 |
| `order` | `asc\|desc` | `asc` | | 3 |
| `limit` | int 1..500 | 200 | | 3 |
| `offset` | int ≥ 0 | 0 | | 3 |

- Response is a **bare JSON array** (iteration-1 compatible), with the total matching count in the
  `X-Total-Count` response header (and `Access-Control-Expose-Headers: X-Total-Count` in CORS).
- Sorting rules (must be implemented exactly): rows with `due_date IS NULL` always sort **last**,
  in both `asc` and `desc`, via `ORDER BY (due_date IS NULL) ASC, due_date <dir>`. `priority`
  ascending means **high → medium → low** (`CASE high→0, medium→1, low→2`). Every sort gets the
  stable tiebreak `created_at ASC, id ASC`. `title` sorts case-insensitively (`lower(title)`).
- Subtasks inside a parent are always ordered `created_at ASC, id ASC`.
- Unknown query parameters are ignored (FastAPI default) — slice-1 clients keep working.
- **422** on a malformed enum/date/int value.

#### `POST /api/todos`
```jsonc
{ "title": "Buy milk",            // required, trimmed, 1..200
  "list_id": "uuid",              // optional → the caller's default list   (slice 2)
  "description": "2% please",     // optional, ≤2000                        (slice 3)
  "priority": "high",             // optional, default "medium"             (slice 3)
  "due_date": "2026-09-05",       // optional                               (slice 3)
  "tags": ["errand", "home"],     // optional, names; auto-created per user (slice 3)
  "parent_id": "uuid"             // optional → creates a subtask           (slice 3)
}
```
`extra="forbid"`. Slice 1 accepts `{title}` only; slice 2 adds `list_id`; slice 3 adds the rest.
- **201** → `TodoResponse` · **400** `subtask_depth_exceeded` · **404** `list_not_found` /
  `todo_not_found` (unknown `parent_id`) · **422** · **401**.
- No `Location` header (iteration-1 D3 preserved).

#### `GET /api/todos/{id}` → 200 `TodoResponse` (with `subtasks`) · 404 `todo_not_found`.

#### `PATCH /api/todos/{id}` — **partial** from slice 3
```jsonc
{ "title": "…", "description": null, "completed": true, "priority": "low",
  "due_date": null, "tags": [], "list_id": "uuid", "parent_id": null }
```
- Every field optional; explicit `null` clears (`description`, `due_date`, `parent_id`);
  `tags: []` removes all tags. `extra="forbid"`.
- `{}` → **400** `empty_update`.
- **Slices 1–2 only**: `PATCH` still requires exactly `{"completed": bool}` (iteration-1 D2).
  Slice 3 relaxes it to partial and updates the corresponding tests. This is a deliberate,
  scheduled contract change — the frontend `setCompleted()` call site is unaffected because
  `{"completed": …}` remains valid in both regimes.
- Changing `list_id` moves the todo and all its subtasks. Changing `parent_id` from `null` to a
  value converts a top-level todo into a subtask **only if it has no subtasks of its own**
  (otherwise 400 `subtask_depth_exceeded`).
- Setting `completed` on a parent does **not** cascade to subtasks (decision D-A2: subtask
  completion is independent; the UI shows `2/5 done`).
- **200** `TodoResponse` (the *affected top-level* todo when the target is a subtask? **No** —
  the response is always the patched todo itself; the SSE event carries the top-level todo, §7).

#### `DELETE /api/todos/{id}` → **204**, cascades subtasks · **404** `todo_not_found`.

#### `POST /api/todos/{id}/subtasks` (slice 3, convenience)
Body `{"title": "…", "description"?, "priority"?, "due_date"?, "tags"?}`.
- **201** `TodoResponse` (the new subtask, `parent_id` = `{id}`).
- **400** `subtask_depth_exceeded` when `{id}` is itself a subtask · **404** `todo_not_found`.

### 6.4 Tags (slice 3)
| Method | Path | Body | Success | Errors |
|---|---|---|---|---|
| GET | `/api/tags` | — | 200 `TagResponse[]` ordered `name ASC` | 401 |
| PATCH | `/api/tags/{id}` | `{"name":"errands"}` | 200 `TagResponse` | 404 `tag_not_found`, 409 `tag_name_taken`, 422 |
| DELETE | `/api/tags/{id}` | — | 204 (removes all its associations) | 404 |

There is no `POST /api/tags` — tags are created implicitly by using them on a todo
(**decision D-A3**; it matches how the AI suggests tags and avoids a second creation path).

### 6.5 Realtime (slice 4)
`GET /api/events` — see §7.

### 6.6 AI (slice 4) — all POST, all rate-limited 20 req / 5 min / user
| Endpoint | Request | Success 200 |
|---|---|---|
| `GET /api/ai/status` | — | `{"enabled":true,"available":true,"model":"qwen2.5:3b","reason":null}` — `reason` added in **iteration 4**, see §5.1 |
| `POST /api/ai/parse-todo` | `{"text":"call the dentist tomorrow morning, urgent","today":"2026-09-02"}` | `{"draft":{"title":"Call the dentist","description":null,"priority":"high","due_date":"2026-09-03","tags":["health"],"subtasks":[{"title":"Find the phone number"}]}}` |
| `POST /api/ai/suggest-subtasks` | `{"todo_id":"uuid","max_items":5}` | `{"subtasks":[{"title":"…"},…]}` |
| `POST /api/ai/suggest-metadata` | `{"todo_id":"uuid"}` | `{"priority":"high","tags":["work","urgent"]}` |
| `POST /api/ai/daily-summary` | `{"list_id":"uuid"\|null,"today":"2026-09-02"}` | `{"summary":"You have 5 open todos…","todo_count":5,"generated_at":"…Z"}` |
| `POST /api/ai/edit-todo` | `{"todo_id":"uuid","instruction":"…","today":"2026-09-10"}` | `{"todo_id":"uuid","empty":false,"context":{…},"change_set":{…}}` — **iteration 5**, see `docs/specs/iteration-5-ai-edit-by-instruction.md` §2.1 |

**Decision D-AI1: AI endpoints never write to the database.** They return *drafts*; the user
reviews and confirms, and the frontend then calls the ordinary `POST /api/todos` /
`POST /api/todos/{id}/subtasks` / `PATCH /api/todos/{id}`. Rationale: an LLM must not be an
unreviewed write path; it also makes every AI endpoint trivially idempotent and testable.

`GET /api/ai/status` is the only AI endpoint that must never fail: when AI is disabled or Ollama
is down it returns 200 with `enabled`/`available` flags so the UI can hide or disable AI
controls. All other AI endpoints return 503/504 per §5 when the model is unreachable.

---

## 7. Realtime design (slice 4)

**Decision D-R1: Server-Sent Events, not WebSocket.**
Justification: the requirement is strictly one-way (server → client). SSE rides on ordinary
HTTP/1.1, so the Vite dev proxy and the nginx compose proxy need no `Upgrade` handling (only
`proxy_buffering off`), it reconnects automatically, there is no second protocol to secure, and a
dropped stream degrades to "this tab is stale" rather than a broken app. WebSocket would add a
protocol, a heartbeat/ping frame protocol, and an auth handshake for zero benefit here.

### 7.1 Endpoint
`GET /api/events` → `200 text/event-stream`
Headers: `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`.

**Auth without leaking tokens into URLs:** native `EventSource` cannot set headers, so the
frontend does **not** use it. `src/api/events.ts` opens the stream with `fetch()` +
`Authorization: Bearer` and parses the `ReadableStream` with a ~60-line SSE reader
(`TextDecoderStream`, split on `\n\n`, handle `event:`, `data:` (concatenate multiple `data:`
lines with `\n`), ignore lines starting with `:`). No extra npm dependency, and **no token ever
appears in a URL or a server log**.

### 7.2 Frames
```
event: ready
data: {"user_id":"uuid"}

event: todo.created
data: {"origin":"<client-id or null>","todo":{…TodoResponse…}}

event: todo.updated
data: {"origin":…,"todo":{…TodoResponse…}}

event: todo.deleted
data: {"origin":…,"id":"uuid","list_id":"uuid"}

event: list.created | list.updated
data: {"origin":…,"list":{…ListResponse…}}

event: list.deleted
data: {"origin":…,"id":"uuid"}

: keep-alive          ← comment frame every 25 s
```
- **Any mutation of a subtask broadcasts `todo.updated` carrying the *top-level parent*** (fully
  embedded subtasks), so a receiver can replace one array element. Deleting a subtask likewise
  broadcasts `todo.updated` of the parent; deleting a top-level todo broadcasts `todo.deleted`.
- **Echo suppression:** the mutating tab sends `X-Client-Id`; the broker copies it into `origin`.
  A client ignores every event whose `origin` equals its own client id (it already applied the
  change from the HTTP response). This removes double-apply flicker.
- Events are **per user**: the broker only ever pushes to queues registered for the row's
  `user_id`.

#### Iteration-4 delta — `tag.*` frames

Added in **iteration 4** (`docs/specs/iteration-4-backlog-sweep.md`, IT3-1). The frames above are
unchanged; these are new members of the same contract and obey every rule in §7.2 and §7.3
(per-user delivery, `origin` echo suppression, publish-**after**-commit).

```
event: tag.created | tag.updated
data: {"origin":"<client-id or null>","tag":{…TagResponse…}}

event: tag.deleted
data: {"origin":…,"id":"uuid"}
```

- `TagResponse` is the §3.3 shape in full: `{"id","name","todo_count"}`.
- **`tag.created`** is emitted where a tag actually comes into existence. Per D-A3 there is no
  `POST /api/tags`, so that is only ever a todo write that names a tag the user did not have yet:
  `POST /api/todos`, `PATCH /api/todos/{id}`, `POST /api/todos/{id}/subtasks`. One frame per newly
  created tag, staged **before** the `todo.*` frame of the same request, so a receiver applying
  frames in order never sees a todo naming a tag it has not been told about.
  `todo_count` on such a frame is `1` when the todo being written is top-level and `0` when it is a
  subtask — exact without a query, because `TagResponse.todo_count` counts top-level todos only and
  a tag created in this request can be attached to nothing else yet (the same reasoning as
  `list.created` in `app/routers/lists.py`).
- **`tag.updated`** comes from `PATCH /api/tags/{id}`; `todo_count` is the value that endpoint
  already computes for its own response.
- **`tag.deleted`** comes from `DELETE /api/tags/{id}` and carries the id only, like `list.deleted`.
- **No per-todo fan-out.** Renaming or deleting a tag changes the `tags` array of arbitrarily many
  todos, and emitting one `todo.updated` per affected row would turn one request into an unbounded
  burst. Instead the contract is explicit: **a receiver must treat `tag.updated` and `tag.deleted`
  as invalidating its todo view as well as its tag vocabulary** and refetch both. The reference
  client already refetches todos, lists and tags on any frame (`App.tsx::refetchView`).
- A client whose active filter names a tag that a `tag.updated`/`tag.deleted` frame changed must
  drop or rename it in its own filter state; otherwise it keeps filtering on a name the server no
  longer knows and silently shows "no matches" forever (unknown tag names filter to empty, §6.3).

### 7.3 Server implementation
`app/events.py`:
- `EventBroker` holds `dict[UUID, set[asyncio.Queue[str]]]`. `subscribe(user_id)` is an async
  context manager registering a `Queue(maxsize=100)`; `publish(user_id, event)` does
  `put_nowait` and, on `QueueFull`, drops the queue and closes that stream (a stalled client must
  not grow memory — the client reconnects and refetches).
- One broker instance per app, on `app.state.broker`, injected via `Depends`.
- The SSE route is an `async def` generator returning `StreamingResponse`; it emits `ready`
  first, then races `queue.get()` against a 25 s timeout for the keep-alive comment, and exits
  on `asyncio.CancelledError` / client disconnect.
- **Single-replica assumption is explicit**: the broker is in-process. Scaling out would require
  Postgres `LISTEN/NOTIFY` or Redis pub/sub — out of scope, documented in the README.
  **Iteration-4 delta (IT3-4):** the assumption stops being implicit. A backend setting
  `REALTIME_BACKEND` (§9.1) is introduced with exactly one accepted value, `memory`; any other
  value — including `redis` — makes the application **fail to start** with a message naming the
  supported value. The backend also logs one INFO line at startup stating that fan-out is
  in-process and that exactly one replica may run. `EventBroker.subscribe` / `EventBroker.publish`
  remain the only two seams a future distributed backend has to replace. **No Redis, and no other
  distributed fan-out, is added in iteration 4.**
- The `BodySizeLimitMiddleware` must not touch responses (it is request-side only) — verified by
  an SSE test.

### 7.4 Client behaviour
- Connect after login; disconnect on logout/token clear.
- On `ready`: refetch the current view once (covers events missed while disconnected).
- Reconnect with backoff `1s, 2s, 5s, 10s, 30s` (capped), resetting on a successful `ready`.
- After 3 consecutive failed connects, fall back to polling the current view every 15 s and keep
  retrying the stream in the background. The user is never shown a connection error; realtime is
  an enhancement, not a feature gate.
- A 401 on the stream triggers the same session-expiry flow as any other 401.

---

## 8. AI service (slice 4)

### 8.1 Topology decision
**Decision D-AI2: the frontend never talks to `ai-agent` directly; the backend proxies.**
Rationale: one auth surface (JWT), one origin (no extra CORS), the ai-agent stays a private
service on the compose network, and the backend is the only component that may read a user's
todos to build a prompt. `ai-agent` is published on **8020 for debugging only** and is protected
by a shared secret header `X-Internal-Token: $AI_AGENT_TOKEN` on every route except `/health`.

### 8.2 `ai-agent` internal contract (`ai-agent/` FastAPI, port 8000 in-container)
All requests require `X-Internal-Token`; a missing/wrong token → 401
`{"detail":"Invalid internal token","code":"unauthorized"}`.

| Endpoint | Request | Response 200 |
|---|---|---|
| `GET /health` | — | `{"status":"ok","ollama":"ok"\|"unavailable","model":"qwen2.5:3b","model_present":true}` |
| `POST /ai/parse-todo` | `{"text":"…","today":"2026-09-02","known_tags":["home","work"]}` | `{"title":"…","description":null,"priority":"medium","due_date":null,"tags":[],"subtasks":[{"title":"…"}]}` |
| `POST /ai/suggest-subtasks` | `{"title":"…","description":null,"max_items":5}` | `{"subtasks":[{"title":"…"}]}` |
| `POST /ai/suggest-metadata` | `{"title":"…","description":null,"known_tags":[…],"today":"2026-09-02"}` | `{"priority":"medium","tags":["…"]}` |
| `POST /ai/daily-summary` | `{"today":"2026-09-02","todos":[{"title","priority","due_date","completed","list_name"}]}` | `{"summary":"…"}` |
| `POST /ai/edit-todo` | `{"instruction":"…","today":"2026-09-10","known_tags":[…],"todo":{…,"subtasks":[{"title","completed"}]}}` | positional change proposal — **iteration 5**, see `docs/specs/iteration-5-ai-edit-by-instruction.md` §2.2. The snapshot is positional (1-based subtask indexes) and carries no identifiers (D-IT5-3). |

`/health` never depends on Ollama being up (it reports, it does not fail).

**Iteration-4 delta (IT3-2):** one endpoint is added, `GET /ai/ping`.

| Endpoint | Request | Response 200 |
|---|---|---|
| `GET /ai/ping` | — | `{"status":"ok"}` |

It lives on the existing `/ai` router, so it inherits `X-Internal-Token` verification: a missing
or wrong token is the same 401 `{"detail":"Invalid internal token","code":"unauthorized"}` as
every other `/ai/*` route. It touches nothing else — no Ollama call, no model load, no disk — so
it is safe to call on every backend health probe. Its only purpose is to let the backend tell
"ai-agent is unreachable" apart from "ai-agent is up but we disagree about `AI_AGENT_TOKEN`",
which `/health` (unauthenticated by design) can never reveal. See §5.1 for the resulting
`status.reason` values.

### 8.3 Prompt & JSON strategy
- Call `POST {OLLAMA_BASE_URL}/api/chat` with `stream: false`, `options: {"temperature": 0.2,
  "num_predict": 512}`, and **`format` set to the JSON Schema** of the expected response object
  (Ollama structured outputs). If the server rejects a schema `format`, retry once with
  `"format": "json"`.
- System prompt per endpoint, short and imperative, e.g. for parse-todo:
  > You convert a user's note into a single todo item. Reply with JSON only, matching the given
  > schema. `title` ≤ 200 chars, imperative, no trailing punctuation. `priority` is one of low,
  > medium, high — use medium unless the note clearly signals urgency. `due_date` is an ISO date
  > resolved against TODAY={today}; use null when no date is implied. `tags` are 0–3 short
  > lowercase keywords, preferring ones from KNOWN_TAGS. `subtasks` are 0–5 short steps, empty
  > when the note is already a single action.
- **Validate every model response with Pydantic** (`extra="ignore"` here — the model may babble
  extra keys). On validation failure, retry **once** with a repair message containing the
  validator errors; on a second failure return 503 `ai_invalid_response`.
- **Post-validation clamping is mandatory** (never trust the model): trim titles to 200 chars,
  drop empty titles, cap `subtasks` at `max_items` (≤10), normalize tags with the §3.1 tag rules
  and drop invalid ones, cap tags at 5, coerce an unknown `priority` to `medium`, reject a
  `due_date` that fails to parse (→ null), cap the summary at 800 chars.
- Timeouts: `httpx.Timeout(connect=5, read=OLLAMA_TIMEOUT_SECONDS (default 45), write=10,
  pool=5)`. Backend → ai-agent timeout **50 s**. Frontend aborts at **60 s** via `AbortController`.
- Degradation ladder — the app must be fully usable with no AI at all:
  `ollama down / model missing` → ai-agent 503 `ai_unavailable` → backend 503 `ai_unavailable`
  → frontend shows the AI-unavailable copy inline and leaves all manual controls working.

---

## 9. Configuration

### 9.1 Backend settings (`app/config.py`, `pydantic-settings`)
| Var | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://todo:todo@localhost:5433/todo` | async SQLAlchemy URL |
| `TEST_DATABASE_URL` | *(unset)* | when set, pytest runs against it instead of SQLite |
| `APP_ENV` | `dev` | `dev` \| `prod`; when ≠ `dev`, `/docs`, `/redoc` and `/openapi.json` are **disabled** (`docs_url=None, redoc_url=None, openapi_url=None`) — closes an iteration-1 carry-over |
| `JWT_SECRET` | *(required, no default)* | HS256 key; the app **fails to start** if unset or shorter than 32 chars |
| `JWT_EXPIRES_MINUTES` | `60` | access-token lifetime |
| `CORS_ORIGINS` | `http://localhost:5173` | comma-separated exact origins |
| `MAX_BODY_BYTES` | `65536` | body-size middleware |
| `AUTH_ENABLED` | `false` **in slice 1 only — removed in slice 2** | see §11.1 |
| `AI_ENABLED` | `true` | master switch for `/api/ai/*` |
| `AI_AGENT_URL` | `http://ai-agent:8000` | |
| `AI_AGENT_TOKEN` | *(required when `AI_ENABLED`)* | shared secret |
| `AI_TIMEOUT_SECONDS` | `50` | backend → ai-agent |
| `REALTIME_BACKEND` | `memory` | **iteration 4** (IT3-4). Only `memory` is implemented; any other value fails startup. Reserved so that a future `redis` (or `postgres`) fan-out is an additive change rather than a new name to invent under pressure. `memory` implies **exactly one backend replica** (§7.3) |

`ai-agent` settings: `OLLAMA_BASE_URL` (`http://ollama:11434`), `OLLAMA_MODEL` (`qwen2.5:3b`),
`OLLAMA_TIMEOUT_SECONDS` (`45`), `AI_AGENT_TOKEN` (required), `LOG_LEVEL` (`info`).

**No secret ever has a hardcoded fallback in code.** `.env.example` carries obvious placeholders
(`change-me-…`), `.env` is already gitignored (`.env.*` with `!.env.example`).

### 9.2 `.env.example` (project root, devops owns it — slice 1 creates it, later slices extend)
```dotenv
# --- database ---
POSTGRES_USER=todo
POSTGRES_PASSWORD=change-me-locally
POSTGRES_DB=todo
DB_HOST_PORT=5433
DATABASE_URL=postgresql+asyncpg://todo:change-me-locally@db:5432/todo

# --- backend ---
APP_ENV=dev
BACKEND_HOST_PORT=8010
# generate with: openssl rand -hex 32
JWT_SECRET=change-me-generate-with-openssl-rand-hex-32
JWT_EXPIRES_MINUTES=60
CORS_ORIGINS=http://localhost:5173

# --- frontend ---
FRONTEND_HOST_PORT=5173

# --- ai (compose profile "ai") ---
AI_ENABLED=true
AI_AGENT_HOST_PORT=8020
AI_AGENT_URL=http://ai-agent:8000
AI_AGENT_TOKEN=change-me-internal-shared-secret
AI_TIMEOUT_SECONDS=50
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_TIMEOUT_SECONDS=45
```
The `db` service mounts `docker/db/init/01-create-test-db.sql` creating a second database
`todo_test` (used by the Postgres pytest lane).

---

## 10. Docker topology

```
docker-compose.yml
├── db         postgres:17-alpine   5433→5432   volume pgdata     healthcheck pg_isready
├── backend    ./backend            8010→8000   depends_on db(healthy)
│                                   entrypoint: alembic upgrade head && uvicorn
├── frontend   ./frontend           5173→80     nginx, proxies /api → backend:8000
├── ai-agent   ./ai-agent           8020→8000   profile: ai   depends_on ollama(started)
├── ollama     ollama/ollama        (not published)  profile: ai  volume ollama_models
└── ollama-pull ollama/ollama       one-shot      profile: ai  pulls $OLLAMA_MODEL, restart: "no"
```
- Network: single user-defined bridge `todo-net`; services address each other by name.
- **Profiles**: `db`, `backend`, `frontend` start with plain `docker compose up -d`. The AI trio
  is under `profiles: ["ai"]` → `docker compose --profile ai up -d`. This makes "the app works
  without AI" the *default*, not a fallback.
- `ollama-pull` runs `ollama pull "$OLLAMA_MODEL"` against `OLLAMA_HOST=http://ollama:11434`,
  exits 0, and is **not** a dependency of `ai-agent` — the stack must come up even if the pull
  fails (no internet, disk full). `ai-agent` then reports `available:false` and the UI hides AI.
- Images: backend & ai-agent on `python:3.12-slim` with uv (`COPY --from=ghcr.io/astral-sh/uv`),
  non-root user, `uv sync --locked --no-dev`. Frontend multi-stage `node:22-alpine` →
  `nginx:1.27-alpine`. All three have `.dockerignore`.
- nginx config must be SSE-safe on `/api`:
  `proxy_http_version 1.1; proxy_set_header Connection ""; proxy_buffering off;
   proxy_read_timeout 3600s;` plus SPA fallback `try_files $uri /index.html;`.
- Healthchecks: `db` → `pg_isready`; `backend` → `GET /api/health`; `frontend` → `wget -q -O- /`;
  `ollama` → `ollama list`.
- **No published port for ollama.** A commented-out `11435:11434` line documents the debug option
  and explains why 11434 is unusable (host Ollama).

---

## 11. Slice plan

Slices are executed **strictly in order**; each merges to `main` (reviewer + security + QA gates)
before the next starts. Within a slice, the listed agents work in **parallel worktrees** with
disjoint file ownership.

### File ownership (unchanged rule from iteration 1 — prevents worktree conflicts)
| Path | Owner |
|---|---|
| `backend/**` | backend agent |
| `ai-agent/**` | a **second backend agent dispatch** (slice 4), disjoint from `backend/**` |
| `frontend/**` | frontend agent |
| `docker-compose*.yml`, `docker/**`, `*/Dockerfile`, `*/.dockerignore`, `.env.example`, `README.md`, `.gitignore`, `.github/**` | devops agent |
| `docs/**` | planner |

Exception, explicitly granted: the **backend** agent owns `backend/Dockerfile` content *review*
but devops writes it; if the backend needs an entrypoint script it lives at
`backend/docker-entrypoint.sh` and is written by **devops**. The frontend agent likewise does not
write `frontend/Dockerfile` or `docker/nginx.conf`.

### 11.1 Slice 1 — Persistence foundation + Docker base
File: `docs/specs/iteration-2-slice-1-persistence-foundation.md`

Swap the in-memory store for PostgreSQL behind the same (now owner-aware) repository protocol,
create the full baseline schema, dockerize db/backend/frontend, and clear the iteration-1
carry-over defects. **The UI keeps working exactly as in iteration 1.**

**Decision D-S1 — how slice 1 has owners before auth exists:** the schema (and therefore
`users`) exists from the baseline migration. With `AUTH_ENABLED=false`, the app bootstraps a
single **local user** on startup (`get_or_create` by the fixed email `local@todo.app`, id
`uuid5(NAMESPACE_URL, "https://todo.app/users/local")`, an unusable random password hash, and an
`Inbox` default list), and `get_current_user()` returns it. Slice 2 replaces the body of that one
dependency with real JWT verification and **deletes the flag entirely** — no permanent auth
bypass survives into slice 2. Dev data created under the local user is orphaned at that point
(acceptable: it is throwaway dev data; the README says so).

| Agent | Complexity | Scope |
|---|---|---|
| BACKEND | **complex** (opus) | SQLAlchemy models, `UtcDateTime`, session/DI, Alembic baseline, `SqlAlchemyTodoRepository`, error contract v2, `extra="forbid"`, `/docs` gating, body-size middleware fix, local-user bootstrap, pytest dual-engine harness |
| DEVOPS | **intermediate** (sonnet) | `docker-compose.yml` (db/backend/frontend), three Dockerfiles + `.dockerignore`, `docker/nginx.conf`, `docker/db/init/*.sql`, `backend/docker-entrypoint.sh`, `.env.example`, README rewrite, CI: SQLite job + Postgres service job with `alembic upgrade/downgrade/check` |
| FRONTEND | **mechanical** (haiku) | `ApiError`, `X-Client-Id`, proxy target 8010, `tsconfig.e2e.json` `DOM` lib, focus-preserving row busy state, types extended to the full `TodoResponse` |
| QA | intermediate (sonnet) | full suite + E2E + a `docker compose up` smoke run |

Depends on: nothing. Unblocks: everything.

### 11.2 Slice 2 — Auth, users, multiple lists
File: `docs/specs/iteration-2-slice-2-auth-and-lists.md`

| Agent | Complexity | Scope |
|---|---|---|
| BACKEND | **complex** (opus) | argon2 hashing, JWT issue/verify, `/api/auth/*`, `get_current_user` (real), rate limiter, `/api/lists` CRUD, `list_id` on todos, per-user scoping tests incl. cross-user isolation, removal of `AUTH_ENABLED` |
| FRONTEND | **complex** (opus) | `AuthContext`, login/register screens, token storage, 401 → session-expiry flow, list switcher + create/rename/delete, `X-Client-Id`, E2E auth fixture |
| DEVOPS | **mechanical** (haiku) | `JWT_SECRET` in compose/CI/`.env.example`, README auth section |
| QA | intermediate (sonnet) | cross-user isolation matrix, auth E2E |

Depends on: slice 1 (schema, error contract, DI).
**Frontend depends on the frozen §6.1/§6.2 contract only — it can start the moment slice 2 is
dispatched, in parallel with backend.**

### 11.3 Slice 3 — Todo enrichment: priority, due date, tags, subtasks, filter/sort/search
File: `docs/specs/iteration-2-slice-3-todo-enrichment.md`

| Agent | Complexity | Scope |
|---|---|---|
| BACKEND | **complex** (opus) | full create/update surface, partial PATCH, tag get-or-create + `/api/tags`, subtask endpoint + invariants I1–I4, `TodoQuery` → SQL (filters, search, sorting incl. null/priority ordering), `X-Total-Count` |
| FRONTEND | **complex** (opus) | todo detail/edit form, priority & due-date controls, tag input, subtask list with `n/m done`, filter bar (status/priority/tag/due), sort control, search box with debounce |
| DEVOPS | — | none (no infra change) |
| QA | intermediate (sonnet) | filter/sort matrix, subtask depth edge cases, E2E |

Depends on: slice 2.

### 11.4 Slice 4 — Realtime + ai-agent + Ollama + AI UI
File: `docs/specs/iteration-2-slice-4-realtime-and-ai.md`

| Agent | Complexity | Scope |
|---|---|---|
| BACKEND-A (`backend/**`) | **complex** (opus) | `EventBroker`, `GET /api/events`, publishing from every mutation, `X-Client-Id` origin, `ai_client.py`, `/api/ai/*` proxy endpoints + rate limit + prompt-context assembly |
| BACKEND-B (`ai-agent/**`) | **complex** (opus) | the whole `ai-agent` FastAPI service, Ollama client, JSON-schema prompting, validation/repair/clamping, mocked tests + one `AI_AGENT_LIVE=1` integration test |
| FRONTEND | **complex** (opus) | SSE client + reconnect/poll fallback, live merge into state, AI add-box, "Split into subtasks", "Suggest priority & tags", daily-summary panel, unavailable states |
| DEVOPS | **intermediate** (sonnet) | `ai-agent` Dockerfile, `ollama` + `ollama-pull` services under profile `ai`, `docker-compose.gpu.yml`, nginx SSE settings, `.env.example` AI block, README AI/GPU section |
| QA | intermediate (sonnet) | two-tab realtime E2E, AI-unavailable degradation, optional live-AI check |

Depends on: slice 3 (BACKEND-A needs the final todo shape; BACKEND-B depends on nothing but §8
and can run fully in parallel).

---

## 12. Iteration-1 carry-overs — where each is fixed

| # | Carry-over | Resolution | Slice |
|---|---|---|---|
| C1 | Repository protocol needs an owner/user parameter | Every repository method takes `user_id` first (§4.2); with `AUTH_ENABLED=false` it is the bootstrap local user (D-S1) | 1 |
| C2 | Split record model from response model | `app/db/models.py` (ORM) vs `app/schemas/**` (Pydantic) (§3.2) | 1 |
| C3 | Consider `extra="forbid"` | Adopted for **all request models** (D-E3); reverses iteration-1 D4; the iteration-1 "extra keys ignored" test is rewritten to assert 422 | 1 |
| C4 | Body-size middleware only checks `Content-Length` | Middleware also wraps `receive` and counts streamed bytes, aborting with 413 once `MAX_BODY_BYTES` is exceeded even without/with a lying `Content-Length`; test sends a chunked body | 1 |
| C5 | Gate `/docs` behind an env flag outside dev | `APP_ENV != "dev"` → `docs_url/redoc_url/openapi_url = None`; test asserts 404 for all three with `APP_ENV=prod` | 1 |
| C6 | `tsconfig.e2e.json` lacks the `dom` lib | `"lib": ["ES2023", "DOM"]` | 1 |
| C7 | Focus lost after toggling a row (rows disable in flight) | Controls are no longer `disabled` while busy; the row gets `aria-busy="true"`, controls get `aria-disabled="true"`, and handlers no-op while busy. Focus therefore stays on the control the user activated; a Vitest test asserts `document.activeElement` is unchanged after a toggle | 1 |

---

## 13. Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | SQLite/Postgres divergence hides a bug until compose runs | "green" tests, broken app | D-T1 portability rules; Postgres CI job runs the same suite + `alembic upgrade/downgrade/check`; QA smoke-tests `docker compose up` every slice |
| R2 | Async SQLAlchemy lazy-load raises `MissingGreenlet` in a response serializer | 500s on real data | Mandatory `selectinload` for `subtasks` and `tags`; `expire_on_commit=False`; a test asserts serialization outside the session scope |
| R3 | argon2 default parameters use 64 MiB per hash; concurrent logins could spike memory | slow/failed logins | Personal-scale app + login rate limit (10 / 15 min per IP+email); parameters are configurable constants in `security.py` |
| R4 | JWT in `localStorage` is XSS-readable | account takeover | No `dangerouslySetInnerHTML` anywhere (grep-checked), React escaping, no `eval`, no third-party script tags, 60-min token lifetime. Cookies were rejected because a Bearer JSON API is CSRF-immune by construction; recorded as a deliberate tradeoff |
| R5 | The in-process `EventBroker` breaks silently with >1 backend replica | some tabs never update | Documented single-replica constraint; compose runs one replica; `ready`-triggered refetch and the 15 s polling fallback mean staleness is bounded even if fan-out is lost |
| R6 | SSE stream buffered by a proxy → events arrive in bursts or never | realtime appears broken | `X-Accel-Buffering: no` from the app + `proxy_buffering off` in nginx; Vite's dev proxy does not buffer; two-tab E2E is the acceptance test |
| R7 | `qwen2.5:3b` returns invalid or hallucinated JSON | AI features error out | JSON-schema `format`, Pydantic validation, one repair retry, then mandatory clamping; unit tests use recorded good/bad/garbage responses |
| R8 | Ollama model pull fails (no network / disk) or is slow on first run | AI silently missing | `ollama-pull` is a non-blocking one-shot; `GET /api/ai/status` drives the UI; README documents `docker compose --profile ai run --rm ollama-pull` |
| R9 | CPU-only Ollama on a 3B model may take 20–40 s per call | UI feels hung | 45/50/60 s timeout ladder, per-button spinners with copy "Thinking… this can take up to a minute", GPU override documented |
| R10 | Baseline migration ships unused tables (D-M1) — a reviewer may flag it as speculative | churn | Explicitly decided here with rationale; slice-1 spec repeats it |
| R11 | Scheduled contract change: `PATCH /api/todos/{id}` goes from required-`completed` to partial in slice 3 | frontend/tests break mid-iteration | `{"completed": …}` stays valid in both regimes, so no call site changes; the slice-3 spec lists the exact tests to update |
| R12 | Host ports 5432/8000/11434 belong to another project | port clashes, or worse, talking to the wrong DB/Ollama | Fixed host-port map (§1.1) used in compose, README, Vite proxy and Playwright; `DATABASE_URL` is never defaulted to `localhost:5432` |
| R13 | Playwright browser download path differs per agent | flaky/failed E2E | `PLAYWRIGHT_BROWSERS_PATH=frontend/node_modules/.cache/ms-playwright` is mandated in every slice spec and in the README |
| R14 | Four sequential slices touching the same files (`App.tsx`, `todos.py`) create rebase pain | lost work | Strict slice ordering (merge before next dispatch) + file-ownership table; no two agents ever hold the same file in one slice |
| R15 | `.env` accidentally committed | leaked secrets | Already gitignored (`.env.*` + `!.env.example`); security reviewer greps each slice for secret-like literals |

---

## 14. Decisions recorded (closed — do not re-litigate)

- **D-P1** Local backend dev port moves 8000 → 8010 (Vite proxy + Playwright default follow).
- **D-P2** Our Ollama is compose-internal only (`ollama:11434`); host 11434 is foreign.
- **D-T1** Tests run on SQLite (`aiosqlite`) by default and on Postgres when `TEST_DATABASE_URL`
  is set; the schema is kept portable by the rules in §2; CI runs both lanes.
- **D-M1** One baseline migration creating the full target schema in slice 1.
- **D-S1** Slice 1 bootstraps a local user behind `AUTH_ENABLED=false`; slice 2 deletes the flag.
- **D-A1** Todos live at `/api/todos` with `list_id` as a query/body field — no nested list routes.
- **D-A2** Completing a parent does not cascade to subtasks.
- **D-A3** No `POST /api/tags`; tags are created implicitly by name when used on a todo.
- **D-E1** Error body is `{"detail": "<human string>", "code": "<machine code>"}`; 422 keeps
  FastAPI's default shape and has no `code`.
- **D-E2** Another user's resource → 404, never 403.
- **D-E3** `extra="forbid"` on all request models (reverses iteration-1 D4).
- **D-R1** Realtime via **SSE**, consumed with `fetch` + streaming (not `EventSource`) so the
  bearer token stays out of URLs.
- **D-AI1** AI endpoints return drafts and never write to the database.
- **D-AI2** The frontend calls AI only through the backend; `ai-agent` is internal and
  shared-secret protected.
- **D-AI3** Default model `qwen2.5:3b`, CPU by default, GPU via `docker-compose.gpu.yml`.
- **D-F1** No router library and no data-fetching library on the frontend: `useState` +
  `useContext` + native `fetch`, consistent with iteration 1. Filters live in component state,
  not in the URL.
- **D-F2** Token in `localStorage` under `todo.auth.token` (+ `todo.auth.user`); tab id in
  `sessionStorage` under `todo.client-id`.
- **D-F3** Priority/tag/date UI uses native HTML controls (`<select>`, `<input type="date">`) —
  no component library, keeps WCAG AA achievable and the bundle small.
- **D-D1** `due_date` is a **date**, never a datetime.
- **D-D2** Tags are stored normalized lowercase; the UI displays them as stored.
- Iteration-1 **D1** (str path params → 404 on malformed id), **D3** (no `Location` header),
  **D6** (no optimistic updates), **D8** (Tailwind v4), **D9** (server-side ordering), **D12**
  (no `dangerouslySetInnerHTML`) all remain in force. **D4 is reversed** (see D-E3). **D7** is
  amended: the Vite proxy target is configurable via `VITE_API_PROXY_TARGET`, default
  `http://localhost:8010`; frontend URLs stay relative.

## 15. Open questions

**None.** Every contract-level question raised by the brief is decided above. Two items are worth
the Team Lead's *awareness* (non-blocking, no action needed before dispatch):
1. **D-T1** (SQLite-first tests) trades a little fidelity for always-runnable tests; the Postgres
   CI lane and the per-slice compose smoke test are the compensating controls.
2. **R4** (JWT in `localStorage`) is the standard choice for a Bearer JSON API; if the owner later
   wants httpOnly cookies, that is a slice-5 change requiring CSRF protection.

## 16. Effort estimate (rough, agent time)

| Slice | Backend | Frontend | DevOps | QA + gates | Total |
|---|---|---|---|---|---|
| 1 Persistence + Docker | 3–4 h | 45 min | 2–3 h | 1 h | ~7–9 h |
| 2 Auth + lists | 3–4 h | 3–4 h | 30 min | 1 h | ~8–10 h |
| 3 Enrichment | 4–5 h | 4–5 h | — | 1.5 h | ~10–12 h |
| 4 Realtime + AI | 3–4 h (A) + 3–4 h (B) | 4–5 h | 1.5–2 h | 1.5 h | ~13–16 h |

## 17. Deviations from plan (orchestrator-approved)

- **Postgres image:** `postgres:16-alpine` is used in `docker-compose.yml` and the CI
  `backend-postgres` job (§10, §9.2 originally specified `postgres:17-alpine`). Only
  `postgres:16`/`postgres:16-alpine` were available in the local image cache at
  implementation time; 17 was not pre-pulled. Functionally equivalent for this
  iteration's schema (no version-17-only features are used). Approved by the
  orchestrator; not a defect to re-flag in later reviews.
- **nginx image:** `frontend/Dockerfile` uses `nginx:1.27-alpine`, matching this plan
  (§10) as originally written. (An intermediate implementation pass briefly used the
  untagged `nginx:alpine` per an earlier dispatch note about locally-cached images;
  this was corrected back to the pinned `nginx:1.27-alpine` during code review, since
  that tag was in fact already available locally.)
- **AI internal auth (§8.1, §8.2, §9.1):** Internal shared secret between backend and
  ai-agent kept (≥32 chars, required when `AI_ENABLED=true`); ai-agent not published
  on the host by default.
