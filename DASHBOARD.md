# DASHBOARD — internal Jira for Todo App

Single source of truth for project status. Rules:
- Each iteration is an epic; inside it, tasks carry a status: `TODO` · `IN PROGRESS` · `REVIEW` · `QA` · `DONE` · `BLOCKED`.
- The orchestrator updates this file at iteration start and close, at every slice merge to `main`, and whenever a blocker appears.
- Time measurement: start = the user's instruction, end = merged to `main` plus a running preview. Session-limit pauses are subtracted and listed separately.
- Backlog items live in the "Backlog" section, tagged with the iteration meant to pick them up.

Owner legend: `lead` (orchestrator, Fable) · `planner` (Opus) · `backend` / `frontend` / `devops` (Opus/Sonnet/Haiku by complexity) · `reviewer` + `security` (Opus) · `qa` (Sonnet).

---

## Current run state (session-limit resilience)

This section is the only place a fresh session (after a usage limit, a restart, or lost
context) reads to reconstruct what was in flight. The orchestrator updates it **at every
agent dispatch and at every report received**, committing as `dashboard: <what changed>` on `main`.

### Active iteration

| | |
|---|---|
| Iteration | — (none in progress; iteration 5 closed) |
| Start (UTC) | — |
| Integration branch | — |
| Last stable point | `main` @ `c352752` |

### Time and pause log

| Event | UTC | Notes |
|---|---|---|
| start | 2026-09-03T07:40Z | Iteration 3 scope 1: UI clarity redesign. Entries: `start`, `pause-start`, `pause-end`, `slice-merged`, `end`. Net time = end − start − Σ pauses. |
| slice-merged | 2026-09-03T08:35Z | IT3-9 merged to `main` (`54a18d8`), preview frontend rebuilt. |
| end | 2026-09-03T08:35Z | Iteration 3 closed. Net 55 min, no pauses. |
| start | 2026-09-03T09:49:29Z | Iteration 4: backlog sweep. |
| blocked | 2026-09-03T10:5xZ | Root filesystem full (0 B); QA re-run in the main tree instead of a worktree; no time deducted (reviews and fixes continued). |
| end | 2026-09-03T11:25:28Z | Iteration 4 closed: merged `ec60fac`, preview rebuilt. Wall 1 h 36 min, no session-limit pauses. |
| start | 2026-09-10T10:21:48Z | Iteration 5: AI edit-by-instruction. Planner dispatched. |
| dispatch | 2026-09-10T10:33Z | Spec committed `c61f69f`; slices A (ai-agent), B (backend), C (frontend) dispatched in parallel worktrees, all opus. |
| gates | 2026-09-10T11:08Z | All three slices reported (C 10:47, B 10:56, A 11:06); merged into `integrate/iteration-5` @ `b4dc3f8`; reviewer + security dispatched. |
| review-1 | 2026-09-10T11:20Z | Security APPROVE (11:14), reviewer CHANGES REQUESTED (11:19). Fix round 1 dispatched to frontend and backend; master-spec delta applied by the lead. |
| gates-2 | 2026-09-10T11:32Z | Fixes merged (`1b0f3ab`, `cde10f2`). Reviewer round 2 and QA dispatched in parallel. |
| end | 2026-09-10T11:55:15Z | Iteration 5 closed: reviewer round 2 APPROVE (11:37), QA PASS (11:50), merged `c352752`, worktrees removed, preview rebuilt with the `ai` profile. Wall 1 h 33 min 27 s, no session-limit pauses. |

### Agents in flight

| Role | Model | Worktree / branch | Task (one line) | Last commit | Next step | State |
|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — |

States: `dispatched` · `working` · `reported` · `cut` (killed by a usage limit) · `merged`.
After an agent reports, its row becomes `reported` with a one-line result; after its branch
is merged it becomes `merged`, and once the worktree is removed the row is deleted.

### Recovery procedure after a cut

1. Read this section and the "Agents in flight" table.
2. `git worktree list` and `git log --all --oneline --graph | head -80`: compare with the table; missing commits mean lost work.
3. In every worktree marked `cut`: `git status --short` (uncommitted files are the agent's last step) and `git log -3`.
4. `docker ps -a | grep todo-app` and `ss -ltn`: orphaned containers and ports left by QA or reviewers; remove only ours (`todo-app-*`, `*pgtest*`, `*probe*`).
5. If the agent can be resumed by message, resume it pointing at its last commit and the "Next step" column. Otherwise dispatch a fresh agent with the same brief plus the branch and last commit.
6. Append `pause-end` to the time log.

### WIP-commit rule for agents

Before any step that may take more than a few minutes (full test suite, Docker build,
Postgres lane, E2E), the agent commits the current state as `<agent>: wip <what is in progress>`
on its branch. `wip` commits are allowed and need no green tests; the next commit describes
the outcome. A cut in the middle of a long step then loses at most that one step.

---

## Iteration 1 — Todo core · `DONE`

| | |
|---|---|
| Status | **DONE** · merged to `main` 2026-09-02 (`acca29d`) |
| Net time | **30 min 4 s** (no pauses) |
| Scope | Add / Remove / Mark completed-active / Display all · Python backend · any frontend |
| Stack | FastAPI (in-memory) · React 19 + TS + Vite + Tailwind v4 · pytest / Vitest / Playwright |
| Tests on main | 27 pytest · 15 Vitest · 2 Playwright |
| Report | `docs/reports/iteration-1-history.html` |
| Spec | `docs/specs/iteration-1-todo-core.md` |

| ID | Task | Owner | Status |
|---|---|---|---|
| IT1-1 | Project bootstrap (interview, CLAUDE.md, settings) | lead | DONE |
| IT1-2 | Spec with a frozen API contract | planner | DONE |
| IT1-3 | FastAPI backend, in-memory repository, 22 tests | backend | DONE |
| IT1-4 | React frontend, components, 14 tests, E2E | frontend | DONE |
| IT1-5 | README, .gitignore, GitHub Actions CI | devops | DONE |
| IT1-6 | Two review rounds + security audit | reviewer, security | DONE |
| IT1-7 | QA: 26/26 acceptance criteria, keyboard-only E2E | qa | DONE |

Key gate findings: an E2E locator matched two elements (blocker); focus did not return to the input after adding a todo (a real bug, caught by a test the reviewer requested).

---

## Iteration 2 — persistence, users, enrichment, realtime, AI · `DONE`

| | |
|---|---|
| Status | **DONE** · merged to `main` 2026-09-03 (`c7892db`) |
| Wall-clock | 15 h 07 min (2026-09-02 14:33 → 2026-09-03 05:40 CEST) |
| Session-limit pauses | 9 h 27 min (3 pauses: 33 min, 3 h 22 min, 5 h 32 min) |
| Net time | **5 h 40 min** |
| Commits | 234 (187 work commits + 47 merges) |
| Tests on main | 700 pytest SQLite · 728 pytest PostgreSQL · 211 ai-agent · 186 Vitest · 13 Playwright (1,138 total) |
| Report | `docs/reports/iteration-2-history.html` |
| Specs | `docs/specs/iteration-2-master.md` + `iteration-2-slice-{1,2,3,4}-*.md` |

### Slices

| ID | Slice | Scope | Net time | Status |
|---|---|---|---|---|
| IT2-S1 | Persistence + Docker | PostgreSQL, async SQLAlchemy, Alembic (one baseline migration), docker-compose db/backend/frontend, nginx, CI with a Postgres lane | ~1 h 15 min | DONE |
| IT2-S2 | Accounts + lists | JWT HS256, argon2id, register/login, per-user isolation, multiple lists, attempt limits | ~1 h 15 min | DONE |
| IT2-S3 | Enrichment + filters | priority, due date, description, tags (≤10), subtasks (one level), filter/sort/search, `X-Total-Count`, partial PATCH | ~1 h | DONE |
| IT2-S4 | Realtime + AI | SSE over `fetch`, per-user broker, `ai-agent` microservice + Ollama `qwen2.5:3b` in compose (`ai` profile, GPU override), four AI features as drafts to confirm | ~2 h | DONE |

### Tasks

| ID | Task | Owner | Status |
|---|---|---|---|
| IT2-1 | Master spec + 4 slice specs (2,725 lines) | planner | DONE |
| IT2-2 | `ai-agent` microservice (in parallel with slice 1) + live-model test lane | backend | DONE |
| IT2-3 | Slice 1 implementation + 3 review rounds + QA with container restart | backend, devops, frontend, reviewer, security, qa | DONE |
| IT2-4 | Slice 2 implementation + QA (3× E2E after the commit-before-response fix) | same | DONE |
| IT2-5 | Slice 3 implementation + QA | same | DONE |
| IT2-6 | Slice 4 implementation + 3 gate rounds + QA with the `ai` profile in a separate compose project | same | DONE |
| IT2-7 | Internal ai-agent secret: removed on request, then restored and hardened (≥32 chars, placeholder rejection) | backend, devops | DONE |
| IT2-8 | Preview via `docker compose --profile ai` + demo account | lead | DONE |

### Critical bugs found by the gates (all fixed)

| ID | Description | Found by |
|---|---|---|
| IT2-B1 | `CORS_ORIGINS` could not be decoded from the environment → the backend container never started | qa, reviewer |
| IT2-B2 | Get-or-create race for user/list → 500s on a cold database | reviewer |
| IT2-B3 | Database commit after the HTTP response was sent → random 401 after register, stale counts | qa |
| IT2-B4 | Unbounded repetition of `tag=` → 250× slower query, 500 at 1,000 repeats | security |
| IT2-B5 | SSE stream outlived the token; no per-user stream cap | security |
| IT2-B6 | The example `JWT_SECRET` from the repo passed length validation → forgeable tokens | security |
| IT2-B7 | A fixed Docker network name joined two compose projects on one host | qa |

---

## Iteration 3 — UI clarity redesign · `DONE`

| | |
|---|---|
| Status | **DONE** · merged to `main` 2026-09-03 (`54a18d8`), closed at the user's request |
| Net time | **55 min** (07:40 → 08:35 UTC, no pauses) |
| Scope | IT3-9 only: the user's request "everything blends together" → two-column shell, sidebar with lists and filters, composer with Add / Draft-with-AI tabs, todo cards, AI menu, collapsed daily summary |
| Commits | 20 (13 frontend + 7 dashboard; 1 merge) |
| Tests on main | 700 pytest SQLite · 211 ai-agent · 208 Vitest (+22) · 13 Playwright |
| Report | `docs/reports/iteration-3-history.html` |

| ID | Task | Owner | Status |
|---|---|---|---|
| IT3-9 | UI clarity redesign (see backlog row below for the details) | frontend, reviewer, qa | DONE |

Gate findings: post-delete refocus effect stole focus on event-driven list changes (blocker, fixed with a one-shot ref + regression test); the new MenuButton shipped without keyboard-contract tests (blocker, 11 tests added, two defects fixed: Tab handling, ArrowDown/Up on the trigger); four lows fixed (unavailable-AI reason in the summary card, dead button size overrides, WCAG 2.5.3 menu names, header truncation). QA: E2E 12/12 twice, keyboard walk clean, 390 px without overflow.

---

## Iteration 4 — backlog sweep · `DONE`

| | |
|---|---|
| Status | **DONE** · merged to `main` 2026-09-03 (`ec60fac`) |
| Net time | **1 h 36 min** (09:49 → 11:25 UTC, no session-limit pauses; a full root filesystem cost a QA restart but no idle time) |
| Scope | Backlog rows IT3-1..4, IT3-6..8, IT4-1, IT4-2 (IT3-5 needs the user) |
| Commits | 45 (35 work commits: 15 backend, 7 frontend, 3 devops, 1 qa, 1 planner, 8 dashboard; 10 merges) |
| Tests on main | 927 pytest SQLite · 942 pytest PostgreSQL · 553 ai-agent · 247 Vitest · 14 Playwright (incl. `ai-menu.spec.ts` on the live model) |
| Report | `docs/reports/iteration-4-history.html` |
| Spec | `docs/specs/iteration-4-backlog-sweep.md` (+ iteration-4 deltas in `iteration-2-master.md`) |
| Estimate vs actual | planner estimated 3.5–4.5 h wall-clock; actual 1 h 36 min |
| Estimation rule | ~1 h net per medium slice (implementation + 2 review rounds + QA), 2 h with a new technology, +15–20 min planning |
| Wall-clock risk | usage limits (they added 9.5 h in iteration 2) |

### Backlog outcome

| ID | Task | Source | Weight | Status |
|---|---|---|---|---|
| IT3-1 | `tag.*` events in the SSE contract: tag rename/delete visible in other tabs without a reload | reviewer S4 | medium | DONE |
| IT3-2 | `/api/ai/status` detects a secret mismatch via `/ai/ping`; new `reason` field | qa S4 | low | DONE |
| IT3-3 | Placeholder guard widened (tables byte-identical in both services, `hide_input_in_errors`) | security S4 | low | DONE |
| IT3-4 | `REALTIME_BACKEND=memory` (only value), startup notice, README scaling note | planner risks | low | DONE |
| IT3-5 | Add an `origin` remote and run GitHub Actions CI for the first time (never verified remotely) | lead | medium | NEEDS USER (remote URL + credentials) |
| IT3-6 | Ollama image digest-pinned via one YAML anchor, GPU troubleshooting section | devops S4 | low | DONE |
| IT3-7 | Focus after deleting a todo: next row → previous → composer, with three guards | qa S1 | low | DONE |
| IT3-8 | Relative-date anchors in the prompt; live lane 5/5 (bar 4); QA's ad-hoc check still saw "in 10 days" off by 3 | ai-agent live lane | low | DONE (partial accuracy, see IT5-2) |
| IT3-9 | (done in iteration 3) UI clarity redesign: sidebar (lists + filters), composer with Add / Draft-with-AI tabs, todo cards with priority pill/due/tags/subtask progress, action group with AI menu, collapsed daily summary | frontend, reviewer, qa | high | DONE |
| IT4-1 | Composer keeps state across tab switches (both panels mounted, inactive hidden) | reviewer IT3-9 | low | DONE |
| IT4-2 | `frontend/e2e/ai-menu.spec.ts` against the live model, passed twice | qa IT3-9 | low | DONE |

Gate findings: review approve (outbox race, ordering, isolation, status invariant, focus guards verified); security approve (lows fixed: `xxx` prefix dropped, `TagName` charset, guard docstrings aligned); QA found that INFO logs never reached the container log (fixed: `LOG_LEVEL` + `configure_logging`).

---

## Iteration 5 — AI edit-by-instruction · `DONE`

| | |
|---|---|
| Status | **DONE** · merged to `main` 2026-09-10 (`c352752`) |
| Net time | **1 h 33 min 27 s** (10:21:48 → 11:55:15 UTC, no session-limit pauses) |
| Scope | User request: on every todo, in its AI menu, a free-text instruction ("rename it", "add a subtask X", "remove subtask Y", "tag it work") that the AI turns into a modification of that todo; sensible edits only, applied as a draft the user confirms |
| Commits | 43 (37 work commits: 17 backend, 8 frontend, 2 qa, 1 planner, 1 docs, 8 dashboard; 6 merges); 29 files, +7,329 / −37 lines |
| Tests on main | 1,009 pytest SQLite · 1,038 pytest PostgreSQL · 740 ai-agent · 296 Vitest · 18 Playwright (13 + 5 live-model: `ai-menu`, `ai-edit`) |
| Live lane | `test_live_edit.py` 3/3 on `qwen2.5:3b` (bar 2/3), stable across two runs; `test_live_dates.py` still 5/5 |
| Report | `docs/reports/iteration-5-history.html` |
| Spec | `docs/specs/iteration-5-ai-edit-by-instruction.md` (+ §6.6 / §8.2 rows in `iteration-2-master.md`) |
| Estimate vs actual | planner estimated 2 h 15 min – 3 h wall-clock; actual 1 h 33 min |

| ID | Task | Owner | Status |
|---|---|---|---|
| IT5-6 | Spec + subtask breakdown (`c61f69f`) | planner | DONE |
| IT5-7A | ai-agent `POST /ai/edit-todo`: prompt + few-shot, `EDIT_TODO_SCHEMA`, sanitizers, 71 endpoint tests, live lane | backend | DONE |
| IT5-7B | backend `POST /api/ai/edit-todo`: proxy under `AI_GUARDS`, positional snapshot, resolution into the change set (index→id, no-op suppression, tag cap, op ordering), tests on both lanes | backend | DONE |
| IT5-7C | frontend: "Edit with AI" menu item, `AiEditPanel`, change-set preview with per-change checkboxes, one-PATCH-then-ops apply loop, 34 new Vitest cases | frontend | DONE |
| IT5-7D | QA gate: `frontend/e2e/ai-edit.spec.ts` (4 tests, live model), full suites, acceptance walk, API probes | qa | DONE |

### Gate outcome

| Gate | Round | Verdict | Findings |
|---|---|---|---|
| security | 1 | APPROVE | no critical/high/medium. 1 low: apply loop had no guard that a `remove` op id ≠ the parent todo id (fixed). 3 info: control chars in the instruction (fixed), backend `due_date` re-clamp lacked the ±5-year horizon (fixed), absolute tag list from the preview-time snapshot (→ IT5-10) |
| reviewer | 1 | CHANGES REQUESTED | seams A↔B and B↔C traced correct end to end. Blocking: a checked "Add tag" silently dropped at the 10-tag cap when its paired removal was deselected, apply reported success; App-level apply tests covered only `title`, `reopen` never asserted. Lows: `applied` counted 404-skipped ops; backend `action` membership test raised on a non-hashable value (500); master-spec delta missing; `add` 404 means the parent is gone; two panel labels untested |
| reviewer | 2 | APPROVE | all 7 + the security low fixed, each with a test that fails on the old code |
| qa | 1 | PASS | 21/21 acceptance criteria; live model right on rename, add subtask + tag, "delete this todo" → `empty: true` (5/5 runs), "tomorrow" → correct date, positional subtask rename → real id. One bug in QA's own spec (stale trigger locator after a rename), fixed |

Notable: the live lane started at 1/3. The shared drafting rules (`_PRIORITY_RULE` "otherwise use medium", `_TAG_RULE`) made a bare rename restate `priority` and invent tags; gating each rule with "only if the request asks to change that field" and rendering the few-shot todo through the same `edit_todo_context()` as the real request took it to 3/3.

---

## Iteration 6 — `TODO` (scope not yet assigned)

| | |
|---|---|
| Status | **TODO** · no scope assigned |
| Estimation rule | ~1 h net per medium slice (implementation + 2 review rounds + QA), 2 h with a new technology, +15–20 min planning; iterations 4 and 5 both landed at ~1 h 35 min against planner estimates of 2.25–4.5 h |

### Backlog (candidates for iteration 6)

| ID | Task | Source | Weight | Status |
|---|---|---|---|---|
| IT3-5 | Add an `origin` remote and run GitHub Actions CI for the first time | lead | medium | NEEDS USER (remote URL + credentials) |
| IT5-1 | `LOG_LEVEL` backend setting: add to `.env.example`, compose and master §9.1 | backend IT4 | low | TODO |
| IT5-2 | Relative dates: "in N days" still mis-resolved in an ad-hoc live check (took the 7-day anchor); consider `qwen2.5:7b` on GPU or a deterministic date parser before the model | qa IT4 | low | TODO |
| IT5-3 | Remote tag rename/delete repairs the filter with a loud fetch (list blanks briefly); make the repair path quiet | reviewer IT4 | low | TODO |
| IT5-4 | Health-cache miss has no in-flight lock (probe pair fires per concurrent status call) | security IT4 | low | TODO |
| IT5-5 | jsdom note: `AiDraftPreview` mount autofocus inside a hidden panel would report a false focus steal in Vitest | reviewer IT4 | info | TODO |
| IT5-8 | "Edit with AI" on subtask rows (deferred by the planner) | planner IT5 | medium | TODO |
| IT5-9 | `parse-todo` path lacks the ±5-year due-date horizon and control-char strip that `edit-todo` has (iteration-2 code) | backend IT5 | low | TODO |
| IT5-10 | Tags applied as an absolute list from the preview-time snapshot; a tag added in another tab between preview and apply is dropped | security IT5 | low | TODO |
| IT5-11 | A nonsense instruction (`"a"`) occasionally makes the model propose `priority: medium`; neutralised by no-op suppression and the checkboxes, but a "no instruction understood" guard in the prompt would be cleaner | qa IT5 | info | TODO |

Larger feature proposals (not assigned): sharing lists between users, due-date notifications, export/import, PWA/offline, mobile layout.

---

## Environment (facts useful to every session)

- Host ports taken by another project (radar-gpw): 5432, 11434, 8000, 3000, 8080. Todo App: frontend 5173, backend 8010, db 5433, ai-agent unpublished.
- Secrets in `.env` (git-ignored), template in `.env.example`; the backend refuses to start on empty or example `JWT_SECRET` / `AI_AGENT_TOKEN`.
- Local Postgres: `postgresql://todo:todo@localhost:5433/todo`, test database `todo_test`.
- Demo account: `demo@example.com` / `Demo-Todo-2026`.
- Ollama model in the `todo-app-ollama` volume; `docker compose down` without `-v` keeps data.
- Docker `/var` filled up once in iteration 2; prune commands are denied to agents, the user frees space.
- Language: all project files, docs, commit messages and code comments are in English.
