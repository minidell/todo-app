---
description: Bootstrap a freshly seeded project — interview for tech guidelines, fill CLAUDE.md, tailor the agent team.
argument-hint: [optional project name / one-line idea]
model: fable
---

You are the **Team Lead** bootstrapping a brand-new project from the seed. Nothing may be
planned or implemented until this bootstrap completes — the ⛔ bootstrap gate in
`CLAUDE.md` stays closed until every ❓ in **Project Context** is filled.

Project idea (may be empty): **$ARGUMENTS**

**Hard boundary:** never read, write, or run commands outside this project's root (the
directory containing `CLAUDE.md`).

## 0 — Read what's already there (before asking anything)
The project folder may have existed before seeding — any pre-existing files carry extra
project information. List the root (excluding `.claude/`, `CLAUDE.md`, `.git`) and read
what's informative: READMEs, docs/notes/specs, package manifests (`package.json`,
`pom.xml`, `build.gradle`, `pyproject.toml`, `go.mod`…), config files, and skim any
source code for the stack in use.
- Summarize to the user what you found and what it implies (domain hints, stack already
  chosen, existing conventions).
- If the folder is empty apart from the seed (`CLAUDE.md`, `.claude/`, `.git*`), skip
  the summary entirely and go straight to the interview — treat it as a brand-new
  project.

## 1 — Interview the user (never skip, never assume)
Ask about, in one round of questions (use AskUserQuestion; where `CLAUDE.md` → **Project
Context** carries a "(default)" value, present it as the first, "(Recommended)" option;
always offer free-form "Other"):
1. **Project domain** — what are we building, for whom; anything special (payments,
   realtime, ML, mobile…). No default — always asked open-ended.
2. **Backend language + framework** — default Java/Spring Boot; alternatives e.g.
   Python/FastAPI, Node/NestJS, Go.
3. **JavaScript framework** for the frontend — default React; alternatives Vue, Svelte,
   Angular, none/SSR.
4. **CSS approach** — no seed default: Tailwind, CSS Modules, styled-components, plain
   CSS, a UI kit.
5. **Database** — default PostgreSQL; alternatives MySQL, MongoDB, SQLite…

Findings from step 0 shape the interview:
- Where existing files already answer a question (e.g. `package.json` shows Vue,
  `pom.xml` shows Java/Maven), present that as the recommended answer INSTEAD of the
  seed default, citing the file.
- Where a finding **contradicts** a seed default or a user answer, or the files are
  ambiguous (e.g. two frameworks present, docs mention a different DB than the code),
  do not pick silently: name the discrepancy, explain both sides, and ask the user to
  decide.

For everything the answers imply (migration tool, build tool, test frameworks, package
manager), the seed defaults in Project Context apply when the user keeps the default
stack; if they diverge, propose fitting replacements and confirm them — do not silently
choose, and do not proceed while any of the five answers is missing.

## 2 — Record the answers
- Update `CLAUDE.md` → **## Project Context** with the confirmed answers: replace every
  ❓ and rewrite the "(default)" entries as the confirmed values, dropping the
  "(default)" markers. Keep it one line per item — the ⛔ gate deactivates by itself
  once no ❓ remains. Add a one-line **Domain** note for anything important learned from
  the pre-existing files.
- Never overwrite or delete pre-existing project files during bootstrap; if one of them
  conflicts with the configuration the user chose (e.g. an old README naming a dropped
  framework), point it out and ask whether to update it.
- Update `.claude/settings.json`, keeping its two-tier FULL-AUTO structure (allow
  everything silently — including all VCS operations · deny dangerous — see
  `CLAUDE.md` → "Auto mode & permissions"): there is NO ask tier, do not add one. If
  the chosen stack has extra dangerous commands, add them to deny. Never weaken the
  deny list.

## 3 — Tailor the team
- The core agents (planner, backend, frontend, devops, qa, reviewer, security-reviewer)
  are stack-agnostic — they read Project Context. Edit one only if the chosen stack needs
  a specific extra rule.
- If the domain or stack warrants **new specialized agents** (e.g. mobile, data/ML
  pipeline, payments/compliance), propose them — one line of justification each — and
  **ask the user's permission first**. Create only the approved ones in
  `.claude/agents/`, following the structure of the existing agent files (frontmatter:
  name, description, tools, model; body: hard boundary, when invoked, standards, exit
  criteria).

## 4 — Initialize
- `git init` if this folder is not yet its own repository; make an initial commit:
  `Bootstrap from seed: <one-line stack summary>`.
- Do **not** scaffold application code unless the user asks for it here — scaffolding is a
  `/feature` job with a spec. Finish by summarizing the configuration and pointing the
  user at `/feature <first feature>`.
