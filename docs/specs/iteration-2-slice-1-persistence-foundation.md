# Iteration 2 · Slice 1 — Persistence Foundation + Docker Base

Status: **ready for dispatch** · Depends on: nothing · Unblocks: slices 2, 3, 4
Read together with `docs/specs/iteration-2-master.md` §1–§5, §9, §10, §12 (binding).

## Goal in one sentence
Replace the in-memory store with PostgreSQL (async SQLAlchemy 2.x + Alembic), create the **full
target schema** in one reversible baseline migration, dockerize `db`/`backend`/`frontend`, and
clear all iteration-1 carry-over defects — **while the UI behaves exactly as it did in
iteration 1**.

## Scope subset of the contract
Only these endpoints change behaviour (paths and the four capabilities are unchanged):

| Endpoint | This slice |
|---|---|
| `GET /api/health` | unchanged, `{"status":"ok"}`, **no DB access** |
| `GET /api/health/ready` | **new** — `{"status":"ok","database":"ok"}` / 503 `database_unavailable` |
| `GET /api/todos` | 200, bare array of the **full `TodoResponse`** (master §3.3), ordered `created_at ASC, id ASC`; scoped to the bootstrap local user; no query params yet |
| `POST /api/todos` | body **exactly** `{"title": str}` with `extra="forbid"`; 201 full `TodoResponse` |
| `PATCH /api/todos/{id}` | body **exactly** `{"completed": bool}` with `extra="forbid"`; 200 |
| `DELETE /api/todos/{id}` | 204 |

Not in this slice: auth endpoints, lists endpoints, tags, subtasks, filters, SSE, AI.
Those tables exist (D-M1) but nothing writes to them yet.

---

## BACKEND — complexity **complex** (opus) · owns `backend/**` except `Dockerfile`, `.dockerignore`, `docker-entrypoint.sh`

### B0. Dependencies (`backend/pyproject.toml`)
Runtime add: `sqlalchemy[asyncio]>=2.0`, `asyncpg`, `alembic`, `pydantic-settings`.
Dev group add: `pytest-asyncio`, `aiosqlite`.
Keep `requires-python = ">=3.12"`. Regenerate and **commit `uv.lock`**.
Add to `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`.

### B1. Files to create
```
backend/alembic.ini
backend/migrations/env.py
backend/migrations/script.py.mako
backend/migrations/versions/0001_initial_schema.py
backend/app/config.py
backend/app/errors.py
backend/app/deps.py
backend/app/bootstrap.py
backend/app/db/__init__.py
backend/app/db/base.py
backend/app/db/types.py
backend/app/db/models.py
backend/app/db/session.py
backend/app/schemas/__init__.py
backend/app/schemas/common.py
backend/app/schemas/todos.py
backend/app/repositories/__init__.py
backend/app/repositories/protocols.py
backend/app/repositories/todos.py
backend/app/repositories/lists.py
backend/app/repositories/users.py
backend/tests/conftest.py                  (rewritten)
backend/tests/test_health.py
backend/tests/test_todos_api.py            (rewritten)
backend/tests/test_validation.py           (rewritten)
backend/tests/test_repository.py           (rewritten)
backend/tests/test_body_size_limit.py      (extended)
backend/tests/test_errors.py
backend/tests/test_docs_gating.py
backend/tests/test_db_types.py
backend/tests/test_migrations.py           (postgres-marked)
```
### Files to modify
`backend/app/main.py` · `backend/app/middleware.py` · **delete** `backend/app/models.py` and
`backend/app/repository.py` (their content moves to `app/schemas/todos.py`,
`app/db/models.py` and `app/repositories/todos.py`).

### B2. `app/db/base.py`
```python
class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)   # master §4.1
```

### B3. `app/db/types.py` — `UtcDateTime`
`TypeDecorator` over `DateTime(timezone=True)`, `cache_ok = True`:
- `process_bind_param`: `None` → `None`; naive datetime → **raise `ValueError("naive datetime")`**;
  otherwise `value.astimezone(timezone.utc)`.
- `process_result_value`: `None` → `None`; naive → `value.replace(tzinfo=timezone.utc)`;
  aware → `value.astimezone(timezone.utc)`.
This is the single mechanism that makes the SQLite lane trustworthy (master D-T1).

### B4. `app/db/models.py` — ORM entities
Implement `users`, `todo_lists`, `todos`, `tags`, `todo_tags` **exactly** as master §3.1,
including every index, FK `ondelete="CASCADE"`, and the portability rules:
- `id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)`
- `priority: Mapped[Priority] = mapped_column(Enum(Priority, native_enum=False, length=6,
  validate_strings=True), nullable=False, server_default="medium")` where
  `class Priority(str, enum.Enum): LOW="low"; MEDIUM="medium"; HIGH="high"`
- `created_at`/`updated_at`: `UtcDateTime`, `default=_utcnow`, `onupdate=_utcnow`
  (Python-side, not `server_default=func.now()`, so both engines agree)
- `Todo.subtasks`: `relationship("Todo", back_populates="parent", cascade="all, delete-orphan",
  order_by=(Todo.created_at, Todo.id))`, `remote_side` on `parent`
- `Todo.tags`: `relationship(Tag, secondary=todo_tags, order_by=Tag.name, lazy="selectin")`
- `Todo.subtasks` and `Todo.tags` must be loaded with explicit `selectinload` in queries
  (do not rely on `lazy="selectin"` alone for `subtasks` — write the loader options).

### B5. `app/db/session.py`
- `create_engine_from_url(url) -> AsyncEngine`: for a `sqlite+aiosqlite` URL use
  `poolclass=StaticPool, connect_args={"check_same_thread": False}` and register a
  `@event.listens_for(engine.sync_engine, "connect")` hook issuing `PRAGMA foreign_keys=ON`;
  for postgres use `pool_size=5, max_overflow=5, pool_pre_ping=True`.
- `create_sessionmaker(engine)` → `async_sessionmaker(expire_on_commit=False, autoflush=False)`.
- Engine created in `main.lifespan`, stored on `app.state.engine` / `app.state.sessionmaker`,
  `await engine.dispose()` on shutdown.

### B6. `app/config.py`
`Settings(BaseSettings)` with the variables in master §9.1 that exist this slice:
`DATABASE_URL`, `APP_ENV`, `CORS_ORIGINS` (comma-separated → `list[str]`), `MAX_BODY_BYTES`,
`AUTH_ENABLED` (default **`false`** this slice only), `JWT_SECRET` (**optional this slice**,
required from slice 2), `TEST_DATABASE_URL`.
`model_config = SettingsConfigDict(env_file=".env", extra="ignore")`.
`get_settings()` is an `@lru_cache` provider; tests override it via `app.dependency_overrides`
or by `get_settings.cache_clear()` + monkeypatched env.

### B7. `app/errors.py` — error contract v2 (master §5)
```python
class AppError(Exception):
    status_code: int; code: str; detail: str
class NotFoundError(AppError): ...        # subclasses set code/detail
class TodoNotFound(NotFoundError): code="todo_not_found"; detail="Todo not found"
class RequestTooLarge(AppError): status_code=413; code="request_too_large"; detail="Request body too large"
class DatabaseUnavailable(AppError): status_code=503; code="database_unavailable"; detail="Database unavailable"
```
`register_error_handlers(app)` installs:
- `AppError` → `JSONResponse(status, {"detail": e.detail, "code": e.code})`
- `HTTPException` → default body **plus** `"code"` when `exc.detail` came from an `AppError`
  helper (simplest: routers raise `AppError`, not `HTTPException`)
- `RequestValidationError` → **unchanged** FastAPI default (no `code`).
Create the full code taxonomy of master §5 now (classes for codes used in later slices may be
added later; only this slice's codes are required).

### B8. `app/schemas/todos.py`
```python
class TodoCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

class TodoUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed: bool

class TodoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID; list_id: UUID; parent_id: UUID | None
    title: str; description: str | None
    completed: bool; completed_at: datetime | None
    priority: Priority; due_date: date | None
    tags: list[str]                      # field_validator converts Tag entities → sorted names
    subtasks: list["TodoResponse"]
    created_at: datetime; updated_at: datetime
```
- `tags` conversion: use a `field_validator("tags", mode="before")` that maps `Tag` objects to
  `.name` and sorts ascending; an empty relationship yields `[]`.
- UUIDs serialize as plain strings (Pydantic v2 default) — assert in a test.
- Datetime format contract from iteration 1 is preserved; a test asserts
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$` for `created_at`.

### B9. `app/repositories/` — protocols + SQLAlchemy implementations
Protocols exactly as master §4.2 (all five `TodoRepository` methods, plus `ListRepository`,
`UserRepository`; `TagRepository` may be a stub protocol this slice with no implementation).
`TodoQuery` frozen dataclass with **every** field from master §6.3 and safe defaults
(`list_id=None, status="all", priorities=(), tags=(), due="any", due_from=None, due_to=None,
today=None, q=None, sort="created_at", order="asc", limit=200, offset=0`). This slice only
honours the defaults; slice 3 implements the rest — **do not** leave the fields out.

`SqlAlchemyTodoRepository(session)`:
- `list_todos(user_id, query)` → `(rows, total)`; this slice: `WHERE user_id = :u AND parent_id
  IS NULL`, `ORDER BY created_at ASC, id ASC`, `selectinload(Todo.subtasks)` +
  `selectinload(Todo.tags)` + `selectinload(Todo.subtasks).selectinload(Todo.tags)`, `limit`
  and `offset` applied, `total` from a `select(func.count())` over the same filter.
- `get / create / update / delete` — every one filters on `user_id`; a row belonging to another
  user is indistinguishable from a missing row (returns `None`/`False` → router raises
  `TodoNotFound` → 404, master D-E2).
- `create(user_id, data)` uses `data.list_id or (await list_repo.get_default(user_id)).id`;
  this slice `data.list_id` is always `None`.
- `update` implements invariant **I4** (`completed_at` set/cleared on transitions) and always
  bumps `updated_at`.
- `flush()` + `refresh` as needed; the **session is committed by `get_session`**, not by the
  repository.

`SqlAlchemyListRepository.get_default(user_id)` returns the user's `is_default` list, or the
oldest list, creating `Inbox` when the user has none.

### B10. `app/bootstrap.py` — local user (master D-S1)
```python
LOCAL_USER_ID = uuid5(NAMESPACE_URL, "https://todo.app/users/local")
LOCAL_USER_EMAIL = "local@todo.app"
async def ensure_local_user(session) -> User   # get-or-create + ensure an "Inbox" default list
```
Password hash: a constant unusable placeholder string `"!"` (no algorithm prefix → can never
verify). **Do not** invent a real password. Called from `lifespan` only when
`settings.AUTH_ENABLED is False`.

### B11. `app/deps.py`
- `get_session(request)` → async generator: `async with request.app.state.sessionmaker() as s:`
  `yield s`; on clean exit `await s.commit()`, on exception `await s.rollback()` and re-raise.
- `get_current_user(session=Depends(get_session), settings=Depends(get_settings)) -> User`:
  this slice, when `AUTH_ENABLED is False`, returns `await ensure_local_user(session)`; when
  `True`, raise `NotImplementedError` with a clear message (slice 2 fills it in).
- `get_todo_repository(session=Depends(get_session)) -> TodoRepository` etc.
- `get_client_id(x_client_id: Annotated[str | None, Header()] = None) -> str | None` — parsed and
  ignored this slice (used in slice 4); validate it is a UUID string or `None`, never echo it.

### B12. `app/routers/todos.py` (rewrite)
Same four routes, now `async def` with `user: User = Depends(get_current_user)` and
`repo: TodoRepository = Depends(get_todo_repository)`. Path ids stay `str`; a helper
`parse_uuid(value) -> UUID | None` returns `None` for a malformed id → `raise TodoNotFound()`
(iteration-1 D1 preserved: malformed id → 404, not 422).

### B13. `app/main.py`
- App factory `create_app(settings=None) -> FastAPI` plus a module-level `app = create_app()`
  (Playwright/uvicorn use `app.main:app`).
- `lifespan`: build engine + sessionmaker; if `AUTH_ENABLED is False`, open a session and
  `ensure_local_user`; on shutdown dispose the engine.
- **`/docs` gating (C5):** `docs_url = "/docs" if settings.APP_ENV == "dev" else None`, same for
  `redoc_url` and `openapi_url`.
- CORS from `settings.CORS_ORIGINS`, methods `GET, POST, PATCH, DELETE, OPTIONS`, headers
  `Content-Type, Authorization, X-Client-Id`, `expose_headers=["X-Total-Count"]`,
  `allow_credentials=False`.
- `BodySizeLimitMiddleware(max_bytes=settings.MAX_BODY_BYTES)`, `register_error_handlers(app)`,
  routers, `/api/health`, `/api/health/ready`.

### B14. `app/middleware.py` — fix C4
Keep the fast `Content-Length` rejection, **and** additionally wrap `receive`: accumulate
`len(message["body"])` across `http.request` messages and, as soon as the running total exceeds
`max_bytes`, send the 413 response and stop forwarding to the app. Must handle a request with no
`Content-Length` (chunked) and one with a *lying* `Content-Length`. Response side untouched
(SSE in slice 4 must stream freely).

### B15. Alembic
- `alembic.ini`: `script_location = migrations`, `prepend_sys_path = .`,
  `file_template = %%(rev)s_%%(slug)s`, **no** `sqlalchemy.url` (read from settings in `env.py`).
- `migrations/env.py`: import `Base.metadata` as `target_metadata`, read the URL from
  `app.config.get_settings().DATABASE_URL`, run online migrations with
  `async_engine_from_config` + `connection.run_sync(do_run_migrations)`; set
  `compare_type=True, compare_server_default=True, render_as_batch=False`.
- `versions/0001_initial_schema.py`, `revision="0001"`, `down_revision=None`: creates all five
  tables of master §3.1 with every index and constraint, and a **complete `downgrade()`** that
  drops them in reverse dependency order (`todo_tags`, `todos`, `tags`, `todo_lists`, `users`).
  Written by hand or autogenerated then reviewed — either way it must round-trip.

### B16. Tests (`backend/tests/`)
`conftest.py` (rewritten):
- `TEST_DATABASE_URL` env decides the engine: unset → `sqlite+aiosqlite:///:memory:` with
  `StaticPool`; set → that URL. Expose it as a `db_url` fixture and print the lane once per run.
- session-scoped `engine` fixture; function-scoped `db_session` that creates all tables
  (`Base.metadata.create_all` on SQLite; on Postgres `drop_all` + `create_all` inside the test
  database) and drops them after — **no state shared between tests**.
- `client` fixture: builds the app with `create_app(test_settings)`, overrides
  `get_session` to yield the test session, wraps it in `TestClient`, and seeds the local user.
- `pytest.mark.postgres` registered in `pyproject.toml` markers; a `pytest_collection_modifyitems`
  hook skips those tests when `TEST_DATABASE_URL` is unset.

Test groups (each must exist):
1. **health** — `/api/health` 200 `{"status":"ok"}` and issues **no** SQL (assert with an engine
   event counter or by pointing `DATABASE_URL` at an unreachable host); `/api/health/ready` 200,
   and 503 `database_unavailable` when the engine is disposed/unreachable.
2. **todos API** — all 12 iteration-1 cases (spec-1 §3.4 items 1–12) re-run against the DB.
3. **response shape** — `TodoResponse` contains every key of master §3.3 with the documented
   defaults; `created_at` matches the `…Z` regex; `id`/`list_id` are strings.
4. **extra="forbid" (C3)** — `POST {"title":"x","nope":1}` → **422**; `PATCH {"completed":true,
   "x":1}` → 422. *(This replaces the iteration-1 test asserting extra keys are ignored — delete
   that assertion.)*
5. **repository** — direct unit tests: ordering, `create/get/update/delete` return values,
   `delete` twice, `completed_at` transitions (I4), and **owner isolation**: create a second user
   via the repository and assert user A's methods never see user B's rows (get→None,
   list→absent, update→None, delete→False).
6. **db types** — `UtcDateTime` raises on a naive datetime, round-trips an aware one to UTC, and
   returns an aware value on both engines.
7. **cascade** — deleting a `todo_lists` row deletes its todos; deleting a parent todo deletes
   its subtasks (insert a subtask directly via the ORM). Requires `PRAGMA foreign_keys=ON`.
8. **body size (C4)** — `Content-Length` over the limit → 413 `{"detail":"Request body too
   large","code":"request_too_large"}`; a **chunked** request with no `Content-Length` whose body
   exceeds the limit → 413; a lying small `Content-Length` with a big body → 413; a normal
   request is unaffected.
9. **docs gating (C5)** — with `APP_ENV=prod`, `/docs`, `/redoc` and `/openapi.json` all return
   404; with `APP_ENV=dev` all three return 200.
10. **errors** — 404 body is exactly `{"detail":"Todo not found","code":"todo_not_found"}`;
    a 422 body has no `code` key.
11. **migrations** (`@pytest.mark.postgres`) — `alembic upgrade head`, then
    `alembic downgrade base`, then `upgrade head` again succeeds; and `alembic check` reports no
    diff against `Base.metadata`.

Run `uv run pytest` and report the summary verbatim. Commit stepwise: `backend: <step>`.

---

## DEVOPS — complexity **intermediate** (sonnet) · owns Docker, env, README, CI

### D1. Files to create
```
docker-compose.yml
docker/nginx.conf
docker/db/init/01-create-test-db.sql
backend/Dockerfile
backend/.dockerignore
backend/docker-entrypoint.sh
frontend/Dockerfile
frontend/.dockerignore
.env.example
```
Modify: `README.md`, `.github/workflows/ci.yml`, `.gitignore` (add `/postgres-data/`,
`/frontend/playwright-report/` if missing).

### D2. `docker-compose.yml` (this slice: `db`, `backend`, `frontend` only)
Follow master §10. Concrete requirements:
- `db`: `image: postgres:17-alpine`, env from `.env` (`POSTGRES_USER/PASSWORD/DB`),
  `ports: ["${DB_HOST_PORT:-5433}:5432"]`, `volumes: [pgdata:/var/lib/postgresql/data,
  ./docker/db/init:/docker-entrypoint-initdb.d:ro]`,
  `healthcheck: pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB` (interval 5s, retries 10).
- `backend`: `build: ./backend`, `ports: ["${BACKEND_HOST_PORT:-8010}:8000"]`,
  `depends_on: {db: {condition: service_healthy}}`, env
  `DATABASE_URL, APP_ENV, CORS_ORIGINS, MAX_BODY_BYTES, AUTH_ENABLED`,
  `healthcheck` hitting `/api/health`, `restart: unless-stopped`.
- `frontend`: `build: ./frontend`, `ports: ["${FRONTEND_HOST_PORT:-5173}:80"]`,
  `depends_on: [backend]`.
- `networks: {default: {name: todo-net}}`; `volumes: {pgdata: {}}`.
- Add the commented-out ollama debug port note now so slice 4 only uncomments.
- **No secrets inline** — everything through `.env` with `change-me-*` placeholders in
  `.env.example`.

### D3. `backend/Dockerfile`
`python:3.12-slim` base; copy `uv` from `ghcr.io/astral-sh/uv:latest`; `WORKDIR /app`;
`COPY pyproject.toml uv.lock ./` then `uv sync --locked --no-dev` (cached layer); copy `app/`,
`migrations/`, `alembic.ini`; create and switch to a non-root user; `EXPOSE 8000`;
`ENTRYPOINT ["/app/docker-entrypoint.sh"]`.

`backend/docker-entrypoint.sh` (executable, `set -euo pipefail`):
```sh
uv run alembic upgrade head
exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### D4. `frontend/Dockerfile`
Stage 1 `node:22-alpine`: `npm ci`, `npm run build`.
Stage 2 `nginx:1.27-alpine`: copy `dist/` to `/usr/share/nginx/html`, copy `docker/nginx.conf`
to `/etc/nginx/conf.d/default.conf`. (The nginx config file lives at the repo root under
`docker/`; the build context is the repo root **or** the file is copied into `frontend/` — pick
one and document it in the compose `build:` block, e.g. `build: {context: ., dockerfile:
frontend/Dockerfile}`.)

### D5. `docker/nginx.conf`
```nginx
server {
  listen 80;
  root /usr/share/nginx/html;
  location /api/ {
    proxy_pass http://backend:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;
    proxy_buffering off;              # required for SSE in slice 4
    proxy_read_timeout 3600s;
  }
  location / { try_files $uri $uri/ /index.html; }
}
```

### D6. `docker/db/init/01-create-test-db.sql`
```sql
CREATE DATABASE todo_test;
```
(runs once on an empty volume; documented in the README).

### D7. `.env.example`
Exactly the block in master §9.2, minus the AI section (slice 4 appends it) — but **do** include
`JWT_SECRET` already, commented as "used from slice 2".

### D8. README rewrite
Sections: what the app is · prerequisites (Python 3.12+, uv, Node 22+, Docker 29/Compose 2.40) ·
**Run with Docker** (`cp .env.example .env`, `docker compose up -d --build`, URLs) ·
**Run locally without Docker** (`docker compose up -d db`, `DATABASE_URL=…@localhost:5433/todo`,
`cd backend && uv run alembic upgrade head && uv run uvicorn app.main:app --port 8010 --reload`,
`cd frontend && npm run dev`) · **port table from master §1.1 with the "these host ports belong to
another project" warning** · testing (`uv run pytest`, the `TEST_DATABASE_URL` Postgres lane,
`npm test`, `npm run build`, the Playwright commands **including
`PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright`**) · migrations
(`uv run alembic upgrade head` / `downgrade -1`) · a note that data created under the
`AUTH_ENABLED=false` local user is dev-only and becomes orphaned when slice 2 lands ·
`docs/specs/` pointer. Remove the "todos are stored in memory" paragraph.

### D9. CI (`.github/workflows/ci.yml`)
Keep the existing `backend` (SQLite lane) and `frontend` jobs; add:
```yaml
  backend-postgres:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17
        env: { POSTGRES_USER: todo, POSTGRES_PASSWORD: todo, POSTGRES_DB: todo }
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U todo" --health-interval 5s
          --health-timeout 5s --health-retries 10
    # steps: setup-uv, uv sync --locked,
    #   DATABASE_URL=postgresql+asyncpg://todo:todo@localhost:5432/todo
    #   uv run alembic upgrade head && uv run alembic downgrade base
    #     && uv run alembic upgrade head && uv run alembic check
    #   create todo_test, then TEST_DATABASE_URL=…/todo_test uv run pytest
```
Playwright still not run in CI (keep the explanatory comment, update the spec reference).
Verify YAML validity locally; report that CI is unverified until pushed.

Commit stepwise: `devops: <step>`.

---

## FRONTEND — complexity **mechanical** (haiku) · owns `frontend/**` except `Dockerfile`/`.dockerignore`

No visual change. Six precise edits:

### F1. `src/api/types.ts` — full response type
```ts
export type Priority = 'low' | 'medium' | 'high';
export interface Todo {
  id: string;
  list_id: string;
  parent_id: string | null;
  title: string;
  description: string | null;
  completed: boolean;
  completed_at: string | null;
  priority: Priority;
  due_date: string | null;   // "YYYY-MM-DD"
  tags: string[];
  subtasks: Todo[];
  created_at: string;
  updated_at: string;
}
```

### F2. `src/api/errors.ts` (new)
```ts
export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string | null, readonly detail: string) {
    super(`${status} ${code ?? 'error'}: ${detail}`);
    this.name = 'ApiError';
  }
}
export async function toApiError(response: Response): Promise<ApiError> { /* parse {detail, code}; tolerate a non-JSON or array `detail` → detail = response.statusText, code = null */ }
```

### F3. `src/api/client.ts`
- Throw `ApiError` (via `toApiError`) instead of a plain `Error` on non-OK.
- Add a shared `request()` helper that sets `Content-Type` on bodies and adds
  `X-Client-Id: getClientId()` on POST/PATCH/DELETE.
- `src/api/clientId.ts` (new): `getClientId()` returns `sessionStorage['todo.client-id']`,
  creating it with `crypto.randomUUID()` on first use; guard against `sessionStorage` throwing
  (private mode) by falling back to a module-level variable.
- URLs stay relative. `grep -r "localhost:8010" src` must return nothing.

### F4. `vite.config.ts`
Proxy target default `http://localhost:8010` (still `process.env.VITE_API_PROXY_TARGET ?? …`).

### F5. `tsconfig.e2e.json` — fix C6
`"lib": ["ES2023", "DOM"]`.

### F6. `TodoItem.tsx` / `TodoList.tsx` / `App.tsx` — fix C7 (focus loss)
- Remove `disabled={busy}` from the checkbox and the Delete button.
- The `<li>` gets `aria-busy={busy ? 'true' : undefined}`; both controls get
  `aria-disabled={busy || undefined}` and their handlers early-return when `busy` is true.
- Keep a visual busy affordance that is **not** `disabled:` styling (e.g. `opacity-60` driven by
  `aria-busy` on the row) so contrast stays ≥ 4.5:1 on the title text.
- New Vitest test: after clicking a row checkbox, while the request is pending
  `document.activeElement` is still that checkbox, and it is still the checkbox after the promise
  resolves. A second test asserts a click while busy does not issue a second `setCompleted` call.

### F7. Playwright
`playwright.config.ts`: `E2E_BACKEND_PORT` default **8010**. Keep both `webServer` entries; the
backend command becomes `uv run uvicorn app.main:app --port ${backendPort}` with
`env: { DATABASE_URL: ..., AUTH_ENABLED: 'false' }`. **Decision:** E2E runs against a real
Postgres; the spec files stay unchanged, and `npm run e2e` requires `docker compose up -d db`
first — document this in the README (devops) and in a comment at the top of
`playwright.config.ts`. E2E specs (`todo.spec.ts`, `keyboard.spec.ts`) keep using unique
per-run titles and must not assert list length.

Run `npm test` and `npm run build`; run `npm run e2e` only if a database is reachable, otherwise
report it as deferred to QA. Commit stepwise: `frontend: <step>`.

---

## QA — complexity **intermediate** (sonnet)
On the integrated branch:
1. `cd backend && uv run pytest` (SQLite lane) — green.
2. `docker compose up -d db` then
   `TEST_DATABASE_URL=postgresql+asyncpg://todo:<pw>@localhost:5433/todo_test uv run pytest` —
   green, including the `postgres`-marked migration tests.
3. `uv run alembic upgrade head`, `uv run alembic downgrade base`, `uv run alembic upgrade head`
   against the compose db — all succeed (**reversibility gate**).
4. `cd frontend && npm test && npm run build` — green.
5. `PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e` — green.
6. `docker compose up -d --build` from a clean `.env`; then:
   `curl localhost:8010/api/health` → `{"status":"ok"}`; `curl localhost:8010/api/health/ready` →
   `database:"ok"`; open `http://localhost:5173`, add/toggle/delete a todo, **restart the backend
   container and confirm the todo is still there** (the headline acceptance of this slice).
7. `curl localhost:8010/docs` with `APP_ENV=prod` in `.env` → 404.
8. Keyboard pass: focus is retained on the checkbox across a toggle (C7).
9. `grep -rn "dangerouslySetInnerHTML\|localhost:8010" frontend/src` → nothing.

---

## Acceptance criteria
1. `docker compose up -d --build` brings up `db`, `backend`, `frontend`; the app is usable at
   `http://localhost:5173` and the API at `http://localhost:8010`.
2. Todos survive a backend restart and a `docker compose restart backend`.
3. `alembic upgrade head` → `downgrade base` → `upgrade head` succeeds on PostgreSQL with no
   errors and no leftover objects.
4. The baseline migration creates all five tables with every column, index, FK and check
   constraint of master §3.1.
5. `GET /api/todos` returns a bare array of full `TodoResponse` objects (master §3.3) ordered
   `created_at ASC`, scoped to the bootstrap local user.
6. `POST /api/todos {"title":"  x  "}` → 201 with `title:"x"`, `priority:"medium"`, `tags:[]`,
   `subtasks:[]`, `parent_id:null`, `completed:false`, `completed_at:null`, `due_date:null`,
   a `list_id`, and `created_at` matching the `…Z` regex.
7. `POST`/`PATCH` with an unknown extra key → **422** (C3).
8. `PATCH` toggling to completed sets `completed_at`; toggling back clears it.
9. Unknown **or malformed** todo id on `GET`/`PATCH`/`DELETE` → 404
   `{"detail":"Todo not found","code":"todo_not_found"}`.
10. A repository call with user A's id can never read, update or delete user B's row (unit test).
11. A request body over 64 KiB is rejected with 413 `request_too_large` — with a correct, an
    absent (chunked) and a falsified `Content-Length` (C4).
12. With `APP_ENV=prod`, `/docs`, `/redoc` and `/openapi.json` all return 404; with `dev`, 200 (C5).
13. `GET /api/health` returns 200 without touching the database; `GET /api/health/ready` reports
    the database and returns 503 `database_unavailable` when it is down.
14. `uv run pytest` is green on SQLite **and** with `TEST_DATABASE_URL` pointing at Postgres.
15. `npm test` and `npm run build` are green; `npm run e2e` passes the iteration-1 happy path and
    the keyboard spec against the containerized/local backend on port 8010.
16. Focus remains on the toggled checkbox during and after an in-flight toggle (C7), and a second
    click while busy issues no second request.
17. `tsconfig.e2e.json` includes the `DOM` lib and `npm run build` type-checks the `e2e/` project (C6).
18. `.env.example` exists with placeholder-only secrets; `git grep` finds no real secret and no
    `.env` file is committed.
19. `README.md` documents both run modes, the port table with the foreign-port warning, the
    migration commands, both test lanes, and `PLAYWRIGHT_BROWSERS_PATH`.
20. CI has three jobs (`backend` SQLite, `backend-postgres`, `frontend`) and the Postgres job runs
    upgrade/downgrade/upgrade + `alembic check` + pytest.
