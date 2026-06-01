# Email Attachment Tools — Design

**Date:** 2026-06-01
**Status:** Approved
**Target release:** v0.4.0

## Problem

The MCP server can read message bodies but offers no way to enumerate or
download attachments. A parallel cowork session implemented two tools
(`email_list_attachments`, `email_get_attachment`) plus 20 tests, but left
the change in three states that block a clean release:

1. A first-ever `ruff format` sweep of the whole 2,900-line `email_mcp.py`
   is tangled into the feature diff (~490 of 677 changed lines are pure
   reformatting).
2. Test coverage dropped from the repo's standing **100%** to **99%** — the
   new tools have 8 uncovered statements + 3 partial branches.
3. `ruff check` reports 9 pre-existing lint errors in test files.
4. Manifest/README parity and tool counts are stale and don't list the new
   tools.

## Goals

- Ship the two attachment tools as a clean, reviewable PR.
- Restore **100% statements + 100% branches** on `email_mcp.py` (the repo's
  coverage goal — README "Test coverage").
- Keep the three version manifests in sync and trigger the auto-release.

## Non-goals

- No new attachment capabilities beyond list + download-to-disk.
- No unrelated refactoring outside the attachment code.

## Tools (already implemented — kept as-is)

- **`email_list_attachments`** (read-only): `BODY.PEEK[]` fetch (keeps the
  message unread), renders a Markdown table `Index | Filename | Content-Type
  | Size`. Counts both `Content-Disposition: attachment` parts and inline
  parts that carry a filename (Outlook PDF case).
- **`email_get_attachment`** (writes to disk): selects one attachment by
  0-based `index` **or** `filename` (exactly one, enforced by a model
  validator), writes raw bytes to a validated absolute `save_path` (rejects
  relative paths and system dirs `/etc /usr /bin /sbin /System
  /Library/System`, including `..`-normalized traversal). Response carries
  metadata only, never the binary payload.

## Plan

### Commit 1 — `style: ruff format + lint sweep` (no version bump)
- `ruff format` `servers/email_mcp.py` (whole-file normalization).
- `ruff check --fix` the 9 lint errors across test files.
- Pure hygiene; zero behavior change. Isolated so commit 2 reviews cleanly.

### Commit 2 — `feat: email attachment list + download tools`
- **Refactor** the `email_get_attachment` selector loop: replace the nested
  `if index / elif filename` with a flat `is_match` computation, eliminating
  the provably-unreachable `elif` branch (`2211→2204`). No pragma needed.
- **Add ~5 tests** to `tests/test_email_mcp_attachments.py` restoring 100%:
  1. attachment part with no filename → renders `attachment-0`.
  2. `list_attachments` `logout()` raises → swallowed.
  3. `list_attachments` outer `except` via `_imap_connect` raising.
  4. `get_attachment` payload-`None` via monkeypatched `_iter_attachment_parts`
     seam.
  5. `get_attachment` `logout()` raises → swallowed.
- **manifest.json**: add the two tool entries; fix long_description count
  `34 → 38`.
- **README.md**: tool count `34 → 38`; refresh the test-coverage line
  (tests + file count); add roadmap **step 18 at the end** of the list (per
  the "new steps go at the end" rule); bump `<summary>` to "all 18 steps";
  add a `0.4.0` changelog entry.
- **Version bump** `0.3.15 → 0.4.0` in `manifest.json`,
  `.claude-plugin/plugin.json`, `pyproject.toml` (parity-checked by
  `pack-mcpb.sh` and the release workflow).

## Verification

- `uv run pytest tests/ --cov --cov-report=term` → all green, `email_mcp.py`
  back at **100%**.
- `uv run ruff check servers/ tests/` → clean.
- `bash scripts/pack-mcpb.sh` → version parity passes, bundle builds.

## Delivery

Feature branch `feat/email-attachments` → GitHub issue → PR referencing the
issue → CI `release.yml` auto-builds and publishes the `v0.4.0` `.mcpb` on
merge to `main`.
