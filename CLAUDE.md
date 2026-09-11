# Agent Team Configuration — FULL AUTO variant

Central rules for a cooperating multi-agent software-development flow. Every subagent
inherits this file. Keep it short and authoritative — it is loaded into every session.
This file ships as a **universal seed**: copy it (with `.claude/`) into a new project
folder, then run `/new-project` to configure it.

> ⚙️ **This is the full-auto seed.** The team never asks for permission to edit files
> or run commands — including git merges, rebases, pushes, and every other VCS
> operation. The only hard limits: never touch anything outside the project root, and
> never run denied dangerous commands (`sudo`, destructive system ops, `rm -rf` outside
> the project root, force-push). Everything else runs automatically.

## Project Context
<!-- Filled by /new-project. ❓ = must be asked (no default). "(default)" = seed default —
     still confirmed or overridden during the bootstrap interview. -->
- **Domain:** Personal Todo app for the owner's private tasks. **Iteration 1 (current):**
  add todo, remove todo, mark completed/active, display all todos — in-memory store,
  no auth, no Docker (DB persistence and Docker are iteration-2 items). **Iteration 2
  roadmap:** PostgreSQL persistence · auth / multiple users · priorities, due dates, tags ·
  filtering / sorting / search · subtasks or multiple lists · Docker + docker-compose for
  backend, frontend, ai-agent and db · real-time updates · AI features (todos from natural
  language, split into subtasks, suggest priority/tags, daily summary) via a small Ollama
  model running in Docker.
- **Backend:** Python 3.12+ / FastAPI · package manager **uv** · Pydantic v2 models
- **Frontend:** React 18+ with TypeScript on Vite · **CSS:** Tailwind CSS
- **Database:** PostgreSQL (from iteration 2; iteration 1 is in-memory) · **Migrations:**
  Alembic (SQLAlchemy 2.x)
- **Build/test:** uv + pytest (httpx `AsyncClient` over `ASGITransport`) for the backend · npm + Vitest +
  Playwright for the frontend
- **CI/CD:** GitHub Actions

> ⛔ **Bootstrap gate:** if any ❓ remains above, this is a fresh seed copy. Before ANY
> planning or implementation — including `/feature` — run `/new-project` (or interview
> the user directly) about ALL of: project domain, backend language, JavaScript
> framework, CSS approach, and database — presenting the defaults above as the
> recommended options. If the folder contained files before seeding, read them first —
> they carry project info that shapes the interview; surface and resolve any
> discrepancy between those files, the defaults, and the user's answers instead of
> picking silently. Write the confirmed answers here, dropping the "(default)"
> markers. Never start work on an unconfigured stack, and never skip the interview just
> because defaults exist. Agents read this section to pick idioms and tools.

## How the team actually works

The **main session is the Team Lead / Orchestrator** (you, driving Claude Code, on **Fable**).
It does not write implementation code itself — it dispatches specialist **subagents** (in
`.claude/agents/`) via the Agent tool, reviews what they return, and decides what happens
next. There is no peer-to-peer messaging between subagents: every subagent reports its
result back to the orchestrator, which is the single coordination point. Shared state
lives in the repo (specs, code, git), not in a side-channel.

**`DASHBOARD.md` is the project's internal Jira.** It is the single source of truth for
iteration status, per-slice tasks, time measurements (net of session-limit pauses),
critical findings, and the backlog. The orchestrator updates it at iteration start, at
every slice merge to `main`, on any blocker, and at iteration close. Planners read its
backlog when scoping the next iteration. Never track status anywhere else.

**Session-limit resilience.** Sessions and subagents can be cut mid-task by usage limits.
Two rules keep that cheap: (1) the orchestrator keeps the "Current run state" section
of `DASHBOARD.md` current at every dispatch and every report (in-flight agents, worktree,
last commit, next step, time/pause log) and commits it, so a fresh session can recover
from the repo alone; (2) every implementing agent commits a `<agent>: wip <step>` commit
before any step longer than a few minutes (full suites, Docker builds, Postgres lane, E2E).
`wip` commits need no green tests. Never keep timing or state only in temp directories.

Pipeline for a feature (see `/feature`):

```
Planner ─▶ [Backend ‖ Frontend ‖ DevOps]  ─▶  Reviewer + Security  ─▶  QA  ─▶  Team Lead merges
  spec        implement in a git worktree         gate                verify    automatically
```

- **Bootstrap is step zero.** On a fresh seed copy, `/new-project` interviews the user for
  the tech guidelines (domain, backend language, JS framework, CSS, database) and fills
  **Project Context** before anything else runs.
- **Planning is synchronous and first.** No implementation starts without a spec. The
  spec is NOT paused for human approval — the orchestrator reviews it and proceeds,
  stopping only if the planner flags an unresolved contract-level ambiguity (API shape,
  DB schema, auth semantics) that cannot be answered from the repo.
- **Implementation agents run in parallel, each in its own git worktree** so they never
  clobber each other's files. Merge happens back through the orchestrator.
- **Progress is visible live from the main tree.** Each implementation agent commits
  every meaningful step on its own worktree branch (message format `<agent>: <step>`).
  Worktrees share the repo, so the human Team Lead can watch the work land step by step
  with `git log --all --oneline --graph` from the project root — and browse the files
  themselves under `.claude/worktrees/<name>/`. One big end-of-task commit is not
  acceptable.
- **Review and QA are gates, not suggestions.** Code does not reach `main` until the
  reviewer and QA agents pass it. Once both gates pass, the orchestrator merges the
  worktree branches into the default branch **automatically** — no human GO is
  required.

## Global Rules

### Safety
- **Stay inside the project root.** The project root is the directory containing this
  `CLAUDE.md`. No agent — orchestrator included — may read, write, or run commands on
  paths outside it. Git worktrees used by implementation agents live under
  `.claude/worktrees/` inside the root and are the only sanctioned working copies.
  If a task seems to require touching anything outside the root, stop and report it to
  the user instead. This is one of the only two hard limits in this seed.
- **All VCS operations are automatic.** The orchestrator merges, rebases, resets, and
  pushes without asking, as part of the pipeline. The only exception: **never
  force-push** — it is in the deny tier.
- DB migrations must be reviewed by the DevOps/DB agent and be reversible (tested
  rollback) — this is an agent gate, not a human prompt.
- No deploys to production without explicit human approval (deploys are outward-facing
  and are not covered by auto mode).
- No hardcoded secrets. Flag any secret-like string immediately.
- Implementation agents branch/worktree; they do not commit to a shared branch directly.
  On their **own** worktree branch they commit small and often — one commit per
  meaningful step, `<agent>: <step>` messages — so progress is observable from the main
  tree via `git log --all`.

### Auto mode & permissions
`settings.json` implements **two** permission tiers — there is NO ask tier; nothing
prompts the user:
- **Allowed silently:** everything else — file edits (`Edit`/`Write`), all commands
  inside the project root, and all VCS operations (`git merge`, `git rebase`,
  `git reset`, `git push`, network fetches, package installs, `rm -rf` **inside** the
  project root). No prompts. The root boundary itself is enforced by every agent's
  hard-boundary rule (Bash permission rules cannot be path-scoped): `rm -rf` and any
  other command may only ever target paths inside the project root.
- **Denied outright:** standard dangerous commands — `sudo`/`su`, shutdown/reboot,
  `dd`/`mkfs`/`fdisk`, `rm -rf` on `/`, `~` or `$HOME` (or anything else outside the
  project root), `chmod 777`, force-push. Never work around a denial (no re-quoting,
  no wrapping in a script, no alternative command with the same effect) — report the
  block to the user instead.
- Deny beats allow. Never promote a denied class to allow.
- **Subagents don't talk to the user.** A subagent that hits a denied command (or a
  need outside the project root) stops and returns the blocker to the orchestrator,
  which reports it to the user. Everything not denied, the subagent just does.

### Scope & altitude
- Do the task asked; surface adjacent problems rather than silently expanding scope.
- Prefer the smallest change that is correct and matches surrounding code.
- If requirements are ambiguous, stop and ask the Team Lead — never guess on contracts
  (API shapes, DB schema, auth semantics).

### Conventions
- **English only** in every project file: docs, specs, `DASHBOARD.md`, README, commit
  messages, code comments, UI copy, test names. Chat with the user may be in their
  language, but nothing written into the repo is.
- Match existing package/module structure and code style; read neighbors before writing.
- Backend: idiomatic use of the framework in **Project Context** — dependency injection,
  structured logging, meaningful error messages, tests on critical paths.
- Frontend: idiomatic components for the chosen framework, typed props/interfaces where
  the stack supports it, accessibility (WCAG AA).
- Every change ships with tests. "Done" means tests written and passing.

### Reporting
Each subagent's final message is its return value to the orchestrator (not a human
chat). It must be self-contained: what was done, files touched, tests run + result,
and anything the Team Lead must decide. State test failures plainly — never claim green
without having run them.

## Roles & model policy (details in `.claude/agents/`)

Orchestrator runs on **Fable**. Implementer model is set by task complexity:
`mechanical → haiku · intermediate → sonnet · complex → opus`. If a task can't be confidently
placed between sonnet and opus, use **opus** — sonnet is then reserved for QA. The planner
tags each subtask's complexity; the orchestrator passes the matching `model` at dispatch.

| Agent | File | Model | Writes code? |
|-------|------|-------|--------------|
| Orchestrator (Team Lead) | `commands/feature.md` | fable | no (coordinates) |
| Planner | `planner.md` | opus | no (specs only) |
| Backend | `backend.md` | haiku / sonnet / opus by complexity | yes |
| Frontend | `frontend.md` | haiku / sonnet / opus by complexity | yes |
| DevOps / DB | `devops.md` | haiku / sonnet / opus by complexity | yes (infra/migrations) |
| QA | `qa.md` | sonnet | yes (tests only) |
| Reviewer | `reviewer.md` | opus | no (read-only) |
| Security Reviewer | `security-reviewer.md` | opus | no (read-only) |
