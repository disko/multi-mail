# multi-mail — project notes for Claude

Working notes for contributors (human or AI). Read once at the start of a
session to avoid repeating the same questions and rediscovering the same
pitfalls.

## Architecture in one paragraph

A single Python MCP server (`servers/email_mcp.py`, ~2.9K lines) exposes 31
tools across email (IMAP/SMTP), Sieve (ManageSieve, RFC 5804), CalDAV, and
CardDAV. Security primitives (SSRF guard, redirect hook, DAV host pinning)
live in `servers/_security.py`. Account credentials are persisted as
plaintext JSON in `~/.claude/multi-mail-accounts.json` with `0600`/`0700`
permissions enforced by the writer. The repo ships **two parallel plugin
manifests** for two different Claude clients — see below.

## Dual manifest — keep all three versions in sync

| File | Consumer | Why |
|---|---|---|
| `.claude-plugin/plugin.json` | Claude Code (loaded via `~/.plugins/.claude-plugin/marketplace.json`) | Marketplace entry, must match plugin runtime |
| `manifest.json` (root) | Claude Desktop (.mcpb bundle uploaded via Customize → Personal plugins) | mcpb manifest_version 0.2 schema |
| `pyproject.toml` | Dev tooling (`uv sync`, pytest, etc.) | Identity of the dev environment |

`scripts/pack-mcpb.sh` and the `release` workflow both refuse to build if the
three versions don't match. When you bump a version, **bump all three in the
same commit**.

## CI is auto-release on version bump

`.github/workflows/release.yml` triggers on push to `main` that touches
`manifest.json` or `.claude-plugin/plugin.json`. The `detect` job validates
parity and skips if a matching `v<version>` tag already exists. The `bundle`
job packs the `.mcpb`, pushes a tag, and creates a GitHub release with the
bundle attached. **No tag bump → no release.** Manual `workflow_dispatch`
with `force: true` rebuilds the current version (deletes+recreates).

Quirks to remember:
- `.mcpb-cache/` starts with a dot, so `actions/upload-artifact@v4` needs
  `include-hidden-files: true` or the bundle is silently dropped.
- `fetch-depth: 0` on checkout — the version-diff check needs the full
  history to compare HEAD vs HEAD^, and the tag-existence check needs all
  tags fetched.

## Testing philosophy

- **No httpx mock library dependency.** Each test file rolls a small fake
  for what it needs (`_FakeIMAP`, `_FakeSieve`, `_FakeAsyncClient`,
  `_FakeClient`/`_FakeCalendar`/`_FakeEvent`). Less moving parts; fakes
  encode the exact contract we care about.
- **Monkeypatch at the integration boundary.** `_carddav_propfind`,
  `_carddav_list_vcards`, `_get_account`, `_imap_connect`, `_smtp_connect`,
  `_sieve_connect`, `_caldav_client`, `_get_calendar`, `_try_*` — these are
  the seams. Tests replace them with `monkeypatch.setattr` and drive the
  layer above.
- **Tests-only commits don't get a version bump.** If runtime didn't change,
  the `.mcpb` bundle would be identical. Save version bumps for behaviour
  changes or roll several rounds of coverage work into one milestone release.
- Tests have caught real bugs every couple of rounds (header decoding,
  formatter UID, etc.). Don't skip the "easy" coverage work — it pays.

## Recurring gotchas

### `dict.get(key, default)` is NOT a null fallback

`dict.get("k", X)` returns the **value** at `k` when present, even if that
value is `None`. Pydantic models with `Field(default=None)` serialize to
`"k": null`, so the key IS present after load → `.get` returns `None` →
your "default" never fires. The Sieve regression in v0.3.2 was exactly this
pattern. Use `dict.get("k") or default` (or explicit `is None` check) for
fields that can be nullable.

### `socket.create_connection((None, port))` connects to localhost

If a hostname resolution yields `None` and you pass it to `create_connection`,
you'll get `[Errno 61] Connection refused` (macOS) / `[Errno 111]` (Linux)
because Python's socket layer interprets `None` as "any interface" → loopback.
Always validate that host strings are truthy before connecting.

### Authenticated requests to server-controlled URLs leak credentials

DAV servers return `<href>` elements that the client follows with HTTP Basic
auth. A compromised server can redirect those to an attacker (`evil.com` —
public IP, SSRF guard doesn't block it). Pin every DAV URL to the host of the
configured `carddav_url`/`caldav_url` before issuing the auth'd request.
`_security.resolve_dav_url()` does this; the four CardDAV writers use it.

### File modes — `0o700` on a directory is MORE restrictive, not less

Semgrep's `insecure-file-permissions` rule treats anything other than `0o644`
as suspicious. For a credential file, that's backwards. `0o600` on the file
and `0o700` on its parent dir is the *correct* answer because the user is
the only legitimate reader. Use a `# nosemgrep:
python.lang.security.audit.insecure-file-permissions.insecure-file-permissions`
inline annotation with a comment explaining why.

## Semgrep false positives we suppress (with justification)

- `python.lang.security.audit.insecure-file-permissions.insecure-file-permissions`
  on `os.chmod(..., 0o700)` for the `accounts.json` parent dir — owner-only
  is correct for credential data.
- `python.mcp.mcp-auth-passthrough-taint.mcp-auth-passthrough-taint` on the
  three CardDAV `client.put(...)` / `client.delete(...)` calls — taint-flow
  pattern doesn't recognize `resolve_dav_url()` as a sanitizer, but it is
  one (test coverage in `tests/test_dav_url_pinning.py`).

If semgrep flags new code, treat it as a starting point: confirm the
finding, fix the underlying issue if real, suppress with a justified inline
comment if it's a false positive.

## Hostnames and PII — keep them out

This repo went public. Real mail server hostnames (e.g. the ones the user
hands you while debugging) **must not** appear in tests, fixtures, commit
messages, or comments. Use `example.com` / `example.org` placeholders. The
test suite is greppable and lives forever.

## Useful one-liners

```bash
# Run everything (uses uv + pyproject [tool.coverage] config)
uv run pytest tests/ --cov --cov-report=term

# Build the Desktop bundle locally (script enforces version parity)
bash scripts/pack-mcpb.sh

# Trigger CI release manually (e.g. to rebuild without bumping)
gh workflow run release.yml -f force=true
```

## Coverage roadmap state

See README "Test Coverage" section. Steps 1–8 done; steps 9–10 remaining
(`email_add_account`/move/folder/reply/forward write paths, and the four
`_try_*` discovery sources). New steps go at the end of that list, not the
top — order represents historical decisions.
