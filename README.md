# Multi-Mail Plugin

[![tests](https://github.com/disko/multi-mail/actions/workflows/test.yml/badge.svg)](https://github.com/disko/multi-mail/actions/workflows/test.yml)
[![coverage](https://codecov.io/gh/disko/multi-mail/branch/main/graph/badge.svg)](https://codecov.io/gh/disko/multi-mail)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> Talk to your own mail, calendar, and contact servers from Claude. Self-hosted, multi-account, no SaaS in the middle.

Multi-Mail plugs Claude into the open mail-server stack you already run — IMAP/SMTP, ManageSieve filters, CalDAV calendars, and CardDAV contacts. Add an email address and the plugin auto-discovers the rest. Once configured, just talk to Claude:

> "Check my work email and reply to anything from Alice about the invoice."
> "Set up a vacation auto-reply for next week."
> "What's on my calendar Thursday?"

## What you get

| | |
|---|---|
| **31 MCP tools** | Email read/send/reply/forward/move, folder/Sieve management, calendar event CRUD, contact CRUD |
| **Autodiscovery** | Mozilla autoconfig, Microsoft/Mailcow Autodiscover, DNS SRV, and `.well-known` DAV — add an account by typing its email address |
| **Multi-account** | Add and remove accounts at runtime; switch contexts per request |
| **Sieve filters** | Manage server-side filtering rules via ManageSieve (RFC 5804) |
| **Security by default** | TLS verification, SSRF guard on every redirect hop, `defusedxml` parsing, DAV host pinning, `0600`/`0700` on the credentials file |
| **No SaaS** | All traffic goes from your machine to your mail server. The plugin talks to your servers, not ours. |

---

## Install

### Claude Desktop (recommended for most users)

1. Grab the latest `.mcpb` bundle from the [releases page](https://github.com/disko/multi-mail/releases/latest).
2. In Claude Desktop, open **Customize → Personal plugins**, then drag the `.mcpb` onto the panel. To update later, use the ⋮ menu on the existing entry to replace.
3. Restart Claude Desktop.
4. Install [`uv`](https://docs.astral.sh/uv/) if you don't have it — the server uses PEP 723 inline deps via `uv run`:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
5. Ask Claude: *"Add my email account — alice@example.com"*. Claude will run autodiscovery and walk you through it.

### Claude Code

1. Clone into Claude Code's plugin directory (or anywhere on disk; point Code at it via `/plugin`):
   ```bash
   git clone https://github.com/disko/multi-mail.git ~/.plugins/multi-mail
   ```
2. Install `uv` (same one-liner as above).
3. Restart Claude Code. You'll see `/email-add-account`, `/email-remove-account`, and `/email-list-accounts` slash commands plus the 31 MCP tools.

Updating later: `git pull` then restart the client.

### Verify it's working

Ask Claude:

> "List my email accounts."

You should see an empty list (or your accounts if you've already added some). If Claude tells you the tools aren't available, restart the client.

---

## Try it

Once you've added at least one account:

| You say | Claude does |
|---|---|
| *"Check my work email"* | Lists recent INBOX messages |
| *"Search for emails from alice about the invoice"* | IMAP SEARCH `FROM "alice" SUBJECT "invoice"` |
| *"Reply to that and say I'll be there"* | `email_reply` with threading headers |
| *"Forward this to the team"* | `email_forward` with notes |
| *"Create a folder called Archive/2026"* | `email_create_folder` |
| *"Show me my Sieve filters"* / *"Set up a vacation auto-reply"* | `email_sieve_*` |
| *"What's on my calendar this week?"* | `cal_list_events` with a date window |
| *"Create a meeting tomorrow at 2pm"* | `cal_create_event` |
| *"Find Bob's phone number in my contacts"* | `card_search_contacts` |
| *"Add a new contact for Alice"* | `card_create_contact` |

---

## Configuration

Accounts are stored at `~/.claude/multi-mail-accounts.json`. The plugin creates and chmods this file (`0600` on POSIX) — you can also edit it by hand. Each entry looks like:

```json
{
  "id": "work",
  "display_name": "Work Email",
  "email_address": "me@example.com",
  "username": "me@example.com",
  "password": "app-password-here",
  "imap_host": "mail.example.com",
  "imap_port": 993,
  "imap_security": "ssl",
  "smtp_host": "mail.example.com",
  "smtp_port": 587,
  "smtp_security": "starttls",
  "sieve_host": null,
  "sieve_port": 4190,
  "sieve_security": "starttls",
  "caldav_url": "https://mail.example.com/dav",
  "carddav_url": "https://mail.example.com/dav"
}
```

- **Sieve fields are optional** — `sieve_host` defaults to the IMAP host on port 4190 with STARTTLS.
- **DAV URLs are optional** — autodiscovery tries `.well-known` endpoints.
- **Use app-specific passwords** when your mail server supports them. Don't paste your primary password.
- **Self-signed certs:** set `*_allow_insecure: true` only on trusted networks. Don't ship this to production.

### Slash commands (Claude Code)

- `/email-add-account` — add a new account (runs autodiscovery automatically)
- `/email-remove-account` — remove an account
- `/email-list-accounts` — show all configured accounts

---

## Security

Multi-Mail handles credentials and reaches user-controlled servers, so the threat model is non-trivial.

### Already in place

- **TLS verification** on every outbound connection (IMAP / SMTP / ManageSieve / CalDAV / CardDAV / autodiscovery). Disable per-account with `*_allow_insecure: true` only when you must.
- **SSRF guard** resolves every hostname and every redirect hop before connecting — refuses loopback, RFC1918, link-local, multicast, reserved, and known cloud-metadata addresses. Override with `MULTI_MAIL_ALLOW_PRIVATE_AUTODISCOVER=1` only on lab networks.
- **`defusedxml`** for every XML response from an untrusted server (autodiscovery + DAV) — billion-laughs and entity-expansion payloads are rejected.
- **DAV host pinning** — `<href>` elements returned by a CardDAV server are resolved against your configured `carddav_url` and cross-host results are refused. A compromised server can't redirect your auth'd request to an attacker.
- **Credential file mode** — `accounts.json` is created with `0o600` and its parent dir with `0o700` on POSIX. Verified by tests.
- **UIDPLUS `UID EXPUNGE`** for moves — won't accidentally wipe other `\Deleted` messages in the source folder.

### Known limitations

- Account passwords are still **plaintext** in `~/.claude/multi-mail-accounts.json`. Migration to OS keychain (`keyring`) is on the roadmap.
- Inbound HTML email bodies are returned to the model **verbatim** — treat them as untrusted input. HTML stripping + "untrusted content" delimiter is on the roadmap.
- Windows: the chmod is a no-op. Rely on the user profile ACL.

To report a vulnerability privately, see [SECURITY.md](SECURITY.md).

---

## MCP tool reference

<details>
<summary><b>Email (14 tools)</b></summary>

| Tool | Description |
|------|-------------|
| `email_autodiscover` | Auto-detect IMAP/SMTP/CalDAV/CardDAV from an email address |
| `email_list_accounts` | List all configured accounts |
| `email_add_account` | Add a new account |
| `email_remove_account` | Remove an account |
| `email_list_folders` | List IMAP folders |
| `email_create_folder` | Create a folder |
| `email_delete_folder` | Delete a folder |
| `email_list_messages` | List messages (paginated) |
| `email_search_messages` | Search with IMAP SEARCH syntax |
| `email_read_message` | Read full message content |
| `email_send_message` | Send a new email |
| `email_reply` | Reply or reply-all |
| `email_forward` | Forward a message |
| `email_move_message` | Move between folders |
</details>

<details>
<summary><b>Sieve filters (6 tools)</b></summary>

| Tool | Description |
|------|-------------|
| `email_sieve_list` | List Sieve filter scripts |
| `email_sieve_get` | Retrieve a script's content |
| `email_sieve_put` | Upload (create/replace) a Sieve script |
| `email_sieve_activate` | Set active filter or deactivate all |
| `email_sieve_delete` | Delete a Sieve script |
| `email_sieve_rename` | Rename a Sieve script |
</details>

<details>
<summary><b>Calendar — CalDAV (6 tools)</b></summary>

| Tool | Description |
|------|-------------|
| `cal_list_calendars` | List all calendars |
| `cal_list_events` | List events (optional date range) |
| `cal_get_event` | Get full event details |
| `cal_create_event` | Create a new event |
| `cal_update_event` | Update an existing event |
| `cal_delete_event` | Delete an event |
</details>

<details>
<summary><b>Contacts — CardDAV (7 tools)</b></summary>

| Tool | Description |
|------|-------------|
| `card_list_addressbooks` | List all address books |
| `card_list_contacts` | List contacts in an address book |
| `card_search_contacts` | Search contacts by name or email |
| `card_get_contact` | Get full contact details |
| `card_create_contact` | Create a new contact |
| `card_update_contact` | Update an existing contact |
| `card_delete_contact` | Delete a contact |
</details>

---

## Development

### Run the test suite

```bash
uv sync --group dev
uv run pytest tests/ --cov --cov-report=term
```

To open an HTML coverage report:

```bash
uv run pytest tests/ --cov --cov-report=html
open htmlcov/index.html
```

### Test coverage

| Module | Coverage | Status |
|--------|----------|--------|
| `servers/_security.py` | 96% | SSRF guard, redirect hook, DAV host pinning |
| `servers/email_mcp.py` | ~71% | Helpers, account IO, message assembly, vCard/iCal formatters, IMAP read + write flows (folder CRUD, move with UIDPLUS branch, reply, forward), CardDAV/CalDAV/Sieve tool flows, autodiscover orchestrator + all four discovery sources |

Coverage is being increased phase by phase (see roadmap below) starting with the modules that have the highest blast radius (security, autodiscovery, server interaction). Three real bugs and one security fix have been surfaced by this coverage work so far.

<details>
<summary>Coverage roadmap (steps 1–8 ✅ done)</summary>

1. ✅ Account management (`tests/test_account_io.py`)
2. ✅ Message formatting (`tests/test_message_building.py`)
3. ✅ vCard / iCal formatting (`tests/test_dav_formatting.py`)
4. ✅ IMAP tool flows (`tests/test_imap_tool_flows.py`)
5. ✅ CardDAV tool flows (`tests/test_carddav_tool_flows.py`)
6. ✅ CalDAV tool flows (`tests/test_caldav_tool_flows.py`)
7. ✅ Sieve tool flows (`tests/test_sieve_tool_flows.py`)
8. ✅ Autodiscover orchestrator (`tests/test_autodiscover_orchestrator.py`)
9. ✅ Remaining IMAP/account write paths — `email_add_account` (dedupe + disk persistence), `email_create_folder`/`delete_folder`, `email_move_message` (both UIDPLUS branches), `email_reply` (threading + reply-all addressee filtering), `email_forward` (`tests/test_imap_write_flows.py`)
10. ✅ Discovery sources — `_try_mozilla_autoconfig` (primary URL, well-known fallback, network-error swallow), `_try_microsoft_autodiscover` (subdomain primary, root-domain fallback), `_try_wellknown_dav` (PROPFIND 207, partial discovery), `_try_dns_srv` (SSL-over-STARTTLS preference, RFC 2782 target-`.` rejection) — fake httpx client + fake `dig` subprocess (`tests/test_discovery_sources.py`)
</details>

### Build the Desktop bundle locally

```bash
bash scripts/pack-mcpb.sh
```

Writes `.mcpb-cache/multi-mail-<version>.mcpb`. The script enforces that the three version fields (`manifest.json`, `.claude-plugin/plugin.json`, `pyproject.toml`) match — releases will fail otherwise. CI (`.github/workflows/release.yml`) packs and publishes the bundle automatically on every version bump pushed to `main`.

For deeper contributor notes, see [CLAUDE.md](CLAUDE.md).

---

## Components

| Component | Description |
|-----------|-------------|
| **MCP Server** (`servers/email_mcp.py`) | Python/FastMCP server exposing 31 tools |
| **Security helpers** (`servers/_security.py`) | SSRF guard, TLS redirect hook, DAV host pinning |
| **Workflow skill** (`skills/email-workflows/`) | Workflow guidance, IMAP search syntax cheatsheet, Sieve language reference |
| **Slash commands** | `/email-add-account`, `/email-remove-account`, `/email-list-accounts` |

---

## Changelog

### 0.3.9 — Sieve error diagnostics

- **Improved:** `_sieve_connect` now surfaces *why* a ManageSieve login failed instead of bubbling up the library's bare `No matching authentication mechanism found` / `NO` strings. Every error now includes the host:port, the security mode actually used, TLS verification state, and the SASL mechanisms the server advertised. When the server returns no mechanisms and `sieve_security != "starttls"`, the error explicitly tells the user to change that field in `accounts.json` (which is almost always the fix).
- **Added:** `tests/test_sieve_diagnostics.py` — 6 tests covering empty-loginmechs path (with and without STARTTLS hint), `login()` returning `NO`, `login()` raising `managesieve.error`, and the happy path.

### 0.3.8 — coverage roadmap complete

No runtime changes. Roadmap steps 9 and 10 done — every MCP tool flow and every autodiscovery source now has regression coverage.

- **IMAP/SMTP write paths** (`tests/test_imap_write_flows.py`, 11 tests) — `email_add_account` (dedupe + disk persistence), folder CRUD, both branches of `email_move_message` (UIDPLUS + UIDPLUS-absent refuse-and-rollback), `email_reply` (threading + reply-all addressee filtering + idempotent "Re:" prefix), `email_forward` (quoted-original assembly + idempotent "Fwd:" prefix).
- **Discovery sources** (`tests/test_discovery_sources.py`, 14 tests) — Mozilla autoconfig (primary URL + well-known fallback + network-error swallow), Microsoft Autodiscover (subdomain primary + root-domain fallback), `.well-known` DAV (PROPFIND 207 + partial discovery), DNS SRV (SSL-over-STARTTLS preference + RFC 2782 target-`.` rejection) via fake httpx client + fake `dig` subprocess.

Project coverage: **60% → 72%** (`email_mcp.py` from 65% to ~71%; `_security.py` steady at 96%). 184 tests across 11 files.

### 0.3.7 — tool flow coverage milestone

No runtime changes — but a substantial test investment. Coverage of `email_mcp.py` went from **24% to 59%** (`_security.py` at 96%, project total 60%). 62 new tests across five files covering the IMAP, CardDAV, CalDAV, Sieve tool flows and the autodiscover orchestrator.

### 0.3.6 — calendar UID fix + DAV formatter tests

- **Fixed:** `_format_event` returned the literal string `"{''}"` as the UID for any iCalendar event missing a UID property. Coverage 31% → 33%.

### 0.3.5 — accounts.json permissions

- **Fixed (security):** `_save_accounts` wrote `accounts.json` with the process umask — typically `0644`, world-readable. Now created with `0o600` / `0o700` via `os.open` + explicit `chmod` on POSIX.

### 0.3.4 — header decoding + helper coverage

- **Fixed:** `_decode_header` produced doubled spaces in mixed encoded/plain headers and crashed with `LookupError` on unknown charsets (common in spam). Both fixed; coverage 23% → 27%.

### 0.3.3 — CardDAV host pinning

- **Fixed (security):** A compromised or MITM'd CardDAV server could return cross-origin `<href>` elements and trick the client into sending HTTP Basic credentials to an attacker. `resolve_dav_url()` now pins every href to the configured `carddav_url` host.

### 0.3.2 — Sieve fix

- **Fixed:** ManageSieve connections failed with `[Errno 61] Connection refused` for every account because `dict.get("sieve_host", imap_host)` returns `None` (not the IMAP host) when the saved JSON has `"sieve_host": null`. Now falls back via truthy-OR.
- **Added:** `LICENSE` (MIT) and `SECURITY.md`.

### 0.3.1 — packaging

- Claude Desktop `.mcpb` bundle, pack script, and auto-release CI workflow.

### 0.3.0 — security release

- TLS verification by default on autodiscovery + CardDAV.
- SSRF guard.
- `defusedxml` for untrusted XML.
- UIDPLUS `UID EXPUNGE` (no more accidental wipe of `\Deleted` siblings on move).
- All blocking IMAP/SMTP/ManageSieve work runs in `asyncio.to_thread`.

### 0.2.0

Initial release.

---

## License

MIT — see [LICENSE](LICENSE).

## Contributing

Bug reports, security advisories, and PRs welcome. See [SECURITY.md](SECURITY.md) for the vulnerability reporting workflow and [CLAUDE.md](CLAUDE.md) for contributor notes.
