# fix-issue-team — Runbook

Shared knowledge for the GitHub-issue-fixing agent team. Every agent reads
this file at the top of its run. The `team-retrospective` agent updates it
with new learnings after each completed issue.

This is the team's **long-term memory**. Keep entries terse and load-bearing.

---

## Mission

Take a GitHub issue number → ship a green PR that fixes it or builds the
requested capability. Six agents run in sequence; each writes one artifact
under `.claude/agents/fix-issue-team/runs/<issue-number>/` so later agents
can pick up cold and the retrospective can score the run.

The team handles **three issue shapes**: bug fixes, feature requests, and
**coverage campaigns** (multi-PR iterations against an umbrella issue that
stays open until coverage hits the target). Every agent prompt has a
framing section that covers all three — read your own first, then the
investigator's framing in `01-findings.md` to know which shape this run
is in.

### Coverage-campaign mode in one paragraph

A coverage campaign is an umbrella issue (e.g. "drive servers/ to 100%")
that fans out into **N iterations of tests-only PRs**. Each iteration picks
one tractable uncovered chunk, pins existing behaviour with regression
tests, ships, and the umbrella issue stays open until the target metric is
hit. Key inversions from bug/feature work:
- Tests are expected to **PASS first** (not red). Any failure surfaces a
  real bug — implementer decides in-scope vs follow-up per the plan.
- Implementer typically makes **no source changes** (tests-only iteration).
  No version bump. No manifest edits. The `.mcpb` would be byte-identical.
- Shipper uses **"Closes one chunk of #N"** in the PR body, NOT `Fixes #N`
  — the campaign issue must survive the merge.
- Commit subject is `test(scope): …`, not `fix` / `feat`.

## Per-run artifact contract

```
.claude/agents/fix-issue-team/runs/<N>/
  00-issue.md       gh issue view <N> output (verbatim)
  01-findings.md    issue-investigator   — root cause(s) + code refs
  02-plan.md        fix-planner          — tests to write, files to edit
  03-tests.md       tdd-test-writer      — test names + red run log
  04-impl.md        fix-implementer      — diff summary + green run log
  05-ship.md        pr-shipper           — commit SHA, PR URL, CI run id+status
  06-retro.md       team-retrospective   — what to change next time
```

Each agent **must** write its artifact before returning. Filenames are stable
so the orchestrator can show the user a checkpoint at each step.

## Repo conventions

- **Dual-manifest version parity**: `manifest.json`,
  `.claude-plugin/plugin.json`, and `pyproject.toml` must agree. Bump all
  three in the same commit, or don't bump at all. Tests-only changes do
  **not** get a version bump (see CLAUDE.md). CI auto-releases on push to
  `main` if `manifest.json` or `.claude-plugin/plugin.json` changed.
- **Tests live in `tests/`** and run via `uv run pytest tests/ --cov`.
- **No httpx-mock dependency**. Each test file rolls its own small fake
  (`_FakeIMAP`, `_FakeSieve`, `_FakeAsyncClient`, `_FakeClient`/
  `_FakeCalendar`/`_FakeEvent`). Monkeypatch at the integration seam
  (`_imap_connect`, `_get_account`, `_carddav_propfind`, etc.) — not deeper.
- **Conventional commits**: `fix(scope): …`, `feat(scope): …`,
  `test(scope): …`, `docs(scope): …`. Subject ≤72 chars, imperative mood.
- **PR titles** mirror the commit subject. PR body references `Fixes #N`
  for bug/feature shapes that **close** the issue, or `Closes one chunk of
  #N` for **coverage-campaign** iterations where the umbrella issue must
  stay open across multiple PRs.

## Recurring gotchas (from CLAUDE.md + past runs)

1. **`dict.get(key, default)` is NOT a null fallback.** If a Pydantic
   `Optional[str] = Field(default=None)` field is serialized as `null`,
   `acct.get("k", X)` returns `None`, not `X`. Use `acct.get("k") or X`.
   Bit us in Sieve v0.3.2 and in `email_list_folders` heading ("for None").

2. **`socket.create_connection((None, port))` connects to localhost.** Always
   guard truthy hostnames.

3. **DAV `<href>` URLs must be host-pinned** before issuing authenticated
   requests — use `_security.resolve_dav_url()`. Tests in
   `tests/test_dav_url_pinning.py`.

4. **`0o700` on a credentials parent dir is correct, not insecure.** Semgrep
   flags it; suppress with `# nosemgrep:
   python.lang.security.audit.insecure-file-permissions.insecure-file-permissions`
   + a one-line justification.

5. **No real hostnames/PII in tests, fixtures, commits.** Use
   `example.com` / `example.org`. The repo is public and greppable.

6. **`uv.lock` version-field syncs are not dep changes.** A version bump
   in `pyproject.toml` causes `uv.lock` to update its
   `name = "multi-mail-dev"` package's `version = …` line. Fold that into
   the same commit. A diff that touches anything else (new package
   entries, hash changes for unrelated packages) is a real dep delta —
   surface it to the user before committing.

7. **Narrow `except` in tests, always.** When asserting that bad input is
   rejected, catch the specific exception (`pydantic.ValidationError`,
   `ValueError`, etc.) — never bare `except Exception`. A broad catch
   swallows the `AttributeError` from a missing symbol and turns a
   feature-add red into a false green via the wrong path.

8. **Coverage iteration ≠ TDD red-then-green.** For coverage campaigns
   (issue shape: campaign), the test-writer's red is **inverted**: tests
   are expected to PASS on first run because they pin existing behaviour.
   A failure on first run = a real bug found (see past coverage rounds:
   header decoder, calendar UID, Sieve null fallback). The test-writer
   surfaces that to the implementer, who decides in-scope-fix vs
   follow-up per the plan — they do NOT silently rewrite the test to make
   it pass. Coverage iteration commits are `test(scope): …`, never `fix`
   or `feat`, and skip the version bump.

## IMAP/Sieve/DAV parsing pitfalls (carry forward)

- **IMAP LIST responses are not always quoted.** RFC 3501 allows the mailbox
  name to be an atom, a quoted string, or a literal `{N}\r\n…`. A parser that
  assumes quoted names extracts the delimiter `/` instead of the folder name.
  See `email_list_folders` (issue #4).
- imaplib returns literal-form responses as **tuples**:
  `(b'(...) "/" {N}', b'Literal-Name')`. Item iteration must handle both
  `bytes` and `tuple`.
- Modified UTF-7 mailbox names (RFC 3501 §5.1.3) are out of scope for the
  list parser; if a user reports Unicode garble, that's a separate fix.
- **Bare `try/except` fallbacks that never fire mask bugs, don't fix them.**
  The old folders parser had `try: rsplit(...) except IndexError: split()` —
  the except branch was dead code for the failing input. When investigating
  a parser bug, confirm which branch actually executes.

## Agent invocation contract

The orchestrator passes each agent the same context block:

```
Issue: #<N>
Run dir: .claude/agents/fix-issue-team/runs/<N>/
Previous artifacts: <list of files already present>
Branch: <current git branch>
```

Each agent is allowed (and encouraged) to read the previous artifacts before
starting. None of them should re-run `gh issue view` if `00-issue.md`
already exists — read the file.

## Scoring (used by team-retrospective)

The retrospective rates each step on:
- **Correctness**: did the artifact match reality?
- **Crispness**: was the artifact terse enough to be useful?
- **Surprises**: did the agent need clarification or hit dead ends?

Findings feed back into edits to the agent prompts. Material learnings end
up here, in the "Recurring gotchas" or "Pitfalls" sections.
