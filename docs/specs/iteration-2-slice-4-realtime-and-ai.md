# Iteration 2 · Slice 4 — Realtime (SSE) + `ai-agent` Service + Ollama + AI UI

Status: **ready for dispatch after slice 3 merges** · Depends on: slice 3
Read together with `docs/specs/iteration-2-master.md` §6.5, §6.6, §7, §8, §9, §10 (binding).

## Goal in one sentence
A second browser tab reflects every change without a reload, and a small Ollama model — running
in Docker behind a separate `ai-agent` microservice — can draft todos from natural language,
split a todo into subtasks, suggest priority and tags, and write a daily summary — while the
whole app stays fully usable when no AI is available.

## Four parallel work units
| Unit | Owner | Paths | Depends on |
|---|---|---|---|
| **BACKEND-A** | backend agent #1, **complex** (opus) | `backend/**` | slice 3 |
| **BACKEND-B** | backend agent #2, **complex** (opus) | `ai-agent/**` (new tree, no overlap) | nothing but master §8 |
| **FRONTEND** | frontend agent, **complex** (opus) | `frontend/**` | the frozen contract §6.5/§6.6 |
| **DEVOPS** | devops agent, **intermediate** (sonnet) | `docker-compose*.yml`, `docker/**`, `ai-agent/Dockerfile`, `.env.example`, `README.md`, `.github/**` | nothing |

`ai-agent/Dockerfile` and `ai-agent/.dockerignore` are **devops-owned**; everything else under
`ai-agent/` is BACKEND-B's.

---

## BACKEND-A — realtime + AI proxy (`backend/**`)

### A1. Files
Create: `app/events.py`, `app/routers/events.py`, `app/ai_client.py`, `app/routers/ai.py`,
`app/schemas/ai.py`, `tests/test_events.py`, `tests/test_events_broadcast.py`,
`tests/test_ai_api.py`.
Modify: `app/main.py`, `app/deps.py`, `app/config.py`, `app/ratelimit.py`,
`app/routers/todos.py`, `app/routers/lists.py`.
Add dependency: `httpx` moves from the dev group to runtime.

### A2. `app/events.py`
```python
@dataclass(frozen=True)
class Event:
    name: str                 # "todo.created" | "todo.updated" | "todo.deleted" | "list.*"
    data: dict[str, Any]      # already JSON-serializable; always contains "origin"

class EventBroker:
    def subscribe(self, user_id: UUID) -> AbstractAsyncContextManager[asyncio.Queue[Event]]
    async def publish(self, user_id: UUID, event: Event) -> None
    @property
    def subscriber_count(self) -> int          # for tests
```
- `dict[UUID, set[Queue]]`, `Queue(maxsize=100)`; `publish` uses `put_nowait` and on `QueueFull`
  **drops that subscriber** (removes the queue and pushes a sentinel so its generator exits) —
  a stalled client must never grow memory.
- `format_sse(event) -> str` produces `event: {name}\ndata: {json}\n\n`; the JSON must be a single
  line (`json.dumps(..., separators=(",", ":"))` — a newline inside `data` would break framing).
- Builders: `todo_created_event(todo, origin)`, `todo_updated_event(todo, origin)`,
  `todo_deleted_event(todo_id, list_id, origin)`, `list_*` equivalents. The todo payload is the
  serialized `TodoResponse` produced by the same code path the HTTP response uses.
- One broker per app on `app.state.broker`, provided by `get_broker`.

### A3. `GET /api/events` (`app/routers/events.py`)
- Auth via the normal `Depends(get_current_user)` (the frontend sends the bearer header — master
  D-R1; there is **no** `?token=` query parameter and adding one is forbidden).
- Returns `StreamingResponse(generator(), media_type="text/event-stream", headers={
  "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})`.
- Generator: emit `event: ready\ndata: {"user_id":"…"}\n\n`, then loop:
  `await asyncio.wait_for(queue.get(), timeout=25)` → yield the frame;
  `TimeoutError` → yield `: keep-alive\n\n`;
  `asyncio.CancelledError` / `await request.is_disconnected()` → unsubscribe and return.
- The route **must not hold a DB session open** for the life of the stream: resolve the user, then
  release the session (use a dedicated dependency that closes before streaming starts, or read the
  user id into a local and depend on nothing else). A test asserts the connection pool is not
  exhausted by `pool_size + 2` concurrent streams.

### A4. Publishing from mutations
Every mutating todo/list handler publishes **after** the transaction commits (use a small helper
that collects the event and emits it in a `finally`/after `session.commit()` — never publish a
change that then rolls back).
- create top-level todo → `todo.created` with that todo
- update/delete/create of a **subtask** → `todo.updated` carrying the **reloaded top-level parent**
  (with `subtasks` and `tags`) — master §7.2
- update of a top-level todo → `todo.updated`; delete → `todo.deleted` (`{id, list_id}`)
- moving a todo between lists → a single `todo.updated` (the payload carries the new `list_id`)
- list create/rename → `list.created` / `list.updated`; list delete → `list.deleted` plus **one**
  `todo.deleted` per top-level todo it contained (the client can also just refetch on
  `list.deleted`; emit both and let the client be idempotent)
- `origin` = the `X-Client-Id` header value (validated as a UUID string) or `null`.

### A5. AI proxy (`app/routers/ai.py`, `app/ai_client.py`, `app/schemas/ai.py`)
Implements master §6.6. Rules:
- All endpoints require auth and are rate-limited **20 requests / 5 min per user id** →
  429 `rate_limited` + `Retry-After`.
- `AI_ENABLED=false` → every endpoint except `/api/ai/status` returns 503 `ai_disabled`;
  `/api/ai/status` returns `{"enabled": false, "available": false, "model": null}`.
- `ai_client.py`: a module-level `httpx.AsyncClient` created in `lifespan` with
  `timeout=httpx.Timeout(connect=5, read=settings.AI_TIMEOUT_SECONDS, write=10, pool=5)`,
  base URL `settings.AI_AGENT_URL`, and the `X-Internal-Token: settings.AI_AGENT_TOKEN` header on
  every request. Mapping: `httpx.TimeoutException` → 504 `ai_timeout`;
  `httpx.ConnectError`/`RequestError`/5xx from ai-agent → 503 `ai_unavailable`;
  ai-agent 401 → 503 `ai_unavailable` **and a warning log** (misconfigured shared secret — never
  surface the token). The user never sees an ai-agent error body verbatim.
- `/api/ai/status` calls `GET {AI_AGENT_URL}/health` with a **3 s** timeout and always returns 200:
  `{"enabled": settings.AI_ENABLED, "available": <bool>, "model": <str|null>}`.
- Prompt-context assembly happens **here**, not in ai-agent (ai-agent never touches the DB):
  - `parse-todo` → forwards `text`, `today`, and the caller's `known_tags` (their tag names, ≤ 50).
  - `suggest-subtasks` → loads the todo (404 `todo_not_found` if absent/foreign), forwards
    `title`, `description`, `max_items` (default 5, clamped 1..10).
  - `suggest-metadata` → loads the todo, forwards `title`, `description`, `known_tags`, `today`.
  - `daily-summary` → loads the caller's **active top-level todos** in `list_id` (or all lists),
    capped at 50, ordered by due date then priority; forwards
    `[{title, priority, due_date, completed, list_name}]` and `today`. Returns `todo_count` =
    the number of todos actually sent.
- **No AI endpoint writes to the database** (master D-AI1) — a test asserts the DB is unchanged
  after each call.
- Never log the model's raw output at `info`; `debug` only.

### A6. Tests (BACKEND-A)
1. **broker unit** — subscribe/publish/unsubscribe; a full queue drops only that subscriber;
   events for user A never reach user B; `format_sse` output framing including a payload with a
   newline in the title.
2. **stream** — `GET /api/events` without a token → 401; with a token → first frame is `ready`;
   a `POST /api/todos` from another client produces a `todo.created` frame containing the todo;
   `X-Client-Id` echoes into `origin`; keep-alive appears when idle (patch the 25 s timeout to
   ~0.1 s in the test); disconnecting removes the subscriber. Use `httpx.AsyncClient` +
   `ASGITransport` with `client.stream("GET", ...)`.
3. **subtask events** — creating/updating/deleting a subtask emits `todo.updated` carrying the
   parent with its subtasks embedded.
4. **transaction ordering** — a handler that raises after the DB write (monkeypatched) publishes
   **no** event.
5. **session lifetime** — `pool_size + 2` concurrent streams plus a normal request all succeed.
6. **AI proxy** — with a mocked ai-agent (`httpx.MockTransport`): happy paths for all four
   endpoints and `status`; `ai-agent` 503 → 503 `ai_unavailable`; timeout → 504 `ai_timeout`;
   `AI_ENABLED=false` → 503 `ai_disabled` and `status.enabled=false`; missing/foreign `todo_id` →
   404; rate limit exceeded → 429 with `Retry-After`; the `X-Internal-Token` header is sent and
   never appears in a response or log; the database is untouched after every AI call.

---

## BACKEND-B — the `ai-agent` microservice (`ai-agent/**`)

A standalone uv project. **It never talks to PostgreSQL and has no auth beyond the shared secret.**

### B1. Layout
```
ai-agent/
├── pyproject.toml            # requires-python >=3.12; deps: fastapi, uvicorn[standard], httpx, pydantic-settings
├── uv.lock                   # committed
├── README.md                 # what it is, how to run it standalone, env vars
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, lifespan httpx client, /health + /ai/* routers
│   ├── config.py             # Settings: OLLAMA_BASE_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT_SECONDS,
│   │                         #           AI_AGENT_TOKEN (required), LOG_LEVEL, APP_ENV
│   ├── auth.py               # verify_internal_token dependency (X-Internal-Token)
│   ├── ollama.py             # OllamaClient: chat_json(schema, system, user) + list_models()
│   ├── prompts.py            # the four system prompts + JSON schemas, as module constants
│   ├── sanitize.py           # clamping/normalization helpers (the "never trust the model" layer)
│   ├── schemas.py            # request/response Pydantic models (requests extra="forbid")
│   └── routers/ai.py
└── tests/
    ├── conftest.py           # TestClient + a fake Ollama transport
    ├── test_health.py
    ├── test_auth.py
    ├── test_parse_todo.py
    ├── test_suggest_subtasks.py
    ├── test_suggest_metadata.py
    ├── test_daily_summary.py
    ├── test_sanitize.py
    └── test_live_ollama.py    # skipped unless AI_AGENT_LIVE=1
```

### B2. `ollama.py`
```python
async def chat_json(self, *, system: str, user: str, schema: dict, max_tokens: int = 512) -> dict
```
- `POST {base}/api/chat` with
  `{"model": settings.OLLAMA_MODEL, "stream": False, "format": schema,
    "options": {"temperature": 0.2, "num_predict": max_tokens},
    "messages": [{"role":"system","content":system},{"role":"user","content":user}]}`.
- If Ollama rejects a schema `format` (4xx mentioning `format`), retry once with `"format": "json"`.
- Parse `response["message"]["content"]` as JSON. If it is not valid JSON, **strip a markdown code
  fence** and retry the parse once before failing.
- Errors → `OllamaUnavailable` (connection refused, 404 model not found, 5xx) or
  `OllamaTimeout` (`httpx.TimeoutException`).
- `list_models()` → `GET /api/tags`, used by `/health` to report `model_present`.
- Timeout: `httpx.Timeout(connect=5, read=OLLAMA_TIMEOUT_SECONDS, write=10, pool=5)`.

### B3. Endpoint pipeline (identical for all four)
1. Validate the request (`extra="forbid"`).
2. Build the user message from a small template (`prompts.py`); include `TODAY={today}` and
   `KNOWN_TAGS={…}` where relevant.
3. `chat_json` with the endpoint's JSON schema.
4. Validate with a Pydantic "raw model" (`extra="ignore"` — the model may add keys).
5. On `ValidationError`, **retry once** with an extra user message:
   `Your previous reply was invalid: {errors}. Reply with JSON only, matching the schema.`
6. On a second failure → 503 `{"detail":"AI produced an invalid response","code":"ai_invalid_response"}`.
7. **Sanitize** (`sanitize.py`) — mandatory, tested independently:
   - `title`: strip, collapse whitespace, drop a trailing `.`, truncate to 200, reject empty
   - `description`: strip, truncate to 2000, empty → `None`
   - `priority`: lowercase, must be one of low/medium/high else `"medium"`
   - `due_date`: parse ISO; reject non-parsable or a date more than 5 years from `today` → `None`
   - `tags`: normalize per master §3.1, drop invalid, dedupe, cap at 5
   - `subtasks`: sanitize each title, drop empties/duplicates, cap at `max_items` (≤ 10)
   - `summary`: strip, collapse blank lines, truncate at 800 chars on a word boundary
8. Return the sanitized response.

Prompts (system messages) live in `prompts.py` as constants, are short and imperative, and forbid
prose ("Reply with JSON only."). The `parse-todo` prompt is quoted in master §8.3 — implement it
essentially verbatim; write the other three in the same style:
- **suggest-subtasks**: "Break the todo into at most {max_items} concrete, ordered steps. Each
  step is a short imperative phrase ≤ 200 characters. Return fewer steps — or none — if the todo
  is already a single action. Reply with JSON only."
- **suggest-metadata**: "Choose a priority (low, medium, high) and 0–3 short lowercase tags for
  the todo. Prefer tags from KNOWN_TAGS when they fit. Use medium unless the todo clearly signals
  urgency or a deadline. Reply with JSON only."
- **daily-summary**: "Write a 2–4 sentence plain-text briefing about the user's open todos for
  TODAY. Mention what is overdue and what is due today, then the single most important thing to
  do next. No lists, no markdown, no headings. Reply with JSON only: {\"summary\": \"…\"}."

### B4. `/health`
Always 200: `{"status":"ok","ollama":"ok"|"unavailable","model":"<name>","model_present":bool}`.
It calls `list_models()` with a 3 s timeout and swallows every error.

### B5. Auth
`verify_internal_token`: compare `X-Internal-Token` to `settings.AI_AGENT_TOKEN` with
`hmac.compare_digest`; missing/wrong → 401 `{"detail":"Invalid internal token","code":"unauthorized"}`.
Applied to all `/ai/*` routes, **not** to `/health`. `AI_AGENT_TOKEN` is required at startup (fail
fast, no default).

### B6. Tests (BACKEND-B)
1. **auth** — every `/ai/*` route without / with a wrong token → 401; `/health` works without one.
2. **happy paths** (fake Ollama transport returning recorded good JSON) for all four endpoints,
   asserting the exact response shape.
3. **schema/format** — the outgoing Ollama request carries `stream: false`, the JSON schema in
   `format`, `temperature 0.2`, and the configured model name; the schema-rejection fallback to
   `"format":"json"` is exercised.
4. **garbage handling** — content that is not JSON → the fence-stripping retry then 503
   `ai_invalid_response`; content that is valid JSON but violates the schema → one repair retry
   (assert exactly two upstream calls) then success or 503.
5. **sanitization** (`test_sanitize.py`, pure unit) — a 500-char title truncated to 200;
   `priority:"URGENT"` → `medium`; `due_date:"next tuesday"` → `None`; `due_date:"2099-01-01"` →
   `None`; tags `["Home", "home", "bad!", "  work  "]` → `["home","work"]`; 12 subtasks capped;
   empty subtask titles dropped; a 3 000-char summary truncated to ≤ 800 on a word boundary.
6. **failures** — Ollama connection refused → 503 `ai_unavailable`; timeout → 504 `ai_timeout`;
   model-not-found 404 → 503 `ai_unavailable` with a message naming the model.
7. **live** (`test_live_ollama.py`, `@pytest.mark.skipif(os.getenv("AI_AGENT_LIVE") != "1")`) —
   one real `parse-todo` against a running Ollama, asserting only that the response validates and
   `title` is non-empty (never assert on model wording).

---

## FRONTEND — realtime client + AI UI (`frontend/**`)

### F1. Files
Create: `src/api/events.ts`, `src/hooks/useEventStream.ts`, `src/api/ai.ts`,
`src/components/AiAddBox.tsx`, `src/components/AiDraftPreview.tsx`,
`src/components/AiSuggestionPanel.tsx`, `src/components/DailySummaryPanel.tsx`,
`src/components/ConnectionStatus.tsx`, tests alongside, `e2e/realtime.spec.ts`, `e2e/ai.spec.ts`.
Modify: `src/App.tsx`, `src/components/TodoItem.tsx`, `src/components/TodoDetail.tsx`.

### F2. `src/api/events.ts` — SSE over `fetch` (master §7.1)
```ts
export interface StreamHandlers { onReady(): void; onEvent(name: string, data: unknown): void; onError(e: unknown): void }
export function openEventStream(handlers: StreamHandlers, signal: AbortSignal): Promise<void>
```
- `fetch('/api/events', { headers: { Authorization: `Bearer ${token}`, Accept: 'text/event-stream' }, signal })`.
- 401 → throw an `ApiError` so the global session-expiry path runs.
- Parse `response.body!.pipeThrough(new TextDecoderStream())`: accumulate into a buffer, split on
  `\n\n`; per block, read `event:` (default `message`) and concatenate all `data:` lines with
  `\n`; ignore lines starting with `:` (keep-alives) and unknown fields; tolerate `\r\n`.
- **No `EventSource`** and **no token in the URL** — a test greps for it.

### F3. `useEventStream`
- Connects when authenticated; aborts on logout/unmount.
- Backoff `1s, 2s, 5s, 10s, 30s` (reset on `ready`); after **3** consecutive failures set
  `mode = 'polling'` and refetch the current view every 15 s while continuing to retry the stream
  in the background; a successful `ready` returns to `mode = 'live'`.
- On `ready`: refetch the current view once (covers the reconnect gap).
- Event application in `App`:
  - ignore any event whose `origin === getClientId()`
  - `todo.created` / `todo.updated`: if the todo does not match the current list/filter selection,
    drop it and instead refetch the count; otherwise upsert by `id` and re-sort using the current
    sort. **Simpler and permitted alternative, which the implementer should prefer for
    correctness:** debounce 250 ms and refetch the current query — the todo lists are small. Pick
    the refetch approach and document it in a code comment.
  - `todo.deleted`: remove by `id`.
  - `list.*`: refetch `GET /api/lists`; on `list.deleted` of the selected list, fall back to the
    default list.

### F4. `ConnectionStatus`
A small, unobtrusive indicator in the header: `mode === 'live'` → nothing rendered (or a visually
hidden `Live updates on`); `mode === 'polling'` → text `Live updates paused — refreshing every 15
seconds`, `role="status"`, `aria-live="polite"`. **Never** an error dialog; realtime is an
enhancement.

### F5. AI UI — exact copy
The whole AI section is hidden when `GET /api/ai/status` returns `enabled: false`, and rendered
**disabled with an explanation** when `enabled: true, available: false`.

| Element | Copy / accessible name |
|---|---|
| AI section heading (`h2`) | `AI helpers` |
| Unavailable banner (`role="status"`) | `AI features are unavailable right now. Everything else works as usual.` |
| **AiAddBox** textarea label | `Describe a todo in your own words` (placeholder `e.g. call the dentist tomorrow, urgent`) |
| AiAddBox submit | `Draft with AI` (busy: `Thinking…`) |
| AiAddBox hint | `This can take up to a minute on a slow machine.` |
| **AiDraftPreview** heading (`h3`) | `Suggested todo` |
| Draft fields | the normal editable Title / Description / Priority / Due date / Tags controls, prefilled |
| Draft subtasks | checkbox list, each labelled `Include subtask {title}`, all checked by default |
| Draft confirm | `Add this todo` · discard: `Discard suggestion` |
| **`Split into subtasks`** button on a todo | accessible name `Split {title} into subtasks` |
| Subtask suggestion list | checkboxes `Add subtask {title}` (all checked), buttons `Add selected subtasks` / `Cancel` |
| No suggestions returned | `The AI had no subtasks to suggest.` |
| **`Suggest priority & tags`** button | accessible name `Suggest priority and tags for {title}` |
| Metadata suggestion | `Suggested priority: {Level}. Suggested tags: {a, b}.` with buttons `Apply suggestion` / `Dismiss` |
| **DailySummaryPanel** heading (`h2`) | `Today at a glance` |
| Generate button | `Generate summary` (busy: `Writing your summary…`) |
| Summary body | plain text in a `<p>`, never `dangerouslySetInnerHTML` |
| Summary meta | `Based on {n} open todos · generated {HH:MM}` |
| Empty input | `Nothing to summarise — you have no open todos.` |
| AI error (503/504) | `AI is unavailable right now. Please try again later.` |
| AI rate limited (429) | `Too many AI requests. Please wait a moment.` |
| AI timeout (frontend abort at 60 s) | `That took too long. Please try again.` |

Behaviour rules:
- **Every AI result is a draft the user confirms** (master D-AI1). Applying a draft uses the
  ordinary endpoints: `POST /api/todos` (with the checked subtasks created via
  `POST /api/todos/{id}/subtasks` afterwards), `POST /api/todos/{id}/subtasks`, or
  `PATCH /api/todos/{id}` for the metadata suggestion.
- All AI calls use an `AbortController` with a 60 s timeout and are cancelled on unmount/discard.
- AI buttons are never the only way to do something: the manual add form, the edit panel and the
  subtask form remain fully functional and visible at all times.
- Accessibility: each AI panel is a `<section>` with an accessible name; results are announced via
  `aria-live="polite"`; focus moves to the draft's Title field when a draft appears and back to the
  triggering button when it is discarded; busy buttons use `aria-busy` (never `disabled`, per C7).

### F6. Vitest tests
1. SSE parser unit tests: multi-line `data:`, comment lines, `\r\n`, a payload split across two
   chunks, an unknown event name.
2. `useEventStream`: a `todo.created` from another origin updates the list; one with **our**
   `client-id` is ignored; `todo.deleted` removes the row; three failed connects switch to
   polling and the polling copy appears; a `ready` after reconnect triggers exactly one refetch.
3. A 401 on the stream triggers the session-expiry flow.
4. `ai/status` `enabled:false` → no AI section rendered at all; `available:false` → the section
   renders with the unavailable banner and the AI buttons inert.
5. AiAddBox: submitting calls `parseTodo` with the text and the local `today`; the draft preview
   prefills every field; `Add this todo` calls `createTodo` with the edited values and creates the
   checked subtasks; unchecking a subtask excludes it; `Discard suggestion` restores focus.
6. `Split {title} into subtasks`: suggestions render as checkboxes, `Add selected subtasks` calls
   the subtask endpoint once per checked item, and an empty suggestion list shows the no-subtasks copy.
7. `Suggest priority and tags`: `Apply suggestion` issues one `PATCH` with exactly `priority` and
   `tags`; `Dismiss` issues none.
8. Daily summary: renders the text and the meta line; the no-todos case shows the empty copy.
9. AI errors: 503 → unavailable copy, 429 → rate-limit copy, an aborted request → timeout copy;
   in each case the manual controls remain enabled.

### F7. Playwright
- `e2e/realtime.spec.ts` — **two browser contexts signed in as the same user**: tab A adds a todo,
  tab B shows it without reloading (`await expect(...).toBeVisible({ timeout: 10_000 })`); tab A
  completes it, tab B's checkbox becomes checked; tab A deletes it, the row disappears from tab B.
- `e2e/ai.spec.ts` — runs **only when `E2E_AI=1`** (the AI stack is not part of the default E2E
  environment); drafts a todo from natural language and confirms it. Otherwise the spec is skipped
  with a clear reason.
- Both with `PLAYWRIGHT_BROWSERS_PATH=node_modules/.cache/ms-playwright`.

---

## DEVOPS — Ollama, GPU override, AI compose wiring

### D1. `ai-agent/Dockerfile` + `.dockerignore`
Same pattern as the backend: `python:3.12-slim`, uv copied from `ghcr.io/astral-sh/uv:latest`,
`uv sync --locked --no-dev`, non-root user, `EXPOSE 8000`,
`CMD ["uv","run","uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`.

### D2. `docker-compose.yml` additions (all under `profiles: ["ai"]`)
```yaml
  ai-agent:
    build: ./ai-agent
    profiles: ["ai"]
    ports: ["${AI_AGENT_HOST_PORT:-8020}:8000"]
    environment:
      OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://ollama:11434}
      OLLAMA_MODEL: ${OLLAMA_MODEL:-qwen2.5:3b}
      OLLAMA_TIMEOUT_SECONDS: ${OLLAMA_TIMEOUT_SECONDS:-45}
      AI_AGENT_TOKEN: ${AI_AGENT_TOKEN:?set AI_AGENT_TOKEN in .env}
    depends_on: { ollama: { condition: service_started } }
    healthcheck: ["CMD","python","-c","import urllib.request;urllib.request.urlopen('http://localhost:8000/health')"]
    restart: unless-stopped

  ollama:
    image: ollama/ollama:latest
    profiles: ["ai"]
    volumes: [ollama_models:/root/.ollama]
    # ports: ["11435:11434"]   # debug only — host 11434 belongs to another project
    healthcheck: ["CMD","ollama","list"]
    restart: unless-stopped

  ollama-pull:
    image: ollama/ollama:latest
    profiles: ["ai"]
    environment: { OLLAMA_HOST: "http://ollama:11434", OLLAMA_MODEL: "${OLLAMA_MODEL:-qwen2.5:3b}" }
    entrypoint: ["/bin/sh","-c","ollama pull \"$$OLLAMA_MODEL\""]
    depends_on: { ollama: { condition: service_healthy } }
    restart: "no"
```
`backend` gains `AI_ENABLED`, `AI_AGENT_URL`, `AI_AGENT_TOKEN`, `AI_TIMEOUT_SECONDS`.
**`ai-agent` must not depend on `ollama-pull` completing** — the stack comes up regardless.
`volumes:` gains `ollama_models`.

### D3. `docker-compose.gpu.yml`
Exactly the override in master §1.2 (only the `ollama` service, only the `deploy.resources`
block). Add a header comment with the run command and a note that it requires the `nvidia`
runtime and that `qwen2.5:3b` fits a 6 GB card.

### D4. nginx
Confirm (and test) the SSE settings from slice 1 (`proxy_buffering off`, `proxy_http_version 1.1`,
`proxy_set_header Connection ""`, `proxy_read_timeout 3600s`). If `/api/events` buffers behind
nginx, that is a devops bug, not a backend bug.

### D5. `.env.example`, README, CI
- `.env.example`: append the AI block from master §9.2 with placeholder secrets only.
- README: a **Realtime** section (SSE, single-replica limitation, the polling fallback) and an
  **AI features** section — `docker compose --profile ai up -d --build`, first-run model pull time
  and `docker compose --profile ai run --rm ollama-pull` to retry it, `OLLAMA_MODEL` alternatives,
  the GPU command, the host-11434 warning, `AI_ENABLED=false` to turn everything off, and the
  explicit statement that **the app is fully usable without the AI profile**.
- CI: add an `ai-agent` job (setup-uv, `uv sync --locked`, `uv run pytest` in `ai-agent/`) with
  `AI_AGENT_TOKEN` set to a test-only literal and `AI_AGENT_LIVE` unset. No Ollama in CI.

---

## QA — complexity **intermediate** (sonnet)
1. `uv run pytest` in `backend/` (both lanes) and in `ai-agent/`; `npm test`; `npm run build`.
2. All E2E specs including `realtime.spec.ts` (two contexts).
3. **Realtime by hand**: two browser tabs, same account — add / complete / edit / delete / move
   between lists / create and delete a list; every change appears in the other tab within ~2 s and
   the originating tab shows no duplicate row. Leave a tab idle for 2 minutes and confirm the
   stream is still alive (keep-alives) and still receives events.
4. **Degradation matrix**, each verified in the browser:
   - `docker compose up -d` (no `--profile ai`) → no AI section, everything else works.
   - `AI_ENABLED=false` → no AI section.
   - AI profile up but `docker compose stop ollama` → AI section shows the unavailable banner and
     AI actions fail with the unavailable copy; manual todo management is unaffected.
   - Model not pulled → same as above.
   - Backend stopped → the SSE stream drops, the UI switches to the polling copy, and recovers
     when the backend returns.
5. `docker compose --profile ai up -d --build`, wait for `ollama-pull` to finish, then exercise all
   four AI features end to end and confirm **no AI action writes anything until the user confirms**
   (check the DB / the list before confirming).
6. Optional GPU check if the machine allows:
   `docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai up -d` and confirm
   `nvidia-smi` inside the ollama container sees the GPU; report the latency difference.
7. Security spot-checks: no token in any URL (`/api/events` has no query string), `X-Internal-Token`
   never reaches the browser, `ai-agent` returns 401 without the token, no AI output is rendered as
   HTML.

---

## Acceptance criteria
1. `GET /api/events` streams `text/event-stream`, requires a bearer token (401 otherwise), sends a
   `ready` frame, keep-alive comments while idle, and never accepts a token via the URL.
2. Two tabs signed into the same account see each other's creates, updates, deletes and list
   changes within a few seconds, with no page reload and no duplicated rows in the originating tab.
3. A subtask change delivers a `todo.updated` event carrying the complete top-level parent.
4. Events are scoped per user: a second account never receives another user's events.
5. A handler that fails after the DB write publishes no event; a slow/stalled subscriber is dropped
   without affecting others or growing memory.
6. Losing the stream three times switches the UI to 15-second polling with the documented
   `role="status"` copy and recovers automatically; the user is never shown an error dialog.
7. `GET /api/ai/status` always returns 200 and correctly reports `enabled` / `available` / `model`.
8. All four AI endpoints work against a running `ai-agent` + Ollama and **never write to the
   database**; the user explicitly confirms every draft before it is persisted.
9. With Ollama stopped, the model missing, `ai-agent` down, or `AI_ENABLED=false`, the app remains
   fully usable: AI controls are hidden or clearly marked unavailable and every manual feature
   works.
10. AI failures surface as 503 `ai_unavailable`, 503 `ai_disabled`, 504 `ai_timeout` or 429
    `rate_limited`, each mapped to the exact UI copy in §F5.
11. `ai-agent` rejects any `/ai/*` request without a valid `X-Internal-Token` (401) and requires
    `AI_AGENT_TOKEN` at startup; `/health` works without it.
12. `ai-agent` validates and sanitizes every model response: oversized titles, invalid priorities,
    unparseable or absurd dates, malformed tags, excess subtasks and overlong summaries are all
    clamped, with unit tests proving it.
13. Invalid model JSON triggers exactly one repair retry and then a 503 `ai_invalid_response`.
14. `docker compose up -d` (no profile) starts db + backend + frontend only; `docker compose
    --profile ai up -d` additionally starts `ai-agent`, `ollama` and the one-shot `ollama-pull`;
    Ollama is not published on the host; the stack starts even if the model pull fails.
15. `docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile ai up -d` grants the
    GPU to Ollama; the default compose file runs on CPU.
16. `OLLAMA_MODEL` selects the model and defaults to `qwen2.5:3b`.
17. CI runs the `ai-agent` test suite with mocked Ollama; the live test is skipped unless
    `AI_AGENT_LIVE=1`.
18. `README.md` documents realtime (including the single-replica limitation), the AI profile, the
    model pull, the GPU override, the host-port warnings, and how to disable AI entirely.
