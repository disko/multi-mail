# Issue #20 — Retrospective

## Score

- investigator: 5 — correctly identified the real crash site (`b" ".join(conn.capabilities)`, not `conn.uid()` as the reporter claimed), found all three sibling sites, and diagnosed the fake's type mismatch as the test coverage gap. Precise line citations throughout.
- planner:      5 — plan was surgical: exact line numbers, grep-verify step, explicit instruction to update all bytes-literal call-sites and the fake default, correct version-bump decision, and a note to re-grep after edits to avoid missing a fourth site.
- test-writer:  5 — three focused regression tests, each isolating one of the three affected tools with a str-tuple fake; red run clearly showed the right failure mode (uid_expunges empty because outer except caught the TypeError).
- implementer:  4 — all edits correct and verified with before/after grep counts; drive-by reformatting of `plugin.json` keywords array was the only out-of-plan change (harmless, user approved shipping as-is).
- shipper:      5 — clean commit, CI 5/5 across Python matrix, correct version bump, PR linked to issue.

## What worked

- Investigator correctly rejected the reporter's suggested `.encode()` patch and traced the crash to the actual raise site inside the broad `except`.
- Sibling sweep found all three affected sites in one pass; no site was missed.
- TDD cycle was tight: three red tests → six-line source fix → full suite green in a single implementer pass.

## What didn't

- `_FakeIMAP.capabilities` had been bytes-typed since the fake was first written, silently hiding the type mismatch for the full lifetime of the UIDPLUS-gate tests. (caused: three production tools were broken while 100% coverage was reported.)
- Implementer reformatted the `plugin.json` keywords array without being asked, adding noise to the diff. (caused: cosmetic only; no functional impact, but makes diffs harder to review.)

## Learnings encoded this run

- `RUNBOOK.md` — added gotcha #9: "Broad `except` at call site means the crash line isn't the blamed line"
- `RUNBOOK.md` — added gotcha #10: "Test fakes must mirror real-library type contracts, not assumed ones"
- `issue-investigator.md` — added "Don't trust the reporter's blamed line" bullet to Investigation protocol (pointer to RUNBOOK #9)
- `tdd-test-writer.md` — added "Verify fake attribute types against the real library" section (pointer to RUNBOOK #10)
- `fix-implementer.md` — added "No drive-by reformatting" rule to Rules section

## Follow-ups (not encoded — needs human call)

- IMAP modified UTF-7 encoding for non-ASCII folder names in the `COPY` arg — noted in issue as out-of-scope; whether to add `imap-utf7` dependency is a product decision, not a team rule.
