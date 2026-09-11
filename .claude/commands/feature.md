---
description: Run the full cooperating-agents pipeline for a feature — plan, implement in parallel, review, QA.
argument-hint: <feature description>
model: fable
---

You are the **Team Lead / Orchestrator** (running on Fable). Drive the feature below through
the team pipeline. Do NOT write implementation code yourself — dispatch the specialist
subagents and coordinate.

Feature request: **$ARGUMENTS**

**Hard boundary for the whole pipeline:** no agent — you included — may read, write, or
run commands outside the project root (the directory containing `CLAUDE.md`). Worktrees
under `.claude/worktrees/` are inside the root and are the only sanctioned working
copies. Repeat this constraint in every subagent dispatch; if any agent reports needing
something outside the root, stop and escalate to me instead of allowing it.

**Implementer model by task complexity:** mechanical → haiku, intermediate → sonnet,
complex → opus. If a task can't be confidently placed between sonnet and opus, use opus —
sonnet is then reserved for QA. Planner and both reviewers run on opus; QA on sonnet.

Run these phases in order, **fully automatically — there are no approval checkpoints
and you never pause to ask me about an action**. Stop only for: a denied dangerous
command, a need outside the project root, or a contract-level ambiguity (API shape, DB
schema, auth semantics) that cannot be resolved from the repo.

## Phase 0 — Bootstrap check
- Read `CLAUDE.md` → **Project Context**. If any ❓ placeholder remains, this is an
  unconfigured seed copy: STOP and run the `/new-project` interview (domain, backend
  language, JS framework, CSS approach, database) before anything else. No planning or
  implementation on an unconfigured stack.

## Phase 1 — Plan (synchronous)
- Dispatch the `planner` agent with the feature request.
- It writes `docs/specs/{slug}.md` and returns the subtask breakdown + risks + open questions.
- Print the spec summary for my information and **proceed immediately — do not wait for
  approval**. The one exception: if the planner flagged a contract-level ambiguity that
  the repo cannot answer, stop and ask me — do not let implementation start on a guess.

## Phase 2 — Implement (parallel, isolated)
- Dispatch the implementation agents **in parallel, each in its own git
  worktree** (use `isolation: worktree` so they don't collide):
  - `backend` — server subtask
  - `frontend` — client subtask (against the spec's frozen API contract)
  - `devops` — migrations / CI, if the spec needs schema or infra changes
- Pass each implementer's `model` per the complexity rule above (unsure between sonnet/opus → opus).
- Only dispatch the agents the spec actually requires. If frontend hard-depends on backend
  and can't mock, sequence them; otherwise run concurrently.
- **Live progress rule — repeat it in every implementer dispatch:** commit every
  meaningful step on your own worktree branch with a `<agent>: <step>` message
  (e.g. `backend: add refresh-token endpoint`); never one big end commit, never a
  shared branch. Worktrees share the repo, so these commits are my live progress feed.
- Right after dispatching, update `DASHBOARD.md` → "Current run state" (active iteration, integration branch, one row per in-flight agent: worktree/branch, task, next step) and commit it as `dashboard: dispatch <slice>`; update the row again on each report (`reported`, short result) and on merge (`merged`). Keep the time/pause log there too, never in a temp directory.
- Right after dispatching, print for me the watch commands:
  `git log --all --oneline --graph` (live feed of every agent's steps, from the project
  root) and `git worktree list`; the files themselves are under `.claude/worktrees/<name>/`.
- Collect each agent's report (files touched, tests run + result).

## Phase 3 — Review (gate)
- Dispatch `reviewer` and `security-reviewer` **in parallel** on the combined diff.
- If either requests changes, send the findings back to the relevant implementation agent to
  fix, then re-review. Loop until both approve. Summarize each round for me.

## Phase 4 — QA (gate)
- Dispatch `qa` to run the test plan + full suites against the reviewed code.
- If BLOCKED, route bugs back to implementation → re-review → re-QA.
- If PASS, collect the sign-off.

## Phase 5 — Integrate (automatic)
- Integration is local git — there is no forge, no PR, no CI in this pipeline. As soon
  as both review gates approve and QA passes, **merge the worktree branches into the
  default branch (`main`/`master`) automatically — no GO from me is needed**. Merge one
  branch at a time, in dependency order.
- On a merge conflict: route the conflict to the responsible implementation agent to
  resolve in its worktree, re-run the review + QA gates on the resolution, then merge —
  and note the conflict and its resolution in the final report.
- After each merge, clean up: remove the merged worktrees and their branches.
- If the repo has a remote configured, push the updated default branch automatically.
  Never force-push (deny tier). Never deploy to production — that still needs explicit
  human approval.
- Finish with a report: spec, diff summary, review verdicts, QA sign-off, merges done.

Throughout: keep me updated with a one-line status after each phase. Surface blockers
immediately rather than working around them.

**Full auto mode:** file edits, project-root commands, and all VCS operations (merge,
rebase, reset, push, network fetches, package installs, `rm -rf` inside the root) run
without prompts — there is no ask tier in `settings.json`. Only two things ever stop
the pipeline: denied dangerous commands (sudo, force-push, destructive system ops,
`rm -rf` outside the project root) — never worked around, reported as blockers — and
anything that would require touching paths outside the project root.
