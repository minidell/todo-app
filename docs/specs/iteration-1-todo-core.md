# Iteration 1 — Todo Core Specification

Status: **frozen** (API contract is binding for backend and frontend)
Date: 2026-09-02
Applies to: greenfield repo at project root (only `CLAUDE.md`, `.claude/`, `.gitignore` exist today)

---

## Overview

### What
A single-page personal Todo app with a FastAPI backend and a React + TypeScript frontend.
Exactly four capabilities:

1. **Add a todo** — title required, trimmed, non-empty, max 200 characters.
2. **Remove a todo.**
3. **Toggle a todo** completed ⇄ active (both directions).
4. **Display all todos.**

### Why
Iteration 1 establishes the walking skeleton: repo layout, API contract, test harnesses and
CI. Everything is deliberately minimal so that iteration 2 (PostgreSQL, auth, Docker, AI
features) can be layered on without reshaping the contract.

### Explicitly out of scope (iteration 2 — do NOT build)
Database / SQLAlchemy / Alembic, auth or users, Docker or docker-compose, editing a todo's
title, filtering / sorting / search / priorities / due dates / tags, subtasks, real-time
updates, AI features, pagination, deployment.

### Acceptance criteria
See the numbered, testable list in [§7](#7-acceptance-criteria). All of them must pass before
the reviewer/QA gates are considered satisfied.

### Design constraints carried from `CLAUDE.md`
- Persistence is **in-memory**, but behind a `TodoRepository` protocol so an iteration-2
  Postgres implementation is a drop-in replacement.
- Ids are **UUID4 strings**. Todos carry `created_at` (UTC) and `completed: bool` from day one.
- No auth anywhere: no tokens, no headers, no cookies, no CORS credentials.

---

## 1. Repo layout

Everything below lives inside the project root. Nothing is created outside it.

```
todo-app/
├── CLAUDE.md
├── README.md                      # devops
├── .gitignore                     # devops (extend existing)
├── .github/
│   └── workflows/ci.yml           # devops
├── docs/
│   └── specs/iteration-1-todo-core.md
├── backend/                       # backend agent owns everything under here
│   ├── pyproject.toml             # uv-managed
│   ├── uv.lock                    # committed
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                # FastAPI app, CORS, health, router include
│   │   ├── models.py              # Pydantic v2 schemas + domain Todo
│   │   ├── repository.py          # TodoRepository protocol + InMemoryTodoRepository + DI provider
│   │   └── routers/
│   │       ├── __init__.py
│   │       └── todos.py           # /api/todos endpoints
│   └── tests/
│       ├── __init__.py
│       ├── conftest.py            # client fixture w/ fresh repository per test
│       ├── test_todos_api.py
│       ├── test_validation.py
│       └── test_repository.py
└── frontend/                      # frontend agent owns everything under here
    ├── package.json
    ├── package-lock.json          # committed (CI uses npm ci)
    ├── tsconfig.json / tsconfig.node.json
    ├── vite.config.ts             # dev server port 5173, /api proxy, vitest config
    ├── playwright.config.ts       # webServer: backend + frontend
    ├── index.html
    ├── src/
    │   ├── main.tsx
    │   ├── index.css              # Tailwind entry
    │   ├── App.tsx                # page-level state
    │   ├── api/
    │   │   ├── types.ts           # Todo interface (mirrors the contract)
    │   │   └── client.ts          # listTodos / createTodo / setCompleted / deleteTodo
    │   ├── components/
    │   │   ├── AddTodoForm.tsx
    │   │   ├── TodoList.tsx
    │   │   └── TodoItem.tsx
    │   └── test/setup.ts          # @testing-library/jest-dom
    └── e2e/
        └── todo.spec.ts           # single happy-path spec
```

### File ownership (prevents worktree merge conflicts)
| Path | Owner |
|---|---|
| `backend/**` | `backend` agent |
| `frontend/**` | `frontend` agent |
| `README.md`, `.gitignore`, `.github/**` | `devops` agent |
| `docs/**` | planner (already written) |

No agent touches another agent's paths. If a change seems to require it, report to the
orchestrator.

### `.gitignore` additions (devops)
Append to the existing file (which already has `/.claude/worktrees/`):

```
# Python
__pycache__/
*.py[cod]
.venv/
.pytest_cache/
.ruff_cache/

# Node / frontend
node_modules/
dist/
.vite/

# Playwright
playwright-report/
test-results/
/frontend/e2e/.auth/

# Editors / OS
.DS_Store
.idea/
.vscode/
```

`uv.lock` and `package-lock.json` are **committed** (CI depends on them).

---

## 2. Frozen API contract

Base URL in development: `http://localhost:8000`. All app endpoints are under `/api`.
The frontend always calls **relative** URLs (`/api/todos`) and relies on the Vite proxy.

### 2.1 Resource shape

```jsonc
{
  "id": "3f1b2c4e-9d7a-4f0b-8a11-6c2e5f0d9b31", // string, UUID4
  "title": "Buy milk",                          // string, 1..200 chars, already trimmed
  "completed": false,                           // boolean
  "created_at": "2026-09-02T11:22:33.123456Z"   // string, ISO-8601, UTC, ends with "Z"
}
```

- `created_at` is produced from a timezone-aware UTC `datetime`
  (`datetime.now(timezone.utc)`) and serialized by Pydantic v2, which renders UTC as a
  trailing `Z`. Backend has a test asserting the serialized value matches
  `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$`.
- Field names are exactly as above (`snake_case`, `created_at` — **not** `createdAt`).
- No extra fields are emitted. Frontend must tolerate unknown future fields (never assert on
  object key count).

### 2.2 Endpoints

#### `GET /api/health`
- **200** → `{"status": "ok"}`
- No side effects. Used by the Playwright `webServer` readiness check and by CI smoke checks.

#### `GET /api/todos`
- Request: no body, no query parameters (any query params are ignored).
- **200** → JSON array of Todo objects, possibly empty `[]`.
- **Ordering: `created_at` ascending (oldest first).** Ties are broken by insertion order
  (stable sort over the repository's insertion-ordered storage).

#### `POST /api/todos`
- Request body:
  ```json
  { "title": "Buy milk" }
  ```
- Validation (see §2.4). Title is **trimmed before validation and stored trimmed**.
- **201** → the created Todo object (full shape, `completed: false`).
- **422** → validation error (FastAPI default shape) when the title is missing, not a
  string, empty/whitespace-only, or longer than 200 characters after trimming.
- No `Location` header is set (decision: not needed; keep the contract minimal).

#### `PATCH /api/todos/{id}`
- `{id}` is the todo id as an opaque **string** path parameter.
- Request body:
  ```json
  { "completed": true }
  ```
  `completed` is **required** (this is a full replacement of the completed flag, not a partial
  patch of arbitrary fields). Sending `{}` or a non-boolean → 422.
- **200** → the updated Todo object (same `id`, `title`, `created_at`; new `completed`).
- **404** → `{"detail": "Todo not found"}` when no todo with that id exists.
- Setting `completed` to its current value is allowed and returns 200 (idempotent).

#### `DELETE /api/todos/{id}`
- **204** → empty body (no content).
- **404** → `{"detail": "Todo not found"}` when no todo with that id exists.
- Deleting twice: first call 204, second call 404.

### 2.3 Error bodies

| Case | Status | Body |
|---|---|---|
| Unknown id on PATCH/DELETE | 404 | `{"detail": "Todo not found"}` (exact string) |
| Invalid request body | 422 | FastAPI/Pydantic default: `{"detail": [{"type": ..., "loc": [...], "msg": ..., "input": ...}]}` |
| Method not allowed / unknown route | 405 / 404 | FastAPI defaults, not specified further |

**Decision (contract-level, recorded here so nobody guesses):** the `{id}` path parameter is
typed as `str`, **not** `UUID`. Therefore a syntactically invalid id (e.g. `/api/todos/abc`)
returns **404 `{"detail": "Todo not found"}`**, never 422. This keeps "id is opaque to the
client" true and simplifies the frontend error handling to a single not-found path.

**Decision:** the frontend must **not** parse the 422 body structure. It renders a generic
per-action message (§4.4). The 422 shape is documented only so backend tests can assert it.

### 2.4 Validation rules (authoritative)

`TodoCreate.title`:
- Type `str`. Missing or non-string → 422.
- **Whitespace-stripped first** (leading/trailing), then length-checked:
  `min_length=1`, `max_length=200` — implement with
  `Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]`
  so stripping provably happens before the length check.
- `"   "` (whitespace only) → 422. `"  hi  "` → stored and returned as `"hi"`.
- A 200-character title is accepted; 201 characters → 422. Length is counted in Unicode
  code points (Python `len`), after trimming.
- Unknown extra keys in the body are **ignored** (Pydantic v2 default `extra="ignore"`).
  Decision: no `extra="forbid"` — keeps the contract forward-compatible for iteration 2.
- No HTML/markup sanitisation on the server. React escapes text by default; the frontend must
  never use `dangerouslySetInnerHTML`. (Noted for the security reviewer.)

`TodoUpdate.completed`: type `bool`, required. JSON `true`/`false` only; Pydantic's lax mode
may also accept `"true"`/`1` — that is acceptable and untested behaviour, the frontend always
sends real booleans.

### 2.5 CORS

`CORSMiddleware` on the FastAPI app:
- `allow_origins=["http://localhost:5173"]` (exact origin, no wildcard)
- `allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"]`
- `allow_headers=["Content-Type"]`
- `allow_credentials=False`

CORS is a safety net for direct `http://localhost:8000` calls; in normal dev the Vite proxy
makes requests same-origin so CORS is not exercised.

### 2.6 Vite dev proxy

`vite.config.ts`:
```ts
server: {
  port: 5173,
  strictPort: true,
  proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: false } },
}
```
Consequence: `src/api/client.ts` uses relative paths only (`fetch('/api/todos', ...)`). No
`VITE_API_URL` env var in iteration 1 (decision: not needed; the proxy is the single seam).

---

## 3. Backend technical design

```
HTTP ─▶ FastAPI app (app/main.py)
          ├─ CORSMiddleware
          ├─ GET /api/health
          └─ router: app/routers/todos.py
                 │  Depends(get_todo_repository)
                 ▼
          TodoRepository (Protocol, app/repository.py)
                 ▲
                 └── InMemoryTodoRepository  ← iteration 2 swaps in SqlAlchemyTodoRepository
                        dict[str, Todo], insertion-ordered
```

### 3.1 Models (`app/models.py`)
- `Todo` — the domain/response model (Pydantic v2 `BaseModel`): `id: str`, `title: str`,
  `completed: bool`, `created_at: datetime`. Used both as the internal record and as the
  `response_model`. Decision: one model for both layers this iteration — there is no
  persistence entity to leak yet; iteration 2 introduces the split when the ORM arrives.
- `TodoCreate` — `title` with the constrained annotation from §2.4.
- `TodoUpdate` — `completed: bool`.
- `HealthResponse` — `status: str` (optional but preferred, so `/api/health` is typed).

### 3.2 Repository (`app/repository.py`)
```python
class TodoRepository(Protocol):
    def list(self) -> list[Todo]: ...
    def add(self, title: str) -> Todo: ...
    def get(self, todo_id: str) -> Todo | None: ...
    def set_completed(self, todo_id: str, completed: bool) -> Todo | None: ...
    def delete(self, todo_id: str) -> bool: ...
```
- `InMemoryTodoRepository` implements it over `dict[str, Todo]`.
- `add` generates `str(uuid4())` and `datetime.now(timezone.utc)`.
- `list` returns a **new list** sorted by `created_at` ascending (stable), never the internal
  container.
- `set_completed` returns the updated todo or `None` if absent; `delete` returns `True` /
  `False`. The router converts `None`/`False` into
  `HTTPException(404, detail="Todo not found")`.
- Module-level singleton `_repository = InMemoryTodoRepository()` plus a provider
  `def get_todo_repository() -> TodoRepository: return _repository`. Routers use
  `Depends(get_todo_repository)`; tests override it via `app.dependency_overrides`.
- **Concurrency:** all route handlers are `async def`, so they execute on the single event
  loop thread and the dict is never mutated concurrently. Do not use `def` handlers here (they
  would run in a threadpool). No lock needed; this is noted so the reviewer does not flag it.
- State is process-local and lost on restart / `--reload`. Documented in the README as
  expected iteration-1 behaviour.

### 3.3 Project config (`backend/pyproject.toml`)
- `requires-python = ">=3.12"`.
- Runtime deps: `fastapi`, `uvicorn[standard]`.
- Dev deps via uv dependency group: `[dependency-groups] dev = ["pytest", "httpx"]`
  (`httpx` is required by Starlette's `TestClient`).
- No `[build-system]` section — the backend is a uv *virtual* project, not a distributable
  package.
- `[tool.pytest.ini_options] pythonpath = ["."]` and `testpaths = ["tests"]` so `import app`
  works when running `uv run pytest` from `backend/`.
- `uv.lock` committed.

### 3.4 Required backend tests (`backend/tests/`)
`conftest.py` provides a fixture that builds a fresh `InMemoryTodoRepository`, installs it via
`app.dependency_overrides[get_todo_repository]`, yields a `TestClient(app)`, and clears the
override afterwards — **tests must not share state**.

Cases (each an assertion-bearing test):
1. `GET /api/health` → 200 `{"status": "ok"}`.
2. `GET /api/todos` on an empty store → 200 `[]`.
3. `POST` valid title → 201; response has UUID-parseable `id`, `title` echoed, `completed`
   is `false`, `created_at` matches the ISO-8601-Z regex.
4. `POST` with `"  padded  "` → stored/returned title is `"padded"`.
5. `POST` with `""`, `"   "`, missing `title`, `title: 123`, and a 201-char title → 422 each.
6. `POST` with a 200-char title → 201.
7. `GET /api/todos` after three POSTs → 200, length 3, ordered by `created_at` ascending and
   matching insertion order.
8. `PATCH` existing id `{"completed": true}` → 200, `completed` true, `id`/`title`/`created_at`
   unchanged; then `{"completed": false}` → 200, back to active.
9. `PATCH` unknown id (`str(uuid4())`) and a malformed id (`"not-a-uuid"`) → 404
   `{"detail": "Todo not found"}` for both.
10. `PATCH` with `{}` or `{"completed": "maybe"}` → 422.
11. `DELETE` existing id → 204 with empty body; subsequent `GET` no longer lists it.
12. `DELETE` same id again → 404 `{"detail": "Todo not found"}`; `DELETE` malformed id → 404.
13. `test_repository.py`: direct unit tests of `InMemoryTodoRepository`
    (add/get/list-ordering/set_completed/delete return values, `delete` returns `False` for an
    unknown id, `list()` returns a copy — mutating the returned list does not affect the store).

---

## 4. Frontend technical design

### 4.1 Stack decisions (frozen — do not substitute)
- Vite + React 18 + TypeScript (`npm create vite@latest . -- --template react-ts` equivalent).
- **Tailwind CSS v4** via the `@tailwindcss/vite` plugin, with `@import "tailwindcss";` as the
  first line of `src/index.css`. No `tailwind.config.js` is required. If installation of v4
  fails for any reason, fall back to Tailwind v3 + PostCSS and **report the deviation to the
  orchestrator** — do not silently choose a different styling system.
- State: plain React `useState` / `useEffect` in `App.tsx`. **No** Redux/Zustand/React Query.
- Data fetching: native `fetch`. No axios.
- Tests: Vitest (`environment: 'jsdom'`, `globals: true`) + React Testing Library +
  `@testing-library/user-event` + `@testing-library/jest-dom`. Vitest config lives inside
  `vite.config.ts`; it must **exclude `e2e/**`** so Playwright specs are not picked up.
- E2E: `@playwright/test`.

### 4.2 Component structure and data flow
```
App.tsx
 ├─ state: todos: Todo[], loading: boolean, error: string | null, busyIds: Set<string>
 ├─ useEffect on mount → listTodos()
 ├─ <AddTodoForm onAdd={(title) => createTodo(title)} disabled={loading} />
 ├─ error && <p role="alert">{error}</p>
 ├─ loading ? <p>Loading todos...</p>
 │  : todos.length === 0 ? <p>{EMPTY_STATE}</p>
 │  : <TodoList todos onToggle onDelete />
 │        └─ <TodoItem todo onToggle onDelete />
 └─ <p>{activeCount} item(s) left</p>
```

- **No optimistic updates** (decision). Each mutation awaits the API and then updates state
  from the response: `createTodo` appends the returned Todo; `setCompleted` replaces the item
  by `id`; `deleteTodo` removes by `id` after a 204. On failure the list is left untouched and
  an error message is shown. This keeps component tests deterministic.
- The rendered order is exactly the array order returned by `GET /api/todos` (server order);
  new todos are appended at the end, matching `created_at` ascending.

### 4.3 API client (`src/api/client.ts`)
Typed functions, all relative URLs, all throwing a plain `Error` on non-OK responses (the
caller maps them to user-facing copy):
```ts
export async function listTodos(): Promise<Todo[]>            // GET  /api/todos
export async function createTodo(title: string): Promise<Todo> // POST /api/todos
export async function setCompleted(id: string, completed: boolean): Promise<Todo> // PATCH
export async function deleteTodo(id: string): Promise<void>    // DELETE, expects 204
```
`src/api/types.ts`:
```ts
export interface Todo {
  id: string;
  title: string;
  completed: boolean;
  created_at: string; // ISO-8601 UTC, ends with "Z"
}
```

### 4.4 UX and accessibility requirements (exact copy — tests assert on it)

Header: `<h1>Todos</h1>`.

**Add form** (`<form>` so Enter submits natively):
- `<label>` "New todo title" (visually hidden, e.g. `sr-only`) bound to the input via
  `htmlFor`/`id`; input `placeholder="What needs to be done?"`, `maxLength={200}`,
  `autoComplete="off"`.
- Submit `<button type="submit">Add</button>`, `disabled` when the trimmed input is empty.
- Submitting (button or Enter) with an empty/whitespace-only value is a no-op: no request, no
  error message.
- On success the input is cleared and receives focus back.
- While the create request is in flight the input and button are disabled.

**Todo item** (`<ul>` / `<li>`):
- `<input type="checkbox">` with an accessible name via an associated label:
  - when the todo is active: `Mark {title} as completed`
  - when the todo is completed: `Mark {title} as active`
  - `checked={todo.completed}`.
- Title text rendered as text (never `dangerouslySetInnerHTML`); when completed it is visually
  struck through — Tailwind `line-through` plus a muted colour. Colour alone must never be the
  only completion signal (WCAG 1.4.1).
- `<button type="button">` whose accessible name is `Delete {title}` (visible text may be
  "Delete"; use `aria-label={`Delete ${todo.title}`}`).
- Controls for a row are disabled while that row has a request in flight (`busyIds`).

**Status / feedback copy** (exact strings):
| Situation | Text |
|---|---|
| Loading (initial fetch) | `Loading todos...` |
| Empty list | `No todos yet. Add your first one above.` |
| Active counter, n = 1 | `1 item left` |
| Active counter, n ≠ 1 | `{n} items left` (including `0 items left`) |
| Load failure | `Could not load todos. Please try again.` |
| Create failure | `Could not add todo. Please try again.` |
| Toggle failure | `Could not update todo. Please try again.` |
| Delete failure | `Could not delete todo. Please try again.` |

- The error element has `role="alert"` and is cleared on the next successful action.
- The active counter is always rendered (even for an empty list).

**Accessibility (WCAG AA) checklist:**
- Every interactive control has an accessible name; no icon-only unlabelled buttons.
- Full keyboard operation: Tab reaches input → Add → each checkbox → each Delete; Space toggles
  the checkbox; Enter in the input submits.
- Visible focus ring on all controls (do not remove the default outline without a replacement).
- Text contrast ≥ 4.5:1 — for struck-through completed text use at least Tailwind
  `text-gray-500` on white (`#6b7280` on `#ffffff` ≈ 5.1:1); do **not** use `text-gray-400`.
- Semantic structure: one `<h1>`, list markup for the list, `<form>` for the input.
- `<html lang="en">` in `index.html`; page `<title>Todos</title>`.

### 4.5 Required Vitest component tests (`src/**/*.test.tsx`)
Mock the API module (`vi.mock('./api/client')`) — component tests never hit the network.
1. Renders `Loading todos...` while the initial fetch is pending, then the list.
2. Empty response → empty-state text and `0 items left`.
3. Renders N todos with their titles; completed ones have the strike-through class and a
   checked checkbox.
4. Add: type a title, click **Add** → `createTodo` called once with the trimmed title; the new
   item appears; input is cleared.
5. Add via Enter key → same result.
6. Add button is disabled for empty and whitespace-only input, and submitting does not call
   `createTodo`.
7. Toggle: click the checkbox of an active todo → `setCompleted(id, true)`; the row renders as
   completed. Click again on a completed todo → `setCompleted(id, false)`.
8. Delete: click the button named `Delete {title}` → `deleteTodo(id)`; the row disappears.
9. Counter shows `1 item left` for one active todo and `2 items left` for two.
10. API failure on load / create / toggle / delete → the matching message from §4.4 is shown in
    a `role="alert"` element and the list state is unchanged.

Queries must be accessibility-first (`getByRole('checkbox', { name: ... })`,
`getByRole('button', { name: 'Delete Buy milk' })`), not test-ids.

### 4.6 Playwright E2E (`frontend/e2e/todo.spec.ts`)
- **One happy-path spec** against the **real backend**: add → toggle completed → toggle back →
  delete, asserting the visible list after each step.
- `playwright.config.ts`:
  ```ts
  testDir: './e2e',
  use: { baseURL: 'http://localhost:5173' },
  webServer: [
    { command: 'uv run uvicorn app.main:app --port 8000', cwd: '../backend',
      url: 'http://localhost:8000/api/health', reuseExistingServer: !process.env.CI },
    { command: 'npm run dev', url: 'http://localhost:5173',
      reuseExistingServer: !process.env.CI },
  ],
  ```
  (`cwd: '../backend'` stays inside the project root.)
- Because the backend store is process-global and may already contain data, the spec creates a
  **unique title** (e.g. `` `E2E todo ${Date.now()}` ``) and scopes all assertions to that
  row. It must not assert on total list length.
- Browsers: chromium only in iteration 1.
- Playwright is **not** run in CI this iteration (see §6).

---

## 5. Ports, commands, and the README

| Service | Port | Start command | Working dir |
|---|---|---|---|
| Backend (FastAPI) | **8000** | `uv run uvicorn app.main:app --reload` | `backend/` |
| Frontend (Vite) | **5173** | `npm run dev` | `frontend/` |

Setup and test commands:

```bash
# backend
cd backend && uv sync            # create .venv + install deps
uv run uvicorn app.main:app --reload
uv run pytest                    # backend tests

# frontend
cd frontend && npm install
npm run dev                      # http://localhost:5173
npm test                         # vitest run (component tests)
npm run build                    # tsc + vite build
npm run e2e                      # playwright test (starts both servers itself)
```

`frontend/package.json` scripts must be exactly:
`dev`, `build` (`tsc -b && vite build`), `preview`, `test` (`vitest run`),
`test:watch` (`vitest`), `e2e` (`playwright test`), `lint` (optional).

The root `README.md` (devops) contains: one-paragraph project description, prerequisites
(Python 3.12+, uv, Node 20+), the setup/run/test commands above, the port table, the API
contract summary table, and an explicit note that **iteration 1 stores todos in memory — data
is lost when the backend restarts**.

---

## 6. Subtask breakdown

Contract dependency: §2 is **frozen**. Backend and frontend build against it **in parallel**;
the frontend does not wait for the backend (component tests mock the API client). The only
real dependency is Playwright E2E, which needs a running backend — the frontend agent may run
the backend from its own worktree (`../backend` inside its worktree contains the same tree only
after the backend branch is merged), so:

> **Sequencing note for the orchestrator:** if the frontend worktree does not contain the
> backend implementation, the frontend agent should write `e2e/todo.spec.ts` +
> `playwright.config.ts` and report the E2E as "written, executed after integration". QA runs
> the E2E on the integrated branch. Component tests and build must be green in the frontend
> worktree regardless.

### BACKEND — complexity: **intermediate** (sonnet)
Owns `backend/**`.
1. `uv init`-style scaffold: `pyproject.toml` (deps, dev group, pytest config), `uv.lock`.
2. `app/models.py` — `Todo`, `TodoCreate` (constrained title), `TodoUpdate`, `HealthResponse`.
3. `app/repository.py` — `TodoRepository` Protocol, `InMemoryTodoRepository`, singleton +
   `get_todo_repository` provider.
4. `app/routers/todos.py` — the four endpoints exactly per §2.2, `async def`, `Depends`,
   `response_model`, `status_code`, 404 with `detail="Todo not found"`.
5. `app/main.py` — app factory/instance, CORS per §2.5, `/api/health`, `include_router`.
6. `tests/` — conftest fixture with per-test repository override + all cases in §3.4.
7. Run `uv run pytest`; report the pass/fail summary verbatim.
Commit stepwise: `backend: <step>`.

### FRONTEND — complexity: **intermediate** (sonnet)
Owns `frontend/**`. Depends on the frozen contract in §2 only.
1. Vite React-TS scaffold + Tailwind v4 wiring + `vite.config.ts` (port 5173, `/api` proxy,
   vitest block excluding `e2e/**`).
2. `src/api/types.ts` + `src/api/client.ts` per §4.3.
3. `App.tsx`, `AddTodoForm.tsx`, `TodoList.tsx`, `TodoItem.tsx` per §4.2/§4.4 — exact copy
   strings, accessible names, WCAG AA notes.
4. Vitest setup (`src/test/setup.ts`) + all component tests in §4.5.
5. `playwright.config.ts` + `e2e/todo.spec.ts` per §4.6.
6. Run `npm test` and `npm run build`; run `npm run e2e` if the backend is available in the
   worktree, otherwise report it as deferred to QA. Report results verbatim.
Commit stepwise: `frontend: <step>`.

### DEVOPS — complexity: **mechanical** (haiku)
Owns `README.md`, `.gitignore`, `.github/workflows/ci.yml`. **No DB, no migrations, no Docker
this iteration.**
1. Extend `.gitignore` with the block in §1.
2. Write `README.md` per §5.
3. `.github/workflows/ci.yml`:
   - Triggers: `push` to the default branch and `pull_request`.
   - Job `backend`: `ubuntu-latest`, `actions/checkout@v4`, `astral-sh/setup-uv@v6` with
     `enable-cache: true`, Python 3.12, `working-directory: backend`, `uv sync --locked`,
     `uv run pytest`.
   - Job `frontend`: `ubuntu-latest`, `actions/checkout@v4`, `actions/setup-node@v4` with
     `node-version: 20` and `cache: npm` + `cache-dependency-path: frontend/package-lock.json`,
     `working-directory: frontend`, `npm ci`, `npm test`, `npm run build`.
   - **Playwright is intentionally not run in CI** in iteration 1 — add a comment saying so.
4. The workflow cannot be executed locally; devops verifies YAML validity only and reports
   that CI is unverified until the first push.
Commit stepwise: `devops: <step>`.

### QA — complexity: **intermediate** (sonnet, per model policy QA runs sonnet)
On the integrated branch:
1. Run `uv run pytest`, `npm test`, `npm run build`, `npm run e2e` — all must be green.
2. Verify each numbered acceptance criterion in §7, including the manual/API-level edge cases:
   201-char title, whitespace-only title, double delete, PATCH on a malformed id, toggle both
   directions, ordering after several adds.
3. Keyboard-only pass: add a todo with Enter, toggle with Space, reach and activate Delete via
   keyboard.
4. Confirm the frontend never calls an absolute backend URL (grep for `localhost:8000` in
   `frontend/src` — should be absent).

---

## 7. Acceptance criteria

Backend
1. `GET /api/health` returns 200 `{"status":"ok"}`.
2. `POST /api/todos` with `{"title":"Buy milk"}` returns **201** and a body matching §2.1 with
   `completed:false`, a UUID4 `id`, and a `created_at` ending in `Z`.
3. `POST` with `{"title":"  Buy milk  "}` stores and returns `"Buy milk"`.
4. `POST` with a missing, non-string, empty, whitespace-only, or >200-character (post-trim)
   title returns **422**; a title of exactly 200 characters returns 201.
5. `GET /api/todos` returns 200 with all todos ordered by `created_at` ascending; `[]` when
   empty.
6. `PATCH /api/todos/{id}` with `{"completed":true}` returns 200 with `completed:true`;
   with `{"completed":false}` returns 200 with `completed:false`; `id`, `title` and
   `created_at` are unchanged across toggles.
7. `PATCH` or `DELETE` with an unknown **or malformed** id returns **404** with body exactly
   `{"detail":"Todo not found"}`.
8. `DELETE /api/todos/{id}` on an existing todo returns **204** with an empty body and the
   todo no longer appears in `GET /api/todos`; a second `DELETE` returns 404.
9. `PATCH` with a missing or non-boolean `completed` returns 422.
10. `uv run pytest` passes with every case listed in §3.4 present.

Frontend
11. With both servers running, `http://localhost:5173` renders the heading `Todos`, the input,
    the Add button, and the active-item counter.
12. Typing a title and pressing Enter (or clicking **Add**) creates the todo via
    `POST /api/todos`, appends it to the visible list, clears the input, and returns focus to
    the input.
13. The Add button is disabled and submission is a no-op when the input is empty or
    whitespace-only; no request is issued.
14. Each row exposes a checkbox whose accessible name is `Mark {title} as completed` when
    active and `Mark {title} as active` when completed; clicking it issues the correct
    `PATCH` and updates the row in both directions.
15. Completed rows render the title with a strike-through and a contrast-compliant muted
    colour; completion is conveyed by more than colour alone.
16. Each row has a control whose accessible name is `Delete {title}`; activating it issues
    `DELETE` and removes the row from the list.
17. Before the first fetch resolves the page shows `Loading todos...`; an empty result shows
    `No todos yet. Add your first one above.`.
18. The counter reads `1 item left` for exactly one active todo and `{n} items left`
    otherwise, including `0 items left`.
19. Any API failure renders the matching message from §4.4 inside a `role="alert"` element and
    leaves the list unchanged.
20. The whole flow is operable by keyboard only, all controls have visible focus, and no
    interactive control lacks an accessible name.
21. `npm test` (Vitest) passes with every case in §4.5 present; `npm run build` succeeds with
    no TypeScript errors.
22. `npm run e2e` starts both servers and the single happy-path spec (add → toggle → toggle
    back → delete) passes against the real backend.

Repo / CI
23. `backend/` and `frontend/` match the layout in §1; `uv.lock` and `package-lock.json` are
    committed; `.gitignore` contains all entries from §1.
24. Root `README.md` documents setup, run, test commands, the port table, and the in-memory
    caveat.
25. `.github/workflows/ci.yml` exists with the two jobs described in §6 (DEVOPS-3).
26. `grep -r "localhost:8000" frontend/src` returns nothing — the frontend uses only relative
    `/api` URLs.

---

## 8. Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Tailwind v4 setup differs from v3 muscle memory (`@tailwindcss/vite` + `@import "tailwindcss";`, no config file). Agent may scaffold v3 artefacts. | Broken styles / mixed setup | §4.1 pins the exact wiring; fallback to v3 is allowed **only** with an explicit report to the orchestrator. |
| R2 | `created_at` serialization format drifts (offset `+00:00` vs `Z`, missing microseconds). | Frontend/QA assertions break | Backend test asserts the regex in §2.1; frontend never parses the value beyond ordering. |
| R3 | In-memory store is process-global, so E2E runs accumulate rows and can collide with earlier data. | Flaky E2E | E2E uses a unique per-run title and scopes assertions to that row; never asserts list length. |
| R4 | Test pollution across pytest cases via the module-level singleton repository. | Order-dependent failures | Mandatory `dependency_overrides` fixture with a fresh repository per test (§3.4). |
| R5 | CI `npm ci` / `uv sync --locked` fails if lockfiles are missing or stale when devops' branch merges before the app branches. | Red CI on first push | Lockfiles are required deliverables (§1); orchestrator merges backend + frontend before/with devops, and CI is expected to be unverified until the first push (§6 DEVOPS-4). |
| R6 | Three agents editing root files (`README.md`, `.gitignore`) in parallel worktrees. | Merge conflicts | Strict ownership table in §1 — only devops touches root files. |
| R7 | Vitest picking up Playwright specs (both match `*.spec.ts`). | Confusing failures | `vite.config.ts` test config excludes `e2e/**` (§4.1). |
| R8 | Sync (`def`) route handlers would run in a threadpool and race on the dict. | Rare lost updates | §3.2 mandates `async def` handlers. |
| R9 | Port 8000 or 5173 already occupied on the dev machine. | Servers fail to start | `strictPort: true` surfaces the clash immediately; README documents the ports; agent reports the collision rather than silently switching ports (the contract depends on 5173/8000). |
| R10 | Frontend E2E cannot run in its worktree because the backend branch is not merged yet. | "Green" claim without E2E evidence | Explicit sequencing note in §6; QA re-runs E2E on the integrated branch. |

---

## 9. Decisions recorded (previously ambiguous — now closed, do not re-litigate)

- **D1** Path id typed `str`, not `UUID` → malformed ids yield 404, not 422.
- **D2** `PATCH` body requires `completed`; there is no partial-update semantics.
- **D3** No `Location` header on 201.
- **D4** Extra body fields are ignored (`extra="ignore"`), not rejected.
- **D5** One Pydantic model (`Todo`) serves as both record and response model this iteration.
- **D6** No optimistic UI updates; state is derived from API responses.
- **D7** No `VITE_API_URL`; the Vite proxy is the only backend seam, frontend URLs are relative.
- **D8** Tailwind CSS v4 with `@tailwindcss/vite`.
- **D9** Ordering is `created_at` ascending with insertion-order tiebreak, applied server-side;
  the frontend renders server order verbatim.
- **D10** Playwright runs locally only in iteration 1; CI runs pytest + vitest + build.
- **D11** Root files (`README.md`, `.gitignore`, `.github/**`) are owned solely by devops.
- **D12** No server-side HTML sanitisation; React's default escaping is the control, and
  `dangerouslySetInnerHTML` is forbidden.

## 10. Open questions

**None blocking.** Every contract-level question raised by the request has been decided and
recorded in §9. The only item worth the Team Lead's awareness (non-blocking, does not affect
the API contract): **D8 / R1**, the Tailwind v4 choice — if the team prefers v3 for
familiarity, say so before dispatch; otherwise implementation proceeds on v4.

---

## 11. Effort estimate (rough)

| Subtask | Complexity | Estimate |
|---|---|---|
| BACKEND (app + 13 test groups) | intermediate | ~1.5–2 h agent time |
| FRONTEND (scaffold + Tailwind + 4 components + 10 test groups + E2E) | intermediate | ~2.5–3 h agent time |
| DEVOPS (gitignore, README, CI) | mechanical | ~20–30 min |
| QA (integrated verification incl. E2E + keyboard pass) | intermediate | ~45 min |
| Reviewer + Security gates | — | ~30 min |
