# fix-issue-team

A reusable, self-evolving Claude Code agent team that takes a GitHub issue
number and ships a green PR.

## Quick start

```
/fix-gh-issue 4
```

That's it. The orchestrator runs six agents in sequence, checkpoints with
you at the right moments, and watches CI until the PR is green.

## What's in here

```
.claude/
├── agents/
│   ├── issue-investigator.md      # 1. find root causes
│   ├── fix-planner.md             # 2. produce executable plan
│   ├── tdd-test-writer.md         # 3. write failing tests
│   ├── fix-implementer.md         # 4. make them green
│   ├── pr-shipper.md              # 5. commit + PR + watch CI
│   ├── team-retrospective.md     # 6. learn + improve the team
│   └── fix-issue-team/
│       ├── RUNBOOK.md             # shared team memory — gotchas, conventions
│       ├── README.md              # this file
│       └── runs/<issue>/          # per-run artifacts (gitignored)
└── commands/
    └── fix-gh-issue.md            # /fix-gh-issue <N> orchestrator
```

## Per-run artifacts

Every run leaves a trail under `runs/<issue-number>/`:

| File | Author | Contents |
|---|---|---|
| `00-issue.md` | orchestrator | `gh issue view <N>` verbatim |
| `01-findings.md` | investigator | Root cause(s), code refs, siblings |
| `02-plan.md` | planner | Tests to write, files to edit, version-bump call |
| `03-tests.md` | test-writer | New tests + red run log |
| `04-impl.md` | implementer | Diff summary + green run log |
| `05-ship.md` | shipper | Commit SHA, PR URL, CI conclusion |
| `06-retro.md` | retrospective | Scores + learnings encoded into agents |

These are scratch — the gitignore drops them. Permanent learnings live in
`RUNBOOK.md` and the individual agent prompts. The retrospective agent is
the only thing that's allowed to edit those.

## Self-evolution

Each completed run ends with the retrospective. It scores each step,
identifies what was unclear or wrong, and applies surgical edits to
`RUNBOOK.md` and the agent prompts. The team gets sharper run over run.

The retrospective never rewrites agent prompts from scratch — it edits
existing sections additively. If a learning doesn't pass the "one
data point isn't a law" sniff test, it lands in `06-retro.md` as a
follow-up rather than as a permanent rule.

## Extending the team

Add a new agent: drop `.claude/agents/<name>.md` with the standard YAML
frontmatter (`name`, `description`, `tools`, `model`). Wire it into
`.claude/commands/fix-gh-issue.md` at the right position in the run order
and decide what artifact filename it owns (continue the numeric prefix).

Add a new repo gotcha: don't edit `RUNBOOK.md` manually if you can avoid
it — file a fake issue and let the retrospective encode it. That keeps
the runbook's tone consistent.

## Why this exists

Bug fixes have a repeatable shape: read the issue, find the root cause(s),
plan the change, write tests first, implement, ship, watch CI. Doing
that ad-hoc every time leaks attention. A team with crisp charters does
it the same way every time, surfaces decisions at the right checkpoints,
and accumulates institutional knowledge in the runbook rather than in
chat history.
