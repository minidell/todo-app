export const meta = {
  name: 'feature-pipeline',
  description: 'Plan → parallel implement (worktrees) → adversarial review + security → QA gate, for a feature',
  whenToUse: 'Large or highly-parallel features where you want deterministic orchestration with verification gates rather than the interactive /feature command.',
  phases: [
    { title: 'Plan' },
    { title: 'Implement' },
    { title: 'Review' },
    { title: 'QA' },
  ],
}

// Pass the feature description as args, e.g. Workflow({name:'feature-pipeline', args:'add JWT auth'})
const request = typeof args === 'string' ? args : (args && args.request) || ''
if (!request) throw new Error('Provide the feature description via args (string or {request}).')

// ---- Phase 1: Plan (synchronous — everything downstream depends on the spec) ----
phase('Plan')
const SPEC_SCHEMA = {
  type: 'object',
  required: ['specPath', 'subtasks', 'openQuestions'],
  properties: {
    specPath: { type: 'string' },
    subtasks: {
      type: 'array',
      items: {
        type: 'object',
        required: ['agent', 'description'],
        properties: {
          agent: { type: 'string', enum: ['backend', 'frontend', 'devops'] },
          description: { type: 'string' },
          complexity: { type: 'string', enum: ['mechanical', 'intermediate', 'complex'] },
          dependsOn: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    openQuestions: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
  },
}

// Orchestrator runs on Fable (launch this workflow from a Fable session). Per-agent models
// are set explicitly below because a workflow's agent() defaults to the session model.
// Implementer model per subtask complexity; unknown/undecidable falls back to opus.
const MODEL_BY_COMPLEXITY = { mechanical: 'haiku', intermediate: 'sonnet', complex: 'opus' }

// Appended to every agent prompt: no agent may operate outside the project root.
const ROOT_RULE =
  ' Hard boundary: never read, write, or run commands outside the project root (the directory containing CLAUDE.md); worktrees under .claude/worktrees/ are inside the root. If the task requires anything outside the root, stop and report instead.'

const spec = await agent(
  `Act as the planner agent. Produce a technical spec for this feature and write it to docs/specs/. Tag each subtask with a complexity of "mechanical", "intermediate", or "complex" (if unsure between intermediate and complex, use complex). Feature: ${request}` + ROOT_RULE,
  { label: 'plan', phase: 'Plan', agentType: 'planner', schema: SPEC_SCHEMA, model: 'opus' }
)

// Hard stop if the planner surfaced blocking ambiguity — don't implement on a guess.
if (spec.openQuestions && spec.openQuestions.length) {
  log(`Planner raised ${spec.openQuestions.length} open question(s) — returning for human decision instead of guessing.`)
  return { status: 'needs-input', openQuestions: spec.openQuestions, specPath: spec.specPath }
}

// ---- Phase 2 + 3 + 4 pipelined per subtask: each slice implements, gets reviewed, then QA'd
// without a global barrier — backend can be in review while frontend still implements. ----
const REVIEW_SCHEMA = {
  type: 'object',
  required: ['verdict', 'findings'],
  properties: {
    verdict: { type: 'string', enum: ['APPROVE', 'CHANGES_REQUESTED'] },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'summary'],
        properties: {
          severity: { type: 'string' },
          summary: { type: 'string' },
          location: { type: 'string' },
        },
      },
    },
  },
}

log('Watch live progress from the project root: git log --all --oneline --graph (each agent commits every step on its own worktree branch; files under .claude/worktrees/).')

const results = await pipeline(
  spec.subtasks,
  // Stage 1: implement in an isolated worktree (model tiered by complexity; unknown -> opus)
  (task) =>
    agent(
      `Act as the ${task.agent} agent. Implement this subtask from the spec at ${spec.specPath}, in your own git worktree, with tests. Work in small steps and commit after each one on your worktree branch with a "${task.agent}: <step>" message — these commits are the human's live progress feed; never one squashed end commit, never a shared branch. Subtask: ${task.description}` + ROOT_RULE,
      { label: `impl:${task.agent}`, phase: 'Implement', agentType: task.agent, isolation: 'worktree', model: MODEL_BY_COMPLEXITY[task.complexity] || 'opus' }
    ).then((report) => ({ task, report })),

  // Stage 2: general + security review in parallel; loop back once if changes requested
  async (impl) => {
    if (!impl) return null
    const review = async () =>
      (await parallel([
        () =>
          agent(`Act as the reviewer agent. Review the diff for the ${impl.task.agent} subtask of ${spec.specPath}.` + ROOT_RULE,
            { label: `review:${impl.task.agent}`, phase: 'Review', agentType: 'reviewer', schema: REVIEW_SCHEMA, model: 'opus' }),
        () =>
          agent(`Act as the security-reviewer agent. Security-audit the diff for the ${impl.task.agent} subtask of ${spec.specPath}.` + ROOT_RULE,
            { label: `sec:${impl.task.agent}`, phase: 'Review', agentType: 'security-reviewer', schema: REVIEW_SCHEMA, model: 'opus' }),
      ])).filter(Boolean)

    let verdicts = await review()
    const changesWanted = verdicts.filter((v) => v.verdict === 'CHANGES_REQUESTED')
    if (changesWanted.length) {
      const findings = changesWanted.flatMap((v) => v.findings)
      await agent(
        `Act as the ${impl.task.agent} agent. Address these review findings in your worktree and re-run tests, committing each fix on your worktree branch with a "${impl.task.agent}: <step>" message:\n${JSON.stringify(findings, null, 2)}` + ROOT_RULE,
        { label: `fix:${impl.task.agent}`, phase: 'Review', agentType: impl.task.agent, isolation: 'worktree', model: MODEL_BY_COMPLEXITY[impl.task.complexity] || 'opus' }
      )
      verdicts = await review() // one re-review round
    }
    return { ...impl, verdicts }
  },

  // Stage 3: QA gate for this slice (sonnet)
  async (reviewed) => {
    if (!reviewed) return null
    const qa = await agent(
      `Act as the qa agent. Verify the ${reviewed.task.agent} subtask of ${spec.specPath} against its acceptance criteria; run the suites; give a PASS/BLOCKED verdict.` + ROOT_RULE,
      { label: `qa:${reviewed.task.agent}`, phase: 'QA', agentType: 'qa', model: 'sonnet' }
    )
    return { agent: reviewed.task.agent, verdicts: reviewed.verdicts, qa }
  }
)

return {
  status: 'complete',
  specPath: spec.specPath,
  risks: spec.risks || [],
  slices: results.filter(Boolean),
  note: 'FULL AUTO: nothing was merged yet — orchestrator, verify every slice has APPROVE verdicts and a QA PASS, then merge the worktree branches into the default branch automatically (no human GO needed), clean up the worktrees, and report the result. Slices without a clean gate go back to implementation instead of merging.',
}
