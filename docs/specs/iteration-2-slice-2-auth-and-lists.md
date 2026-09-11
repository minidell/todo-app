# Iteration 2 · Slice 2 — Auth, Users, Multiple Lists

Status: **ready for dispatch after slice 1 merges** · Depends on: slice 1 · Unblocks: slices 3, 4
Read together with `docs/specs/iteration-2-master.md` §3, §5, §6.1, §6.2, §9 (binding).

## Goal in one sentence
Turn the single-tenant slice-1 app into a multi-user app: register / login / me with JWT bearer
auth, hard per-user data isolation, and multiple named todo lists with a switcher in the UI.

## Contract subset implemented here
- `POST /api/auth/register`, `POST /api/auth/login`, `GET /api/auth/me` (master §6.1)
- `GET|POST /api/lists`, `PATCH|DELETE /api/lists/{id}` (master §6.2)
- `GET /api/todos?list_id=…` and `POST /api/todos {"title", "list_id"?}`
- **Every** endpoint except `/api/health`, `/api/health/ready`, `/api/auth/register`,
  `/api/auth/login` now requires `Authorization: Bearer <jwt>` → 401 `unauthorized` otherwise.
- `PATCH /api/todos/{id}` still requires exactly `{"completed": bool}` (partial PATCH is slice 3).

---

## BACKEND — complexity **complex** (opus) · owns `backend/**`

### B0. Dependencies
Add runtime: `argon2-cffi`, `pyjwt`, `email-validator` (or `pydantic[email]`). Recommit `uv.lock`.

### B1. Files
Create: `app/schemas/auth.py`, `app/schemas/lists.py`, `app/routers/auth.py`,
`app/routers/lists.py`, `app/ratelimit.py`, `tests/test_auth_api.py`,
`tests/test_lists_api.py`, `tests/test_isolation.py`, `tests/test_security.py`,
`tests/test_ratelimit.py`.
Modify: `app/security.py` (new file if slice 1 did not create it), `app/config.py`,
`app/deps.py`, `app/main.py`, `app/repositories/users.py`, `app/repositories/lists.py`,
`app/repositories/todos.py`, `app/bootstrap.py` (**delete** it), `tests/conftest.py`.

### B2. `app/security.py`
```python
_hasher = argon2.PasswordHasher()          # argon2id defaults: t=3, m=64MiB, p=4
DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")

def hash_password(raw: str) -> str
def verify_password(raw: str, hashed: str) -> bool      # never raises; False on any error
def create_access_token(user_id: UUID, settings) -> tuple[str, int]   # (jwt, expires_in_seconds)
def decode_access_token(token: str, settings) -> UUID   # raises InvalidToken on any failure
```
- JWT: HS256, claims `sub` (user id as str), `iat`, `exp`, `jti` (uuid4). Decoding **must**
  verify signature and `exp` (`options={"require": ["exp", "sub"]}`) and reject any `alg` other
  than HS256 (`algorithms=["HS256"]` — never `jwt.decode(..., options={"verify_signature": False})`).
- `JWT_SECRET` becomes **required**: `Settings` validates it is present and ≥ 32 characters, and
  `create_app` fails fast with a clear message otherwise. No default anywhere in code.
- `argon2` parameters are module constants so they can be lowered in tests
  (`TEST_ARGON2_FAST=1` → `PasswordHasher(time_cost=1, memory_cost=8*1024, parallelism=1)`) —
  otherwise the auth test suite becomes minutes long. The fast profile is **only** selected when
  `APP_ENV == "dev"` **and** the env var is set, and a test asserts production settings never
  pick it up.

### B3. `app/deps.py`
- Replace the slice-1 `get_current_user` body:
  ```python
  async def get_current_user(
      credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
      users: UserRepository = Depends(get_user_repository),
      settings: Settings = Depends(get_settings),
  ) -> User
  ```
  Missing header, wrong scheme, malformed/invalid/expired token, or a `sub` that no longer
  resolves to a user → `UnauthorizedError` (401, `unauthorized`, `Not authenticated`).
- **Delete `AUTH_ENABLED` from `config.py`, compose, `.env.example` and CI** and delete
  `app/bootstrap.py` + its lifespan call. There must be no code path that serves a request
  without a verified token. A test asserts `AUTH_ENABLED` no longer appears anywhere
  (`grep -r AUTH_ENABLED backend/ docker-compose.yml .github/` → nothing).
- Add `get_list_repository`.

### B4. `app/ratelimit.py`
In-process sliding-window counter: `RateLimiter(limit: int, window_seconds: int)` with
`hit(key) -> RateLimitResult(allowed: bool, retry_after: int)`, backed by
`dict[str, deque[float]]` with lazy pruning and a cap on tracked keys (evict oldest at 10 000
keys, so it cannot grow unbounded). Instances on `app.state`:
- auth: **10 attempts / 15 min**, key = `f"{client_ip}|{email.lower()}"` (applies to both
  register and login)
- (slice 4 adds the AI limiter: 20 / 5 min, key = user id)
`client_ip` comes from `request.client.host`; behind nginx add `X-Forwarded-For` support only if
`APP_ENV != "dev"` **and** the first hop is trusted — for iteration 2, document that the limiter
is best-effort and use `request.client.host`. Exceeding → 429 `rate_limited` with a `Retry-After`
header (seconds).

### B5. `app/schemas/auth.py`
```python
class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr                                   # normalized lowercase+strip in a validator
    password: Annotated[str, StringConstraints(min_length=8, max_length=128)]
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None

class LoginRequest(BaseModel):  # extra="forbid"; email: EmailStr, password: str (1..128, no min here)
class UserResponse(BaseModel):  # from_attributes; id, email, display_name, created_at  — NO password_hash
class TokenResponse(BaseModel): # access_token, token_type="bearer", expires_in, user: UserResponse
```
Password validator: reject a password whose `.strip()` is empty. Email must be ≤ 320 chars after
normalization.

### B6. `app/routers/auth.py`
- `POST /api/auth/register` → rate limit → normalize email → `users.get_by_email` → 409
  `email_taken` if present → hash → create user → create the default list **`Inbox`**
  (`is_default=True`) → **201** `UserResponse`. The uniqueness race is also caught by
  `IntegrityError` on the unique index → translated to 409 (test with a monkeypatched
  pre-check).
- `POST /api/auth/login` → rate limit → look up → **always** run a verify (against `DUMMY_HASH`
  when the user is absent) → 401 `invalid_credentials` on failure → 200 `TokenResponse`.
  The response body and status must be identical for "unknown email" and "wrong password".
- `GET /api/auth/me` → 200 `UserResponse`.
- Never log an email/password pair; log `user_id` only.

### B7. `app/routers/lists.py` + `SqlAlchemyListRepository`
Implement master §6.2 exactly.
- `create`: name trimmed 1..100; case-insensitive duplicate check per user (`.ilike()`), plus
  `IntegrityError` → 409 `list_name_taken`. First list for a user gets `is_default=True`.
- `rename`: same conflict rules; renaming to the same name (different case) is allowed.
- `delete`: 409 `cannot_delete_last_list` when it is the user's only list; deleting the default
  promotes the oldest remaining list to `is_default=True`; the DB cascade removes its todos and
  their subtasks/tag links.
- `ListResponse.todo_count` / `active_count` count **top-level todos only** (`parent_id IS NULL`),
  computed with a single grouped subquery — not N+1.
- All methods take `user_id`; another user's list id → 404 `list_not_found`.

### B8. Todos: list scoping
- `GET /api/todos` accepts `list_id: str | None`; malformed → 404 `list_not_found`; valid but not
  the caller's → 404 `list_not_found`; omitted → all of the caller's lists.
- `POST /api/todos` accepts optional `list_id` (add it to `TodoCreate`, `extra="forbid"` keeps
  everything else rejected); omitted → `list_repo.get_default(user_id)`.
- `TodoQuery.list_id` is now honoured by `SqlAlchemyTodoRepository.list_todos`.

### B9. Tests
1. **register** — happy path 201 + body shape (no `password_hash` key anywhere in the response);
   duplicate email (also differing only by case / surrounding spaces) → 409 `email_taken`;
   password 7 chars → 422; password 129 chars → 422; whitespace-only password → 422; invalid
   email → 422; unknown extra key → 422; a default list named `Inbox` exists afterwards.
2. **login** — happy path returns a token, `expires_in == JWT_EXPIRES_MINUTES*60`, and the
   embedded user; wrong password and unknown email produce byte-identical 401 bodies; the token
   works on `/api/auth/me`.
3. **token** — no header → 401; `Authorization: Basic …` → 401; garbage token → 401; token signed
   with a different secret → 401; token with `alg: none` → 401; expired token → 401
   (`freezegun`-free: mint a token with `exp` in the past via `create_access_token` monkeypatch or
   a direct `jwt.encode`); token whose `sub` user was deleted → 401.
4. **isolation matrix** (`test_isolation.py`) — users A and B each with a list and a todo. For
   **every** endpoint (`GET/POST /api/todos`, `GET/PATCH/DELETE /api/todos/{id}`,
   `GET/PATCH/DELETE /api/lists/{id}`) assert A cannot see or affect B's resource and the status
   is **404**, never 403, and that `GET /api/todos` for A never contains B's rows.
5. **lists** — CRUD happy paths; duplicate name (case-insensitive) → 409; empty/101-char name →
   422; delete last list → 409; delete default promotes the oldest remaining; delete cascades the
   list's todos; `todo_count`/`active_count` correctness with subtasks present (subtasks excluded).
6. **rate limit** — 10 failed logins for the same email+IP then the 11th → 429 with a
   `Retry-After` header; a different email is unaffected; the window expires (inject a clock).
7. **config** — missing `JWT_SECRET` → app creation raises with a message naming the variable;
   a 20-char secret → raises; `AUTH_ENABLED` is gone from the codebase.
8. **security regressions** — `UserResponse.model_fields` has no `password_hash`;
   `verify_password` returns `False` (never raises) for a corrupt hash.

`tests/conftest.py` gains `registered_user`, `auth_headers` and `other_user_auth_headers`
fixtures; every existing todo test switches to sending `auth_headers`.

Commit stepwise: `backend: <step>`.

---

## FRONTEND — complexity **complex** (opus) · owns `frontend/**`

Depends only on the frozen contract (master §6.1/§6.2) — starts in parallel with backend.

### F1. Files
Create: `src/auth/AuthContext.tsx`, `src/auth/storage.ts`, `src/api/auth.ts`,
`src/api/lists.ts`, `src/components/LoginScreen.tsx`, `src/components/RegisterScreen.tsx`,
`src/components/ListSwitcher.tsx`, `src/components/ListForm.tsx`,
`src/components/AppHeader.tsx`, tests alongside, `e2e/auth.spec.ts`, `e2e/lists.spec.ts`,
`e2e/fixtures.ts`.
Modify: `src/App.tsx`, `src/main.tsx`, `src/api/client.ts`, `src/api/types.ts`,
`src/api/errors.ts`, existing tests.

### F2. Token storage (master D-F2)
`src/auth/storage.ts`: `loadToken() / saveToken(t) / clearToken()` on
`localStorage['todo.auth.token']`, and `loadUser() / saveUser(u) / clearUser()` on
`localStorage['todo.auth.user']` (JSON). All accessors wrapped in `try/catch` (private mode)
with an in-memory fallback. Never store the password.

### F3. `AuthContext`
State: `{ user: User | null, token: string | null, status: 'loading' | 'anonymous' |
'authenticated' }` and actions `login(email, password)`, `register(email, password,
displayName?)`, `logout()`, `sessionExpired()`.
- On mount: if a token exists, call `GET /api/auth/me`; success → `authenticated`; `ApiError`
  401 → clear storage → `anonymous`; network error → `anonymous` with a retry banner.
- `src/api/client.ts` gains an `Authorization` header from a module-level token setter that
  `AuthContext` keeps in sync (avoids reading `localStorage` on every request), and a **global
  401 handler**: any `ApiError` with status 401 and code `unauthorized` calls the registered
  `onUnauthorized` callback → `sessionExpired()` → clears storage, returns to the login screen
  and shows the session-expiry copy. A 401 with code `invalid_credentials` is **not** treated as
  a session expiry (it is a failed login).
- No router (master D-F1): `App` renders `LoginScreen` / `RegisterScreen` / the todo app based on
  `status` and a `view` state.

### F4. Screens — exact copy and accessible names (tests assert on these)

**LoginScreen**
| Element | Copy / accessible name |
|---|---|
| Heading (`h1`) | `Sign in` |
| Email field label | `Email` (`type="email"`, `autoComplete="email"`, required) |
| Password field label | `Password` (`type="password"`, `autoComplete="current-password"`) |
| Submit button | `Sign in` |
| Link/button to register | `Create an account` |
| Invalid credentials (`role="alert"`) | `Invalid email or password.` |
| Rate limited | `Too many attempts. Please wait a minute and try again.` |
| Network/other failure | `Could not sign in. Please try again.` |
| Session expiry banner (shown once after an expiry) | `Your session expired. Please sign in again.` |
| Submitting | button label becomes `Signing in…` and the form is `aria-busy="true"` |

**RegisterScreen**
| Element | Copy |
|---|---|
| Heading | `Create your account` |
| Fields | `Email`, `Password` (`autoComplete="new-password"`), `Display name (optional)` |
| Password hint (`aria-describedby` on the field) | `At least 8 characters.` |
| Submit | `Create account` |
| Link back | `I already have an account` |
| Email taken (409) | `That email is already registered.` |
| Validation (422) | `Please check the form and try again.` |
| Other failure | `Could not create your account. Please try again.` |
| Success | auto-login via `POST /api/auth/login`, then the todo app; if the auto-login fails, show the login screen with `Account created. Please sign in.` |

**AppHeader** — `<h1>Todos</h1>` stays; add the signed-in user's email (or display name) as text
and a button `Sign out` (clears storage and returns to `Sign in`).

**ListSwitcher**
| Element | Copy / accessible name |
|---|---|
| `<select>` label | `Todo list` |
| Option text | the list name plus ` (n)` where n = `active_count`, e.g. `Inbox (3)` |
| First option | `All lists` (value `""` → `list_id` omitted) |
| New-list button | `New list` → reveals `ListForm` |
| ListForm input label | `New list name` (placeholder `List name`, `maxLength={100}`) |
| ListForm submit | `Create list` · cancel: `Cancel` |
| Rename button | accessible name `Rename {name}` (reuses `ListForm` with submit `Save list name`) |
| Delete button | accessible name `Delete list {name}` |
| Delete confirmation (`window.confirm` is forbidden — use an inline confirm) | `Delete “{name}” and its todos?` with buttons `Delete list` and `Keep list` |
| Name taken (409) | `A list with that name already exists.` |
| Last list (409 `cannot_delete_last_list`) | `You must keep at least one list.` |
| Other list failure | `Could not update your lists. Please try again.` |

- The selected list id is kept in component state and mirrored to
  `localStorage['todo.selected-list']`; on load, if the stored id is not in the fetched lists,
  fall back to the default list.
- Creating a todo while `All lists` is selected posts **without** `list_id` (server default list).
- After creating a list, it becomes the selected list and the input clears and refocuses.

### F5. Accessibility (WCAG AA — non-negotiable)
Every field has a real `<label htmlFor>`; errors use `role="alert"` and are referenced by
`aria-describedby` on the offending field; the submit button is never the only error signal;
focus moves to the error alert on a failed submit; the inline delete confirmation traps nothing
but places focus on its `Delete list` button and returns focus to the trigger on cancel; contrast
≥ 4.5:1 (reuse the iteration-1 palette; error text `text-red-700`).

### F6. Vitest tests (mock `src/api/*`)
1. Unauthenticated (no token) renders `Sign in`; the todo list is not requested.
2. Stored token + `me()` success → the todo app renders; `me()` 401 → login screen and storage
   cleared.
3. Login happy path: token saved, `GET /api/lists` and `GET /api/todos` called, heading `Todos`.
4. Login 401 → `Invalid email or password.` in a `role="alert"`, token not saved, focus on the alert.
5. Login 429 → the rate-limit copy.
6. Register happy path → auto-login → app; register 409 → `That email is already registered.`
7. Sign out clears storage and returns to `Sign in`.
8. A 401 from `listTodos` mid-session → login screen + `Your session expired. Please sign in again.`
9. List switcher renders `All lists` + every list with its active count; changing it refetches
   with the right `list_id`.
10. Create list → appears, becomes selected, input cleared and refocused; 409 → the name-taken copy.
11. Rename and delete lists, including the `cannot_delete_last_list` copy.
12. `Authorization: Bearer` is present on every authenticated request and **absent** on
    login/register (assert on the mocked `fetch`).

### F7. Playwright
`e2e/fixtures.ts`: a `registeredUser` fixture that registers a unique email
(`e2e+${Date.now()}@example.com`) via the API and returns credentials; a `signedInPage` fixture
that signs in through the UI. The domain must be `example.com`, **not** the reserved `.test` TLD:
`email-validator` (behind pydantic's `EmailStr`) rejects `.test` addresses, so registration would
422. Specs:
- `auth.spec.ts` — register → auto-signed-in → sign out → sign in → the todo list is visible.
- `lists.spec.ts` — create a list, switch to it, add a todo there, switch back and confirm the
  todo is not in the other list, delete the list.
- Update `todo.spec.ts` and `keyboard.spec.ts` to use `signedInPage`.
Run with `PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright`.

Commit stepwise: `frontend: <step>`.

---

## DEVOPS — complexity **mechanical** (haiku)
1. `.env.example`: uncomment/keep `JWT_SECRET` and `JWT_EXPIRES_MINUTES`, drop `AUTH_ENABLED`,
   add the generation hint `# openssl rand -hex 32`.
2. `docker-compose.yml`: pass `JWT_SECRET` and `JWT_EXPIRES_MINUTES` to `backend`; remove
   `AUTH_ENABLED`. Backend must fail fast (visible in `docker compose logs backend`) when
   `JWT_SECRET` is missing — verify by unsetting it once.
3. CI: export a throwaway `JWT_SECRET` (a literal 64-hex test value, clearly labelled
   `# test-only, not a secret`) in both backend jobs, and `TEST_ARGON2_FAST=1` + `APP_ENV=dev`.
4. README: a **Accounts and sign-in** section (register, sign in, one account = one set of lists,
   token lifetime 60 min, token stored in `localStorage` with the recorded tradeoff), and a note
   that slice-1 local-user dev data is now orphaned (`docker compose down -v` to start clean).

Commit stepwise: `devops: <step>`.

---

## QA — complexity **intermediate** (sonnet)
1. Backend suite on both lanes; frontend unit + build; all E2E specs with the browsers path.
2. **Isolation audit**: with two accounts and `curl`, attempt every cross-user access in the
   §B9.4 matrix and confirm 404 (never 403, never 200).
3. Confirm no response anywhere contains `password_hash` (`grep` the E2E network log or curl
   every endpoint).
4. Token handling: expired/garbage/`alg:none` tokens rejected; signing out then using the browser
   back button does not restore the session.
5. `docker compose up -d --build` smoke: register, sign in, create a second list, add todos to
   both, restart the stack, confirm everything persists and is still per-user.
6. Keyboard-only pass over both auth screens and the list switcher; focus moves to error alerts.
7. `grep -r AUTH_ENABLED .` (excluding `docs/`) → nothing.

---

## Acceptance criteria
1. `POST /api/auth/register` creates a user and an `Inbox` default list, returns 201
   `UserResponse` with no `password_hash`, and 409 `email_taken` on a duplicate (case-insensitive).
2. Passwords are stored as argon2id hashes; no plaintext or reversible encoding exists in the DB.
3. `POST /api/auth/login` returns a HS256 JWT with `sub`/`iat`/`exp`, `expires_in` matching
   `JWT_EXPIRES_MINUTES`, and returns a byte-identical 401 `invalid_credentials` for unknown
   email and wrong password.
4. `GET /api/auth/me` returns the caller; every protected endpoint returns 401 `unauthorized`
   without a valid bearer token; tokens with a wrong signature, `alg:none`, or a past `exp` are
   rejected.
5. `JWT_SECRET` is required and read from the environment; the app refuses to start without it or
   with fewer than 32 characters; no secret literal exists in the source.
6. 11 failed logins for one email+IP within 15 minutes → 429 `rate_limited` with `Retry-After`.
7. `GET|POST /api/lists` and `PATCH|DELETE /api/lists/{id}` behave exactly per master §6.2,
   including 409 `list_name_taken` and 409 `cannot_delete_last_list`, and deleting the default
   list promotes the oldest remaining list.
8. `GET /api/todos?list_id=…` returns only that list's todos; omitting `list_id` returns all the
   caller's todos; another user's `list_id` → 404 `list_not_found`.
9. `POST /api/todos` without `list_id` lands in the caller's default list.
10. No endpoint or repository method can reach another user's data; every attempt returns 404.
11. `AUTH_ENABLED` and the local-user bootstrap no longer exist anywhere in the code, compose file
    or CI.
12. The UI shows `Sign in` when signed out, supports register → auto-login → sign out → sign in,
    and shows `Your session expired. Please sign in again.` after a mid-session 401.
13. The list switcher lists `All lists` plus each list with its active count, supports create /
    rename / delete with the exact copy in §F4, and persists the selection across reloads.
14. All four E2E specs pass; `npm test` and `npm run build` are green; `uv run pytest` is green on
    both lanes.
15. Both auth screens and the list controls are fully keyboard-operable with visible focus and
    labelled fields; failed submits move focus to a `role="alert"` message.
