# Iteration 4 — Backlog Sweep Specification

Status: **ready to dispatch** · Date: 2026-09-03
Scope source: `DASHBOARD.md` → "Backlog (candidates for iteration 4)".
Binding context: `docs/specs/iteration-2-master.md` (contract v2, error contract, realtime, AI) —
this iteration **amends** that document; every amendment is written into it and marked
"iteration 4". Iteration-1..3 gates stay in force: tests for everything, both backend lanes
(SQLite + `TEST_DATABASE_URL` Postgres), Vitest, Playwright, WCAG AA.

---

## 0. Overview

### What

Nine backlog rows, none of them a feature, all of them a debt the gates raised in iterations 2–3:

| ID | Row | Slice | Complexity |
|---|---|---|---|
| IT3-1 | `tag.*` events in the SSE contract | A (backend) + B (client) | complex / intermediate |
| IT3-2 | `/api/ai/status` detects a backend↔ai-agent secret mismatch | A | intermediate |
| IT3-3 | Placeholder secret guard beyond the `change-me` prefix | A | mechanical |
| IT3-4 | In-process broker: document + reserve a `REALTIME_BACKEND` setting | A | mechanical |
| IT3-6 | GPU discovery flakiness: document + pin the Ollama image | A (devops) | mechanical |
| IT3-7 | Keyboard focus after deleting a todo | B | intermediate |
| IT3-8 | AI prompts: relative dates beyond "tomorrow" | A (ai-agent) | intermediate |
| IT4-1 | Composer tab switch discards the AI draft / typed title | B | intermediate |
| IT4-2 | Live AI menu path (split / suggest) never exercised end-to-end | QA gate | intermediate |

**IT3-5 is excluded — it needs user action.** Adding an `origin` remote and running GitHub
Actions for the first time requires credentials (a GitHub account, a repository, a token) that
only the user has, and pushing to a remote is outward-facing. It stays `TODO` in the backlog,
retagged **"needs user"**. Nothing in this iteration depends on it; CI correctness is still
covered locally by running both backend lanes, Vitest and Playwright before every merge.

### Why

Every row is a known-wrong behaviour that a gate agent already wrote down. Three of them
(IT3-2, IT3-3, IT3-4) are configuration-safety items where the current code fails *quietly*: a
token mismatch looks identical to "the model is still loading", a copied `.env` with an
obviously-fake secret starts the app, and a second backend replica silently halves realtime
delivery. Two (IT3-1, IT3-7) are correctness gaps a user meets directly. The rest close
documentation and coverage holes.

### Acceptance criteria

Numbered, each independently verifiable. Slice letters mark who owns the proof.

**IT3-1 — `tag.*` realtime (A, B)**
1. `PATCH /api/tags/{id}` publishes exactly one `tag.updated` frame carrying the full
   `TagResponse` (`id`, `name`, `todo_count`), to that user's subscribers only.
2. `DELETE /api/tags/{id}` publishes exactly one `tag.deleted` frame carrying `{"id": …}`.
3. A todo write that names a tag the user did not have (`POST /api/todos`,
   `PATCH /api/todos/{id}`, `POST /api/todos/{id}/subtasks`) publishes one `tag.created` frame per
   newly created tag, **staged before** the request's `todo.*` frame, with `todo_count` = 1 for a
   top-level todo and 0 for a subtask.
4. A todo write that only *reuses* existing tags publishes **no** `tag.created` frame.
5. All three frames carry `origin` from `X-Client-Id` and are published **after** the commit: a
   request whose transaction fails (e.g. a 409 on the tag-name unique index) publishes nothing.
6. No `tag.*` frame ever reaches another user's stream.
7. The frontend, on receiving `tag.updated` / `tag.deleted` from another tab, refreshes both the
   tag vocabulary and the todo list without a reload.
8. If the active filter names a tag that a `tag.updated` frame renamed, the filter follows the
   rename; if a `tag.deleted` frame removed it, the filter drops it. The user never lands in a
   permanent "no todos match" state caused by a tag that no longer exists.
9. Master §7.2 documents all three frames (**done** — written into the master spec).

**IT3-2 — AI status detects a secret mismatch (A, B)**
10. `ai-agent` serves `GET /ai/ping` → 200 `{"status":"ok"}` with a valid `X-Internal-Token`, and
    401 `{"detail":"Invalid internal token","code":"unauthorized"}` without one or with a wrong
    one. It makes no Ollama call.
11. `GET /api/ai/status` returns `available:false, reason:"auth_failed"` when ai-agent answers
    `/health` normally but rejects our token — the case that today reports `available:true` and
    hands the user AI buttons that are certain to fail.
12. `reason` takes exactly one of `null | "disabled" | "unreachable" | "auth_failed" |
    "model_unavailable"`, per master §5.1, and is `null` whenever `available` is `true`.
13. `/api/ai/status` still **always** answers 200, still costs at most one upstream round trip per
    `HEALTH_CACHE_SECONDS` window, and its worst-case latency stays ≈ `HEALTH_TIMEOUT_SECONDS`
    (the two probes run concurrently, not back to back).
14. A token mismatch is logged once per probe at WARNING, naming `AI_AGENT_TOKEN` and never the
    value.
15. The frontend renders reason-specific copy for `auth_failed` and `model_unavailable` and falls
    back to the existing generic banner for `unreachable`, `null` and any unknown value.

**IT3-3 — placeholder secret guard (A)**
16. `JWT_SECRET` and `AI_AGENT_TOKEN` are rejected at startup when they match the shared
    placeholder table of §3.3 — exact literals, prefixes, or substrings — casefolded and
    whitespace-stripped, in **both** the backend and ai-agent.
17. The two services' placeholder tables are byte-identical and each carries a comment pointing at
    the other; a test in each service asserts the *same* corpus of placeholder strings is rejected.
18. Values shaped like real secrets (`openssl rand -hex 32` output, and every secret literal the
    existing backend and ai-agent test suites use) are still accepted: both suites pass unchanged
    apart from the new tests.
19. The error message says what is wrong and how to generate a real value; it never echoes the
    rejected value.

**IT3-4 — broker multi-replica (A)**
20. `REALTIME_BACKEND` defaults to `memory`; any other value (including `redis`) fails application
    startup with a message naming the supported value.
21. The backend logs one INFO line at startup stating fan-out is in-process and that exactly one
    replica may run.
22. `docker-compose.yml`, `.env.example` and the README "Realtime" section state the single-replica
    constraint, what breaks with more than one replica, and the two future options
    (Postgres `LISTEN/NOTIFY`, Redis pub/sub) with the two seams that would change.
23. **No Redis dependency, image, service or client is added.**

**IT3-6 — GPU discovery + pinned Ollama (A/devops)**
24. `ollama` and `ollama-pull` use one pinned image reference through a single
    `${OLLAMA_IMAGE:-…}` variable, so the two can never drift; `:latest` no longer appears.
25. The pin resolves on this host **without a network pull** (verified with
    `docker image inspect`), and `docker compose --profile ai up -d` still comes up.
26. `docker compose config` and
    `docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai config` both
    succeed.
27. The README documents the GPU-discovery flake: the symptom, the exact command that shows it, the
    remedy, and the fact that a CPU fallback is degraded-but-correct (every timeout in the ladder
    already assumes CPU speed).

**IT3-7 — focus after deleting a todo (B)**
28. Deleting a todo with the keyboard moves focus to the **next** row's Delete button; if the
    deleted row was last, to the **previous** row's Delete button; if it was the only row, to the
    composer's title input — or, when the composer is on the "Draft with AI" tab, to the composer
    section itself.
29. Focus is moved **only** when the delete succeeded and focus was actually lost by the unmount
    (`document.activeElement` is `body`). A row disappearing because of an event-driven refetch, a
    poll, or a filter change never moves focus — this is the exact IT3-9 blocker and it gets a
    regression test.
30. A failed delete leaves focus where it was and shows the existing error copy.
31. Verified end-to-end in Playwright with keys only (no mouse) as well as in Vitest.

**IT4-1 — composer keeps its state across tab switches (B)**
32. Switching Add → Draft with AI → Add preserves the typed title, the "More options" disclosure
    state and any priority/due-date/tags the user set.
33. Switching Draft with AI → Add → Draft with AI preserves the typed note **and an unconfirmed AI
    draft**, including edits already made to that draft.
34. Returning to the AI tab does not steal focus (the draft preview must not re-run its mount
    autofocus).
35. The inactive panel is hidden from the accessibility tree and from the tab order; the tablist
    keyboard contract (Arrow/Home/End, roving `tabIndex`, `aria-selected`, `aria-controls`) still
    holds, and each tab's `aria-controls` points at its own panel id.

**IT4-2 — live AI menu coverage (QA)**
36. A new `E2E_AI=1` spec drives the row AI menu against the real model: "Split into subtasks" and
    "Suggest priority and tags", both accepted and both verified to persist across a reload.
37. The spec proves D-AI1: nothing is written before the user accepts.
38. At least one menu interaction is performed with the keyboard, and focus returns to the menu
    trigger after the suggestion panel closes.
39. The spec never asserts on the model's wording.

**Iteration gates (all slices)**
40. Backend: `uv run pytest` green on SQLite **and** with `TEST_DATABASE_URL` pointing at
    `todo_test`; ai-agent: `uv run pytest` green.
41. Frontend: `npm run test` (Vitest) and `npm run typecheck`/`build` green; Playwright green
    (13 existing specs + the new ones), run with
    `PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright`.
42. No new WCAG AA violation: every control touched keeps a visible focus ring, an accessible name
    containing its visible label (2.5.3), and never conveys state by colour alone.

---

## 1. Slices, ownership and parallelism

Two slices, disjoint file ownership, both branch from `integrate/iteration-4`. They can run
simultaneously in separate worktrees and merge in either order.

| Path | Slice | Owner |
|---|---|---|
| `backend/**` | A | backend agent |
| `ai-agent/**` | A | backend agent (may be a second dispatch, see below) |
| `docker-compose.yml`, `docker-compose.gpu.yml`, `.env.example`, `README.md` | A | devops (may be a third dispatch) |
| `frontend/**` (src, tests, e2e) | B | frontend agent |
| `frontend/e2e/ai-menu.spec.ts` (new file only) | QA gate | qa agent, after both merges |
| `docs/**`, `DASHBOARD.md` | — | planner / orchestrator |

**Optional finer split of slice A.** The three path groups above are mutually disjoint, so the
orchestrator may dispatch slice A as up to three parallel worktrees (`A-backend`, `A-agent`,
`A-devops`) instead of one. If it does, note that `A-devops` has a **read** dependency on
`A-backend` for one line only — the `REALTIME_BACKEND` variable name — which is frozen in this
spec, so it is not a blocking dependency.

**Slice B does not wait for slice A.** Every contract it consumes (`tag.*` frames, `status.reason`
values) is frozen in master §5.1 / §7.2 and repeated below; slice B builds against the contract and
mocks the server, exactly as iteration 2's frontend slices did.

### Cross-slice contract freeze (do not renegotiate mid-flight)

```jsonc
// GET /api/ai/status  — always 200
{ "enabled": true, "available": false, "model": "qwen2.5:3b", "reason": "auth_failed" }
// reason ∈ null | "disabled" | "unreachable" | "auth_failed" | "model_unavailable"
// reason is null iff available === true

// SSE frames (master §7.2, iteration-4 delta)
event: tag.created   data: {"origin":<uuid|null>,"tag":{"id":"…","name":"errand","todo_count":1}}
event: tag.updated   data: {"origin":<uuid|null>,"tag":{"id":"…","name":"errands","todo_count":3}}
event: tag.deleted   data: {"origin":<uuid|null>,"id":"…"}
```

---

## 2. Slice A — backend, ai-agent, devops

### A1 · IT3-1 · `tag.*` events  — **complex (opus)**

Files: `backend/app/repositories/sql.py`, `backend/app/repositories/tags.py`,
`backend/app/repositories/todos.py`, `backend/app/repositories/protocols.py`,
`backend/app/events.py`, `backend/app/routers/tags.py`, `backend/app/routers/todos.py`,
tests in `backend/tests/`.

**Frame builders** (`app/events.py`, next to the existing ones):

```python
def tag_created_event(tag: dict[str, Any], origin: str | None) -> Event:  # "tag.created"
def tag_updated_event(tag: dict[str, Any], origin: str | None) -> Event:  # "tag.updated"
def tag_deleted_event(tag_id: UUID | str, origin: str | None) -> Event:   # "tag.deleted"
```
plus `EventPublisher.stage_tag_created / stage_tag_updated / stage_tag_deleted`, mirroring the
list/todo staging methods exactly. `origin` stays at the top level of the payload, never inside
`tag`.

**Reporting which tags were actually inserted.** Tags are created inside the *todo* write path
(`SqlAlchemyTodoRepository.create` / `_sync_tags` call
`SqlAlchemyTagRepository.get_or_create_many`), so the router cannot see them. Three small changes,
in this order:

1. `app/repositories/sql.py` — add
   ```python
   async def get_or_create_reporting(session, *, find, build) -> tuple[T, bool]:
       """(row, created_by_us). ``created_by_us`` is False when ``find`` won the
       race and returned somebody else's row — that row is not ours to announce."""
   ```
   and reduce `get_or_create` to `return (await get_or_create_reporting(...))[0]` so the savepoint
   logic exists once.
2. `app/repositories/tags.py` — `SqlAlchemyTagRepository` keeps a per-instance outbox
   `self._created: list[Tag]`, appended by `get_or_create_many` **only** for rows
   `get_or_create_reporting` reports as genuinely inserted. Add
   `def take_created_tags(self) -> list[Tag]` which returns the list in insertion order and clears
   it. The signature of `get_or_create_many` does **not** change.
   The repository is request-scoped (`Depends(get_tag_repository)` builds a new instance per
   request), so the outbox cannot leak between requests — assert that in a test.
3. `app/repositories/todos.py` — `SqlAlchemyTodoRepository.take_created_tags()` delegates to the
   `SqlAlchemyTagRepository` it owns. `protocols.py` gains `take_created_tags` on both
   `TagRepository` and `TodoRepository` (master §4.2, iteration-4 delta, already written).

**Routers.**
- `routers/todos.py` — in the three write handlers, immediately before the existing
  `events.stage_todo_*` call:
  ```python
  for tag in repo.take_created_tags():
      events.stage_tag_created(
          TagResponse(id=tag.id, name=tag.name,
                      todo_count=0 if todo.parent_id else 1).model_dump(mode="json")
      )
  ```
  No extra query: a tag that did not exist before this request can be attached to nothing but the
  todo being written, and `TagResponse.todo_count` counts top-level todos only.
- `routers/tags.py` — replace the unused `_client_id: ClientId = None` parameters with
  `events: Events` (`Annotated[EventPublisher, Depends(get_event_publisher)]`, as in `lists.py`),
  stage the frame, and swap `await session.commit()` for
  `await commit_and_publish(session, events)` in **both** handlers. The frame for `rename_tag`
  carries the `TagResponse` the handler already builds (it computes `todo_count` for its own
  response) — build the response object first, stage it, then commit-and-publish, then return.

**Deliberately not done:** no per-todo fan-out on rename/delete (unbounded burst; the contract
makes the receiver refetch instead — master §7.2 delta), and no `tag.deleted` for tags orphaned by
an edit (an orphaned tag stays in the vocabulary by design, `list_with_counts` keeps it at 0).

**Backend tests** (`backend/tests/test_events_broadcast.py` unless noted):
- rename → exactly one `tag.updated`, correct id/name/`todo_count`, correct `origin`.
- delete → exactly one `tag.deleted` with the id.
- `POST /api/todos` with one new + one existing tag → exactly one `tag.created`, for the new one,
  ordered **before** `todo.created` in the staged sequence.
- same via `PATCH /api/todos/{id}` and `POST /api/todos/{id}/subtasks`; the subtask case asserts
  `todo_count == 0`.
- no new tags → no `tag.created`.
- another user's stream receives nothing (isolation).
- publish-after-commit: a rename that fails on `TagNameTaken` publishes nothing
  (`test_tags_api.py`).
- request scoping: two sequential requests do not see each other's created tags
  (`test_repository.py`).
- `get_or_create_reporting` returns `created=False` when the row already existed and when the
  savepoint race finds the winner's row (`test_sql_helpers.py`).

### A2 · IT3-2 · AI status detects a secret mismatch — **intermediate (sonnet)**

Files: `ai-agent/app/routers/ai.py`, `ai-agent/app/schemas.py`, `backend/app/ai_client.py`,
`backend/app/schemas/ai.py`, `backend/app/routers/ai.py`, tests in both suites.

**ai-agent** — add to the existing `/ai` router (which already carries
`dependencies=[Depends(verify_internal_token)]`, so authentication is inherited, not re-invented):

```python
@router.get("/ping", response_model=PingResponse)   # {"status": "ok"}
async def ping() -> PingResponse: ...
```
No Ollama client dependency, no I/O. Tests: 200 with the right token; 401 with a wrong token, an
absent header, and an empty header (add to `ai-agent/tests/test_auth.py`, which already
parametrises the `/ai/*` routes — extend its route list so the new endpoint is covered by the
existing auth matrix rather than by a copy of it).

**backend `ai_client.py`:**

```python
@dataclass(frozen=True, slots=True)
class AiHealth:
    available: bool
    model: str | None = None
    reason: str | None = None          # None | "unreachable" | "auth_failed" | "model_unavailable"
```

- `health()` keeps its 10 s memoisation and its "failures are cached like successes" rule.
- `_probe_health()` is unchanged apart from returning the extra field.
- New `_probe_ping() -> Literal["ok", "unauthorized", "error"]`: `GET /ai/ping` with
  `HEALTH_TIMEOUT_SECONDS`; 2xx → `ok`; HTTP 401 → `unauthorized`; anything else, including every
  exception → `error`. It never raises.
- The two probes run **concurrently** (`asyncio.gather`) so the worst case stays ≈3 s rather than
  6 s; both already swallow their own failures, so no `return_exceptions` handling is needed.
- Combination, in this precedence order:

  | health probe | ping | `available` | `reason` |
  |---|---|---|---|
  | failed | any | `false` | `unreachable` |
  | ok | `unauthorized` | `false` | `auth_failed` |
  | ok | `error` | `false` | `unreachable` |
  | ok, `ollama != "ok"` or `model_present` false | `ok` | `false` | `model_unavailable` |
  | ok, model present | `ok` | `true` | `null` |

- `auth_failed` logs one WARNING reusing the wording already in `_log_error_status`: the backend
  and ai-agent disagree about `AI_AGENT_TOKEN`, set the same value on both. **Never log the token.**

**backend `/api/ai/status`:** `AiStatusResponse` gains `reason: str | None = None`. With
`AI_ENABLED=false` it returns `{"enabled": false, "available": false, "model": null, "reason":
"disabled"}` and still makes no upstream call.

**Backend tests** (`backend/tests/test_ai_api.py`, driving the existing `FakeAgent` transport):
each row of the table above → the expected `available`/`reason`; `reason` is `null` exactly when
`available` is true; the memoisation still collapses a burst of `/status` calls into one pair of
upstream probes; the two probes are issued concurrently (assert both were requested within one
`health()` call); the token never appears in any log record (`caplog`) or response body.

### A3 · IT3-3 · Placeholder secret guard — **mechanical (haiku)**

Files: `backend/app/config.py`, `ai-agent/app/config.py`, `backend/tests/test_config.py`,
`ai-agent/tests/test_auth.py` (or a new `ai-agent/tests/test_config.py`).

One shared rule set, duplicated verbatim in both services (they are separate `uv` projects with no
shared package — **decision D-IT4-3**; each copy carries a comment naming the other file, and both
test suites assert the identical corpus, so drift fails a test rather than a deployment):

```python
# Matched on value.strip().casefold()
PLACEHOLDER_EXACT      = { …the literals already listed in each service… }
PLACEHOLDER_PREFIXES   = ("change-me", "change_me", "changeme",
                          "replace-me", "replace_me", "replaceme",
                          "your-", "your_", "yoursecret",
                          "example", "placeholder", "insert-",
                          "dummy", "sample", "secret-", "xxx")
PLACEHOLDER_SUBSTRINGS = ("change-me", "change_me", "changeme",
                          "replace-me", "replaceme", "placeholder",
                          "your-secret", "your_secret", "insert-your",
                          "do-not-use", "donotuse")
```

`is_placeholder_secret(value, known=frozenset())` returns true on an exact match against
`known | PLACEHOLDER_EXACT`, a prefix match, or a substring match. Both services keep their
existing call sites and error copy; only the predicate widens.

Two rules were **considered and rejected**, and the reasons are recorded so a reviewer does not
re-open them:

- **A `test-` prefix / `not-a-real` substring.** Both would reject the repository's own harness
  secrets (`backend/tests/conftest.py::TEST_JWT_SECRET`, `TEST_AI_AGENT_TOKEN`,
  `ai-agent/tests/conftest.py::TEST_TOKEN`) and break both suites wholesale, for no deployment
  safety: a test harness's secret guards nothing. Accepted residual gap — an operator who sets
  `JWT_SECRET=test-…` still only faces the length rule.
- **A low-entropy rule** (e.g. "fewer than four distinct characters"). It would reject `"0"*64`,
  `"x"*40`, `"z"*64`, `"f"*64` and `"a"*n`, which the existing backend suite uses in at least
  `test_config.py`, `test_security.py`, `test_ratelimit.py` and `test_live_server.py`. The churn
  is large, the gain small next to the length + placeholder rules, and one of those tests asserts a
  *length* failure that this rule would pre-empt with the wrong message.

Out of scope: `POSTGRES_PASSWORD` and `DATABASE_URL` (the backend never validates them; the URL is
opaque to `Settings`). Note it in the spec, not in the code.

**Tests.** In each service: a parametrised corpus of ≥12 placeholder strings (covering every
prefix, every substring, mixed case and surrounding whitespace) is rejected with a message naming
the variable; a corpus of three real-shaped secrets (64-char hex, a 40-char base64-ish string, and
the existing harness constants) is accepted; the two corpora are literally the same list in both
services.

### A4 · IT3-4 · Broker multi-replica — **mechanical (haiku)**

**Decision D-IT4-4: document + reserve, do not implement Redis.** Rationale: this is a
single-user personal app running one backend container; a Redis dependency would add a service, a
client library, a failure mode and a security surface to solve a problem nobody has. What *is*
wrong today is that the constraint is invisible — nothing stops `docker compose up --scale
backend=3`, and the failure is silent (some tabs simply stop updating). So the constraint becomes
loud, and the name a future implementation needs is reserved now.

- `backend/app/config.py`: `REALTIME_BACKEND: Literal["memory"] = "memory"`, with a validator
  message that names the supported value and points at README "Realtime" (pydantic's own
  `Literal` error is not actionable enough for an operator).
- `backend/app/main.py` lifespan: one INFO line, always, e.g.
  `Realtime fan-out: in-process broker (REALTIME_BACKEND=memory) — run exactly ONE backend replica; with more, some tabs miss events.`
- `docker-compose.yml`: `REALTIME_BACKEND: ${REALTIME_BACKEND:-memory}` in the backend
  environment, plus a comment above the service: single replica only, never add `deploy.replicas`
  or `--scale backend=N`, see README.
- `.env.example`: the variable with a one-line comment.
- README "Realtime": promote the existing bullet into a short "Scaling out (not supported)"
  subsection — what breaks, the two future options, and that `EventBroker.subscribe` /
  `EventBroker.publish` are the only two seams to replace.

Tests: default is `memory`; `REALTIME_BACKEND=redis` raises at `Settings` construction with a
message containing `memory`; `caplog` sees the startup line once when the app starts
(`test_config.py` / `test_events.py`).

### A5 · IT3-6 · GPU discovery + pinned Ollama image — **mechanical (haiku), devops**

- **Pin.** Replace `ollama/ollama:latest` on both `ollama` and `ollama-pull` with a single
  `image: ${OLLAMA_IMAGE:-<pin>}`, and add `OLLAMA_IMAGE` to `.env.example`. Choose `<pin>` from
  what is **already on this host** — do not trigger a network pull and do not guess a tag (the
  iteration-2 `postgres:17` lesson, master §17):
  1. `docker compose --profile ai exec ollama ollama --version` (or
     `docker run --rm ollama/ollama:latest --version`) gives the running version;
  2. if `docker image inspect ollama/ollama:<version>` resolves locally, pin that tag;
  3. otherwise pin by digest: `docker image inspect ollama/ollama:latest --format '{{index .RepoDigests 0}}'`
     → `image: ollama/ollama@sha256:…`.
  Record the chosen value and the reason in `DASHBOARD.md` at merge time.
- **`docker-compose.gpu.yml`:** add `environment: CUDA_VISIBLE_DEVICES: "0"` with a comment — on a
  single-GPU host it removes one source of discovery ambiguity — and keep the `deploy.resources`
  reservation exactly as it is.
- **README "GPU acceleration" → new "Troubleshooting: GPU discovery" subsection:**
  the symptom (a discovery timeout on some starts, success on others, with the container silently
  falling back to CPU); detection —
  `docker compose --profile ai logs ollama | grep -i -e gpu -e cuda -e "looking for compatible"`
  and `nvidia-smi` showing no `ollama` process while a request is in flight; remedy —
  `docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai restart ollama`,
  then re-check, keeping `OLLAMA_KEEP_ALIVE` so the model stays resident; and the reassurance that
  CPU fallback is degraded, not broken (the 45/50/60 s timeout ladder is sized for CPU).
- Verify: both `docker compose config` invocations succeed, and `docker compose --profile ai up -d`
  brings the stack up with the pin.

### A6 · IT3-8 · Relative dates in the AI prompts — **intermediate (sonnet)**

Files: `ai-agent/app/prompts.py`, new `ai-agent/tests/test_prompts.py`, new
`ai-agent/tests/test_live_dates.py`.

Root cause: `_date_anchors` precomputes only `TODAY` and `TOMORROW`, and `_DUE_DATE_RULE` then asks
a 3B model to do the rest of the arithmetic — which it cannot. The fix is to turn every relative
expression the app cares about into a **lookup**.

Make `date_anchors(today)` public and emit a labelled block:

```
TODAY=2026-09-03 (Thursday)
TOMORROW=2026-09-04
DAY_AFTER_TOMORROW=2026-09-05
NEXT_MONDAY=2026-09-07   NEXT_TUESDAY=2026-09-08   … NEXT_SUNDAY=2026-09-06
NEXT_WEEK=2026-09-07
IN_7_DAYS=2026-09-10   IN_14_DAYS=2026-09-17   IN_30_DAYS=2026-10-03
END_OF_MONTH=2026-09-30
```

- `NEXT_<WEEKDAY>` = the next such weekday **strictly after** TODAY (so on a Monday,
  `NEXT_MONDAY` is TODAY+7).
- `NEXT_WEEK` = Monday of the following week (always equal to `NEXT_MONDAY`; kept as its own label
  because the model matches on the words, not on the arithmetic).
- The block costs well under 100 tokens against a 512-token budget.

`_DUE_DATE_RULE` becomes, in substance: *`due_date` is an ISO date, YYYY-MM-DD, and must be
**copied from the DATES block** — never computed. "today" → TODAY, "tomorrow" → TOMORROW, "the day
after tomorrow" → DAY_AFTER_TOMORROW, a bare or "next" weekday → the matching NEXT_<WEEKDAY>,
"next week" → NEXT_WEEK, "in N days" → the matching IN_N_DAYS when present and otherwise counted
from TODAY, "end of the month" → END_OF_MONTH. Use null when the text implies no date. Never
output a date that is not in the block unless the note states an explicit calendar date.*

`suggest_metadata_user` already embeds `date_anchors`, so it inherits the improvement; the priority
rule's "a deadline within two days" becomes checkable against real anchors.

**Deterministic tests** (`test_prompts.py`, pure functions, run in CI):
every documented key is present; the weekday anchors land on the right weekday and are strictly
after TODAY, verified for all seven possible TODAY weekdays; `NEXT_WEEK == NEXT_MONDAY`;
`IN_N_DAYS` arithmetic; `END_OF_MONTH` for 28-, 29- (leap), 30- and 31-day months.

**Live lane** (`test_live_dates.py`, skipped unless `AI_AGENT_LIVE=1`, never in CI): with
TODAY = 2026-09-02 (a Wednesday), five notes with a deterministic expected date —
"next Tuesday" → 2026-09-08 · "in 10 days" → 2026-09-12 · "the day after tomorrow" → 2026-09-04 ·
"on friday" → 2026-09-04 · "next week" → any date in 2026-09-07..2026-09-13 (the English itself is
fuzzy). These assert dates, not wording. **Acceptance: ≥4 of 5 on `qwen2.5:3b`.** If the model
scores lower, the fallback is documentation, not a blocked merge: the README states that relative
dates beyond "tomorrow" may need a correction in the draft — which is editable before anything is
written (D-AI1).

**Decision D-IT4-5: no automatic model switching on a GPU host.** The backlog row floated "a larger
model when a GPU is present". Compose cannot detect a GPU portably, and a 7B model does not fit
every host; instead README "Choosing a model" gains one sentence saying `OLLAMA_MODEL=qwen2.5:7b`
measurably improves relative-date resolution on a GPU host and is a deliberate opt-in. No code
change.

---

## 3. Slice B — frontend

Files: `frontend/src/App.tsx`, `frontend/src/api/ai.ts`, `frontend/src/components/TodoComposer.tsx`,
`frontend/src/components/TodoItem.tsx`, `frontend/src/components/AiAddBox.tsx`,
`frontend/src/components/DailySummaryPanel.tsx`, their tests, `frontend/e2e/keyboard.spec.ts`.
Slice B touches **no** backend file and builds against the frozen contract in §1.

### B1 · IT3-7 · Focus after deleting a todo — **intermediate (sonnet)**

The whole difficulty is *not* stealing focus: IT3-9 shipped a post-delete refocus that fired on
event-driven list changes and had to be reverted. Three guards, all required:

1. **One-shot intent.** A `useRef` is set **only** inside `handleDelete`, only after
   `deleteTodo(id)` resolved, and is cleared by the effect that consumes it. Nothing else in the
   app can set it, so a refetch, a poll or a filter change cannot trigger a focus move.
2. **Neighbours captured before the removal.** `handleDelete` already has `todos` in scope: record
   `nextId = todos[i+1]?.id ?? null` and `prevId = todos[i-1]?.id ?? null` *before* filtering the
   row out, and store them with the intent.
3. **Focus-was-actually-lost check.** The effect acts only when
   `document.activeElement === document.body` (or `null`) — the exact state after the focused
   Delete button unmounts. If the user has already moved on, do nothing.

Target resolution, in order: next row's Delete → previous row's Delete → the composer's title
input (`#new-todo-title`) → the composer section. Addressing is by DOM id rather than by threading
refs through `TodoList`:

- `TodoItem` renders its Delete button with `id={deleteButtonId(todo.id)}`, where
  `export const deleteButtonId = (id: string) => \`todo-delete-${id}\`` lives in `TodoItem.tsx`.
- `TodoComposer` renders its `<section>` with `id="todo-composer"` and `tabIndex={-1}`, so there is
  always a valid last resort — including when the composer is on the "Draft with AI" tab and
  `#new-todo-title` is not focusable. Focusing a `tabindex="-1"` container is announced through its
  existing `aria-label="Add a todo"`.

Scope: **top-level rows only.** Subtask rows (`SubtaskList`) have the same dead end and are
deliberately deferred — see §6, backlog row IT4-3 — to keep this change small enough to review as
one idea.

Tests (Vitest, `App.test.tsx`): delete a middle row → next row's Delete focused; delete the last
row → previous row's Delete; delete the only row → `#new-todo-title`; delete the only row with the
AI tab active → the composer section; a `todo.deleted` frame from another tab removes a row →
`document.activeElement` unchanged (**the IT3-9 regression test**); a failed delete → focus
unchanged and the error copy shown. Playwright (`keyboard.spec.ts`): keys only — Tab to a row's
Delete, Enter, assert the next row's Delete is focused.

### B2 · IT4-1 · Composer keeps its state across tab switches — **intermediate (sonnet)**

**Decision D-IT4-6: keep both panels mounted and hide the inactive one; do not lift state.**
Conditional rendering is why state is lost. Hiding preserves it in the components that own it, with
no new props, no state duplication and no change to `AddTodoForm` or `AiAddBox` at all.

- `TodoComposer` renders **both** `role="tabpanel"` elements, each with its own stable id
  (`panelId('manual')`, `panelId('ai')`) — which is also what makes each tab's existing
  `aria-controls` correct for the first time. The inactive panel gets the `hidden` attribute
  (`display:none`), which removes it from the accessibility tree and from the tab order in every
  supported browser.
- Focus stays on the tab button after activation; do **not** autofocus into the panel (it would
  break the tablist pattern).
- Because `AiDraftPreview` no longer remounts on a tab round trip, its mount-time
  `titleRef.current?.focus()` no longer fires on return — which is the desired behaviour and gets
  its own assertion.
- When `aiStatus.enabled` is false the manual form is still rendered on its own, unchanged.

Tests (`TodoComposer.test.tsx`): title survives Add → AI → Add, together with the "More options"
disclosure and the priority/due/tags fields; the AI note survives AI → Add → AI; an unconfirmed
draft (mocked `parseTodo`) survives the round trip **including edits made to it**, and focus is not
stolen on return; while the AI tab is active, `getByRole('textbox', { name: /new todo title/i })`
finds nothing (proving `hidden` really hides it); the Arrow/Home/End roving-tabindex contract from
the existing tests still passes unchanged.

### B3 · IT3-1 client half · `tag.*` frames — **intermediate (sonnet)**

Today `App.tsx::handleStreamEvent` refetches on *any* non-own frame, so `tag.*` frames already
refresh todos, lists and tags with no production change. Two things are still required:

1. **Proof.** `App.realtime.test.tsx` gets tests that a `tag.updated` and a `tag.deleted` frame
   from another origin trigger exactly one debounced refetch of todos **and** tags, and that a
   frame carrying our own `origin` triggers none (echo suppression).
2. **Stale-filter repair** (acceptance criterion 8). `handleStreamEvent` gains a `tag.*` branch,
   next to the existing `list.deleted` one:
   - `tag.deleted`: look the id up in the current `tags` state; if its name is in `filters.tags`,
     remove it from the filter before scheduling the refetch.
   - `tag.updated`: look the id up in the current `tags` state; if the stored name differs from
     `data.tag.name` and the old name is in `filters.tags`, replace it with the new one.
   The id→name map has to come from client state because the frames carry ids while the filter
   works in names (master §6.3). If the id is unknown locally, do nothing beyond the refetch.
   Tests cover: filtered-on tag renamed → the filter follows and the refetch uses the new name;
   filtered-on tag deleted → the filter drops it and the view is not left empty; a tag the user is
   not filtering on → filters untouched.

### B4 · IT3-2 client half · `status.reason` copy — **mechanical (haiku)**

- `src/api/ai.ts`: `AiStatus` gains `reason: AiUnavailableReason | null` where
  `type AiUnavailableReason = 'disabled' | 'unreachable' | 'auth_failed' | 'model_unavailable'`;
  `getAiStatus` accepts only those literals and maps anything else — including a missing field, so
  an older backend keeps working — to `null`.
- New exported `aiUnavailableBanner(reason)`:
  - `auth_failed` → *"AI is unavailable: the AI service rejected this server's credentials. Everything else works as usual."*
  - `model_unavailable` → *"The AI model is still starting or has not been downloaded yet. Everything else works as usual."*
  - `unreachable` / `null` / unknown → the existing `AI_UNAVAILABLE_BANNER` (kept exported as the
    fallback, so nothing that imports it breaks).
  - `disabled` never renders: `enabled === false` already hides the whole AI section.
- `AiAddBox` and `DailySummaryPanel` take an optional `reason` prop from `App.tsx` (which already
  holds `aiStatus`) and render `aiUnavailableBanner(reason)` instead of the constant. No layout,
  role or `aria-live` change — same element, same styling, different string.
- The copy names no host, no service internals and no value. Tests: each reason renders its own
  copy; an unknown reason renders the generic one; `enabled:false` renders no banner at all.

---

## 4. QA gate — IT4-2 and the iteration exit

Run after both slices merge into `integrate/iteration-4`. QA writes tests only.

**New spec `frontend/e2e/ai-menu.spec.ts`** (new file — no conflict with `ai.spec.ts`), guarded by
`test.skip(process.env.E2E_AI !== '1', …)` with the same header comment as `ai.spec.ts` (how to
bring the AI profile up, the `PLAYWRIGHT_BROWSERS_PATH` export). Budget 90 s per model call.

1. Create a todo through the ordinary UI with a timestamped title, so the spec owns its data.
2. Open `AI actions for <title>` → **Split into subtasks**. Before accepting, assert the subtask
   count is unchanged (**D-AI1: nothing is written until the user accepts**). Accept; assert the
   row shows `n/m subtasks`; reload; assert they survived (they were written through the ordinary
   `POST /api/todos/{id}/subtasks`).
3. Reopen the menu → **Suggest priority and tags**. Assert the panel offers a priority in
   {Low, Medium, High} and at most five tags. Apply; assert the row's priority pill and tag chips
   changed to what was applied; reload; assert it survived.
4. Do at least one of the two menu interactions with the keyboard only (Enter on the trigger,
   ArrowDown, Enter) and assert focus returns to the menu trigger after the panel closes.
5. Never assert on the model's wording — only on shape, persistence and focus.

**Iteration exit checklist** (QA reports each with the number it ran):
- `cd backend && uv run pytest` — SQLite lane.
- `cd backend && TEST_DATABASE_URL=postgresql+asyncpg://todo:todo@localhost:5433/todo_test uv run pytest`
  (needs `docker compose up -d db`).
- `cd ai-agent && uv run pytest`.
- `cd frontend && npm run test` and the type/build check.
- `cd frontend && PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright npm run e2e`.
- `E2E_AI=1` run of `ai.spec.ts` + `ai-menu.spec.ts` with `docker compose --profile ai up -d`.
- `AI_AGENT_LIVE=1` run of `ai-agent/tests/test_live_dates.py`, recording the score out of 5.
- `docker compose up -d` and `docker compose --profile ai up -d` smoke, with the pinned Ollama
  image, plus one two-tab realtime check that a tag rename issued over the API
  (`curl -X PATCH /api/tags/{id}`) shows up in a second tab without a reload.
- A keyboard-only walk of the composer tabs and a row delete, plus a 390 px-wide layout check.

---

## 5. Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | The `tag.created` outbox on a request-scoped repository leaks between requests if DI scoping is ever changed | phantom frames | Explicit test that two sequential requests see independent outboxes; `take_created_tags` drains, so a second read is empty by construction |
| R2 | `todo_count` on a `tag.created` frame is computed, not queried, and could drift if `TagResponse.todo_count` ever counts subtasks | a chip claiming the wrong number | The rule is stated in master §7.2 next to the existing `list.created` precedent, and a test asserts 1 for a top-level write and 0 for a subtask write |
| R3 | The second ai-agent probe doubles `/api/ai/status` upstream traffic | slower status, more load on a wedged ai-agent | The probes run concurrently and share the existing 10 s memoisation, so a burst of tabs still costs one pair; the 3 s health timeout is unchanged |
| R4 | The widened placeholder guard rejects a legitimate secret (false positive) | the app refuses to start | Prefix/substring patterns only, no entropy heuristic; `openssl rand -hex 32` output cannot match any of them; every secret literal in both existing suites was checked against the table while writing this spec; failure is a loud startup message telling the operator to regenerate |
| R5 | The `hidden` inactive composer panel confuses an existing Vitest/Playwright locator | flaky or wrong tests | `getByRole` ignores `display:none`; the slice must run the **whole** frontend suite plus Playwright, and one test explicitly asserts the hidden panel is unreachable by role |
| R6 | Post-delete refocus regresses IT3-9 (stealing focus on event-driven changes) | blocker, already seen once | Three independent guards (one-shot ref set only in `handleDelete`, neighbours captured pre-removal, `activeElement === body` check) and a dedicated regression test for the event-driven path |
| R7 | The pinned Ollama tag is not present locally and triggers a network pull, or does not exist | `--profile ai up` breaks; repeat of the `postgres:17` incident | The pin is chosen from `docker image inspect` output on this host, digest-pinning as the fallback; verified by an actual `up` before the slice reports |
| R8 | `qwen2.5:3b` still gets relative dates wrong after the prompt change | the item is not really closed | The live lane measures it (≥4/5 acceptance) and the outcome is recorded in `DASHBOARD.md`; if it misses, the documented fallback is that the draft is editable before anything is written, plus the opt-in 7B note — no blocked merge |
| R9 | Slices A and B both need to be right about the frozen contract | integration surprise at merge | The contract is written into master §5.1/§7.2 **before** dispatch and repeated verbatim in §1 here; QA's two-tab check exercises the real backend against the real client |
| R10 | Naming `auth_failed` in user-visible copy leaks operational detail | security reviewer flag | The copy names no host, service, variable or value — "the AI service rejected this server's credentials" — and the endpoint is authenticated; the alternative (a generic banner for a misconfiguration only an operator can fix) was rejected as user-hostile |
| R11 | IT3-5 stays open and CI has still never run remotely | a workflow bug ships unnoticed | Explicitly out of scope and re-tagged "needs user"; every gate CI would run is run locally before merge |

## 6. Follow-ups this iteration deliberately creates

Add to the `DASHBOARD.md` backlog at iteration close:

- **IT4-3** — apply the IT3-7 focus rule to **subtask** rows (`SubtaskList`), which have the same
  keyboard dead end. Source: this spec, §3 B1. Weight: low.
- **IT3-5** — unchanged, retagged **"needs user"**: add an `origin` remote and run GitHub Actions
  once.
- Optional, only if the live lane scores poorly: evaluate `qwen2.5:7b` on the GPU host as the
  documented default for GPU users (D-IT4-5 keeps it a manual opt-in for now).

## 7. Effort estimate (agent time, rough)

| Slice / item | Estimate |
|---|---|
| A1 `tag.*` events (backend, complex) | 45–60 min |
| A2 AI status + `/ai/ping` (intermediate) | 25–35 min |
| A3 placeholder guard (mechanical) | 15–20 min |
| A4 `REALTIME_BACKEND` + docs (mechanical) | 15–20 min |
| A5 Ollama pin + GPU notes (mechanical, devops) | 20–30 min |
| A6 prompt date anchors + tests (intermediate) | 30–40 min |
| **Slice A total** (one agent, sequential) | **2h30–3h15** — or ~1h15 wall-clock if split into the three disjoint worktrees of §1 |
| B1 post-delete focus (intermediate) | 30–40 min |
| B2 composer state (intermediate) | 25–35 min |
| B3 `tag.*` client + filter repair (intermediate) | 25–35 min |
| B4 `status.reason` copy (mechanical) | 15–20 min |
| **Slice B total** | **1h35–2h10** |
| Review + security gates (2 rounds) | 30–45 min |
| QA (suites, E2E, `E2E_AI=1`, live dates, compose smoke) | 45–60 min |
| **Iteration** | **~3h30–4h30 wall-clock** with the two slices in parallel |

Note that the DASHBOARD's own estimate for this backlog was 45–90 min net; that was written before
the rows were designed. IT3-1 alone (a new event family with a repository-level change) is a
medium slice by the project's own estimation rule.

## 8. Open questions

**None.** Every contract-level choice is decided above and written into the master spec:
the `tag.*` frame shapes and their "no per-todo fan-out" rule (§7.2), the `status.reason` enum and
its mapping onto §5 (§5.1), the `GET /ai/ping` endpoint (§8.2), `REALTIME_BACKEND` (§9.1, §7.3),
and `take_created_tags` on the repository protocols (§4.2). The one thing an implementer must
*discover* rather than read — the exact Ollama image pin — has a decision procedure in §2 A5 that
cannot invent a tag that does not exist locally.

## 9. Decisions recorded (do not re-litigate)

- **D-IT4-1** `tag.created` is emitted even though the current reference client does not strictly
  need it (it refetches on `todo.created` anyway). The tag vocabulary is a user-visible resource
  driving the filter chips, §7.2 permits surgical application of frames, and leaving one entity
  family without a `created` frame is a trap for the next implementer. Cost: ~15 lines, fully
  tested.
- **D-IT4-2** Tag rename/delete does **not** fan out one `todo.updated` per affected todo; the
  contract makes the receiver refetch instead (unbounded burst otherwise).
- **D-IT4-3** The placeholder table is duplicated verbatim in `backend/app/config.py` and
  `ai-agent/app/config.py` — they are separate `uv` projects with no shared package — and kept
  honest by an identical test corpus in both suites rather than by a new shared library.
- **D-IT4-4** Realtime stays in-process. `REALTIME_BACKEND` is reserved with the single value
  `memory`; no Redis, no `LISTEN/NOTIFY`, in iteration 4.
- **D-IT4-5** No automatic model switching when a GPU is present; `OLLAMA_MODEL=qwen2.5:7b` is a
  documented manual opt-in.
- **D-IT4-6** The composer keeps both panels mounted and hides the inactive one, rather than
  lifting `AddTodoForm` / `AiAddBox` state into `TodoComposer`.
- **D-IT4-7** IT3-7's focus rule covers top-level rows only this iteration; subtask rows become
  backlog row IT4-3.
- **D-IT4-8** IT3-5 (git remote + first GitHub Actions run) is out of scope: it needs credentials
  only the user has and is outward-facing.
