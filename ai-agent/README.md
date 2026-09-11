# `ai-agent` — internal AI microservice

A small FastAPI service that turns a prompt into **validated, clamped JSON** by
talking to an [Ollama](https://ollama.com) model over HTTP. It is the only
component that knows about prompts, JSON schemas and model quirks.

It is deliberately dumb about the rest of the app:

- **no database** — it never reads or writes todos; the backend assembles the
  prompt context and passes it in;
- **no user auth** — it never sees a user or a JWT; only the backend calls it
  (master spec decision **D-AI2**), authenticated by a shared secret;
- **no writes** — every endpoint returns a *draft* that the user reviews and
  confirms through the ordinary todo API (**D-AI1**).

In compose it lives behind the `ai` profile: `docker compose --profile ai up -d`.

## Internal auth

Every `/ai/*` request must carry the shared secret `X-Internal-Token:
$AI_AGENT_TOKEN`; `/health` does not. It is compared with
`hmac.compare_digest`, so a wrong token cannot be discovered byte by byte, and
the token is never logged or echoed in a response.

This is **defence in depth, not the only defence**. The service is also not
published on the host, so it is reachable only from the backend on the private
compose network; the token is what stops anything that *does* get onto that
network from spending your CPU on the model.

`AI_AGENT_TOKEN` is required at startup, must be **at least 32 characters**, and
must not look like a placeholder. Generate a real one:

```bash
openssl rand -hex 32
```

The length floor alone is not enough — the shipped placeholder
`change-me-internal-shared-secret` is *exactly* 32 characters. Since iteration 4
the guard (`app/config.py::is_placeholder_secret`) matches a table of exact
literals, prefixes (`change-me`, `replace-me`, `your-`, `example`,
`placeholder`, `dummy`, `sample`, …) and substrings (`change-me`,
`your-secret`, `do-not-use`, …), casefolded and whitespace-trimmed, so
`Prod-Change-Me-Please` and `change-me-internal-shared-secret-a1b2c3` are
rejected too. A public secret that looks like protection is worse than none.

That table is duplicated **verbatim** in `backend/app/config.py` — the two are
separate `uv` projects with no shared package (decision D-IT4-3). Both test
suites assert the *same* corpus of placeholder and real-shaped values
(`tests/test_config.py`), so if one copy drifts a test fails rather than a
deployment. Edit both files or neither.

Startup errors never echo the rejected value (`hide_input_in_errors`): a
failure message goes straight into the container log, and on a near miss the
rejected value is a real secret.

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Is Ollama up, and has the model been pulled? |
| GET | `/ai/ping` | `X-Internal-Token` | Do we agree about `AI_AGENT_TOKEN`? |
| POST | `/ai/parse-todo` | `X-Internal-Token` | free text → one todo draft |
| POST | `/ai/suggest-subtasks` | `X-Internal-Token` | one todo → ordered steps |
| POST | `/ai/suggest-metadata` | `X-Internal-Token` | one todo → priority + tags |
| POST | `/ai/daily-summary` | `X-Internal-Token` | open todos → a short briefing |
| POST | `/ai/edit-todo` | `X-Internal-Token` | one todo + one instruction → a change proposal |

Request/response shapes are frozen in `docs/specs/iteration-2-master.md` §8.2;
`/ai/edit-todo` (iteration 5) in
`docs/specs/iteration-5-ai-edit-by-instruction.md` §2.2.
`/health` **never fails** — it reports, so the backend's `GET /api/ai/status`
can drive the UI:

```json
{"status": "ok", "ollama": "ok", "model": "qwen2.5:3b", "model_present": true}
```

`GET /ai/ping` (iteration 4) answers `{"status": "ok"}` and nothing else. It
makes no Ollama call, so the backend can probe it on every status refresh. Its
only job is the *status code*: because it sits behind the shared secret while
`/health` deliberately does not, the pair tells "ai-agent is unreachable"
(`/health` fails) apart from "ai-agent is up but we disagree about
`AI_AGENT_TOKEN`" (`/health` fine, `/ai/ping` → 401). Before it existed a token
mismatch looked like a healthy service and the UI offered AI buttons that were
certain to fail.

### Contract — request limits BACKEND-A must respect

Request models use `extra="forbid"` (master D-E3) and every string is bounded.
**Exceeding any of these is a 422, which the backend proxy does not map to a
user-facing AI error** — so the backend must clamp or truncate before calling,
not pass user input straight through.

| Endpoint | Field | Limit |
|---|---|---|
| `parse-todo` | `text` | 1..**4000** characters |
| `parse-todo`, `suggest-metadata` | `known_tags` | ≤ 50 items, ≤ 40 chars each |
| `suggest-*` | `title` | 1..200 characters |
| `suggest-*` | `description` | ≤ 2000 characters |
| `suggest-subtasks` | `max_items` | 1..10 (default 5) |
| `daily-summary` | `todos` | ≤ 50 items |
| `daily-summary` | `todos[].title` | 1..200 characters |
| `daily-summary` | `todos[].priority` | ≤ 10 characters |
| `daily-summary` | `todos[].list_name` | ≤ 100 characters |
| `edit-todo` | `instruction` | 1..**500** characters |
| `edit-todo` | `todo.tags` | ≤ 10 items, tag-charset constrained |
| `edit-todo` | `todo.subtasks` | ≤ **20** items, `{title, completed}` — **no ids** |
| *(any)* | whole body | ≤ 64 KiB, else **413** `request_too_large` |

The `text` cap is this service's own choice, not a master-spec figure: an
unbounded prompt is a cost and latency vector, and 4000 characters is far more
than the "describe a todo in your own words" box is meant to carry. The backend
should truncate a longer note rather than forward it.

### Errors

The backend's error contract v2 (master §5): a human `detail` plus a machine
`code`. Request-validation failures keep FastAPI's default 422 body and carry no
`code`.

| Status | `code` | When |
|---|---|---|
| 401 | `unauthorized` | missing or wrong `X-Internal-Token` |
| 422 | *(none)* | the request body failed validation |
| 503 | `ai_unavailable` | Ollama unreachable, 5xx, or the model is not pulled |
| 503 | `ai_invalid_response` | the model produced unusable output twice |
| 504 | `ai_timeout` | Ollama exceeded `OLLAMA_TIMEOUT_SECONDS` |

## How a request is served

1. Validate the request (`extra="forbid"`).
2. Build a short user message from `app/prompts.py` (`TODAY`, `TOMORROW`,
   `KNOWN_TAGS`, the todo text).
3. `POST {OLLAMA_BASE_URL}/api/chat` with `stream: false`, `temperature 0.2`, and
   the endpoint's **JSON Schema in `format`** (Ollama structured outputs). If the
   server rejects a schema `format`, retry once with `"format": "json"`.
4. Validate the reply with a permissive Pydantic model (`extra="ignore"` — small
   models add keys, and that alone is not an error).
5. On a validation failure, retry **once**, feeding the validator errors back.
6. On a second failure → 503 `ai_invalid_response`.
7. **Sanitize** (`app/sanitize.py`) — the mandatory "never trust the model" layer:
   titles trimmed/truncated to 200, descriptions to 2000, unknown priorities
   coerced to `medium`, unparseable or absurd due dates dropped, tags normalized
   to the master §3.1 rules and capped at 5, subtasks deduped and capped, the
   summary truncated to 800 characters on a word boundary.

Steps 4-6 make bad output *rare*; step 7 makes bad output *harmless*. Both are
required — the prompt is never the last line of defence.

### `/ai/edit-todo` clamps in the other direction

The four drafting endpoints answer "what should this todo be?", so coercing an
unusable value into a sane default is right: a draft needs *a* priority.
`/ai/edit-todo` answers "what should change?", where the same coercion invents a
change nobody asked for and then puts it in front of the user for confirmation.
So on that endpoint an unusable value always means **no change**:
`clean_optional_priority` returns `None` where `clean_priority` returns
`medium`, only a real JSON boolean may flip `completed`, and a due date that is
not an ISO date is dropped rather than guessed at.

Two more properties are structural rather than clamped:

- **The model never sees or emits an identifier** (spec decision D-IT5-3). The
  backend sends subtasks as a positional list and the model answers with 1-based
  `index` values, which are bounds-checked here against the snapshot that was
  sent and again in the backend against the real list. A 3B model reliably
  copies one digit; it does not reliably copy 36 hex characters.
- **Out-of-scope edits are unrepresentable.** `EDIT_TODO_SCHEMA` has no field
  for deleting the todo, moving it to another list or creating other todos, so
  "delete this and add one about the car" can only be answered with the all-null
  object — which is a normal 200, not an error. An empty change set is a
  legitimate answer and is never `ai_invalid_response`.

### Known model limitations

Measured against `qwen2.5:3b`, the default:

- **Relative dates beyond "tomorrow" are unreliable.** `TODAY` and `TOMORROW`
  are precomputed into the prompt, and those resolve correctly; "next Friday"
  still lands on the wrong date fairly often. The sanitizer only rejects dates
  that fail to parse or fall outside a five-year window, so a plausible-but-wrong
  date reaches the user. That is acceptable because **every AI result is a draft
  the user reviews before it is saved** (D-AI1) — but do not build anything that
  trusts an AI due date unattended.
- Suggested tags are occasionally tangential. They are capped at 5 and normalized,
  and the user unchecks what they do not want.
- **On `/ai/edit-todo` it drifts towards restating the current values** — the
  todo's own description, priority or tags come back unchanged-but-present.
  Harmless by construction (the backend drops every no-op before the user sees
  the draft) and largely fixed by the prompt work above, but it is the first
  thing to look for if a change set ever renders noise.
- A bigger `OLLAMA_MODEL` (`qwen2.5:7b`, `llama3.1:8b`) improves all of the above
  at the cost of memory and latency.

## Configuration

No secret has a default. `AI_AGENT_TOKEN` is required, must be at least 32
characters, and must not be the `.env.example` placeholder — see
[Internal auth](#internal-auth). The service refuses to start otherwise.

| Var | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://ollama:11434` | compose-internal Ollama |
| `OLLAMA_MODEL` | `qwen2.5:3b` | any model Ollama can serve |
| `OLLAMA_TIMEOUT_SECONDS` | `45` | httpx *read* timeout |
| `AI_AGENT_TOKEN` | *(required)* | shared secret with the backend, >=32 chars |
| `LOG_LEVEL` | `info` | model output is only ever logged at `debug` |
| `APP_ENV` | `prod` | **only** `dev` serves `/docs`, `/redoc`, `/openapi.json` |

`APP_ENV` **fails closed**: it defaults to `prod`, so an unset, empty or
misspelled value hides the API docs rather than exposing them. Set `APP_ENV=dev`
explicitly — in compose or in your shell — when you want them.

The timeout ladder is `ai-agent → Ollama 45 s`, `backend → ai-agent 50 s`,
`browser → backend 60 s` (master §8.3), so an inner timeout always fires first.

## Running it standalone

```bash
cd ai-agent
uv sync
APP_ENV=dev AI_AGENT_TOKEN="$(openssl rand -hex 32)" \
    OLLAMA_BASE_URL=http://localhost:11435 \
    uv run uvicorn app.main:app --port 8020 --reload
```

Without `APP_ENV=dev` the service still works, but `/docs` returns 404.

> **Host port warning (master D-P2):** host port `11434` belongs to a *different*
> project's Ollama. Never point this service at it. Publish our container on
> `11435` instead.

## Tests

Ollama is always mocked, so the suite is green on a clean checkout with no
Docker, no network and no model:

```bash
cd ai-agent
uv run pytest          # 740 passed, 11 skipped (the three live lanes)
```

The live lane is opt-in and needs a real Ollama with the model pulled:

```bash
docker run -d --name todo-app-ollama-aitest \
    -v todo-app-ollama:/root/.ollama -p 127.0.0.1:11435:11434 ollama/ollama:latest
docker exec todo-app-ollama-aitest ollama pull qwen2.5:3b   # first run only

AI_AGENT_LIVE=1 OLLAMA_BASE_URL=http://localhost:11435 OLLAMA_MODEL=qwen2.5:3b \
    OLLAMA_TIMEOUT_SECONDS=240 AI_AGENT_TOKEN="$(openssl rand -hex 32)" \
    uv run pytest tests/test_live_ollama.py tests/test_live_dates.py \
                  tests/test_live_edit.py -v -s

docker rm -f todo-app-ollama-aitest   # never `docker volume rm todo-app-ollama`
```

CPU inference takes roughly 5-20 s per call on a 3B model, so raise
`OLLAMA_TIMEOUT_SECONDS` for the live lane. The live tests assert only that the
contract holds — never the model's wording, which would make them a coin flip.

### Relative dates

`test_live_dates.py` is the one test that measures model *quality*: five notes
with relative wording ("next Tuesday", "in 10 days", "the day after tomorrow",
"on friday", "next week") against a date that makes each answer deterministic.
It exists because backlog row IT3-8 was opened by an observation nothing could
re-check. Acceptance is the aggregate — **at least 4 of 5** — because a single
flip at temperature 0.2 is noise; the per-case table is printed either way.

The reason it passes is `prompts.date_anchors`: every relative expression the
app supports is precomputed into a labelled `DATES` block and the model is told
to *copy* a value, never to calculate one. Two details are load-bearing and
have tests of their own:

- one label per line (seven weekdays on one row made the model grab a
  neighbouring value);
- the block goes **last, after the note** — a 3B model attends most strongly to
  the end of its context. This alone took the lane from 3/5 to 5/5.

Current score on `qwen2.5:3b`: **5/5**. On a GPU host `OLLAMA_MODEL=qwen2.5:7b`
improves relative-date resolution further and is a deliberate manual opt-in
(decision D-IT4-5) — nothing switches models automatically.

### Edit by instruction

`test_live_edit.py` (iteration 5) is the second quality lane: three
instructions — a rename, an add-a-step, and an out-of-scope "delete this todo
and make one about the car" — against one fixed snapshot. It asserts
**structure only**: whether a rename produced a title at all, whether an add
produced an `add` operation with no index, whether the out-of-scope request
produced the empty change set. What the model *named* things is its business,
because the user reads and confirms the draft before anything is written.

Acceptance is **at least 2 of 3**, and a lower score is explicitly not a merge
blocker (spec §3.1 A6, risk R10) — it is recorded in `DASHBOARD.md` and feeds
backlog row IT5-2. Two assertions do run unconditionally, because failing them
would mean a bug in *this service* rather than a model opinion: the response
keeps the documented shape whichever `format` Ollama accepted, and no operation
names a subtask position outside the snapshot.

Current score on `qwen2.5:3b`: **3/3**, stable across two runs. It started at
**1/3**, and both fixes are worth knowing about before touching
`EDIT_TODO_SYSTEM`:

- **The three shared rules are drafting rules.** `_PRIORITY_RULE` ends with
  "otherwise use medium" and `_TAG_RULE` asks for "0-3 keywords describing the
  topic" — correct when *creating* a todo, actively harmful when editing one.
  Pasted in unqualified they made a bare "rename it to X" come back with a
  restated `priority: medium` and two invented tags. They are still reused
  verbatim (one definition of what a priority is) but each is now prefixed with
  "Only if the request asks to change the …".
- **The few-shot todo must be rendered in the same lines as the real one.** With
  the example todo as a compact `TODO={…}` object the model invented two subtask
  renames on a plain rename request (2/3); rendering it through the same
  `edit_todo_context()` the real request uses — so a worked `SUBTASKS=` line is
  visibly answered by `"subtasks": []` — took it to 3/3.

Model behaviour that the *code* answers rather than the prompt: asked only to
rename, the model at one point proposed re-adding both existing subtasks. Every
other restatement is a no-op the backend drops, but an `add` of an existing
title is a duplicate, so `sanitize.clean_edit_ops` drops adds whose title
already appears in the snapshot.

Open question §6.2 of the iteration-5 spec — whether Ollama accepts an `enum`
containing `null` — is **answered: it does** (`qwen2.5:3b`, Ollama 0.x, HTTP
200 with the schema in `format`). The `format: "json"` fallback was never
triggered; it remains as insurance.

## Layout

```
app/
├── main.py          app factory, lifespan httpx client, /health
├── config.py        pydantic-settings; AI_AGENT_TOKEN required, >=32 chars,
│                    placeholder table (kept in sync with the backend's)
├── auth.py          X-Internal-Token, compared with hmac.compare_digest
├── deps.py          the shared OllamaClient + app-settings dependencies
├── errors.py        the {detail, code} error contract + handlers
├── middleware.py    64 KiB request body limit (counts streamed bytes)
├── ollama.py        /api/chat + /api/tags, normalized failures
├── prompts.py       five system prompts, five JSON schemas, user templates,
│                    date_anchors() — the DATES block (goes last, see above)
├── sanitize.py      the clamping layer (pure functions, unit-tested)
├── schemas.py       request / raw-model / response models
└── routers/ai.py    the five endpoints, /ai/ping, and the repair-retry pipeline
```

`Dockerfile` and `.dockerignore` are owned by the devops agent.
