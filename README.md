# Multi-Mail Plugin

[![tests](https://github.com/disko/multi-mail/actions/workflows/test.yml/badge.svg)](https://github.com/disko/multi-mail/actions/workflows/test.yml)
[![coverage](https://codecov.io/gh/disko/multi-mail/branch/main/graph/badge.svg)](https://codecov.io/gh/disko/multi-mail)

Connect Claude to multiple self-hosted mail servers — IMAP, SMTP, Sieve filters, CalDAV calendars, and CardDAV contacts.

## Features

- **Autodiscovery**: automatically detect IMAP/SMTP/CalDAV/CardDAV settings from an email address via Mozilla autoconfig, Microsoft/Mailcow Autodiscover, DNS SRV records, and `.well-known` endpoints
- **Multi-account**: dynamically add and remove mail accounts at runtime
- **Full IMAP**: list folders, browse messages, search with IMAP SEARCH syntax, read full messages, move between folders, create/delete folders
- **Full SMTP**: send new emails, reply (including reply-all), forward with notes
- **Sieve filters**: manage server-side mail filtering rules via ManageSieve (RFC 5804) — list, create, edit, activate, delete scripts
- **CalDAV calendars**: list calendars, browse/create/update/delete events
- **CardDAV contacts**: list address books, browse/search/create/update/delete contacts
- **Security**: supports SSL/TLS and STARTTLS per account, with optional self-signed cert bypass
- **Sent folder**: outgoing messages are automatically saved to the Sent folder

## Components

| Component | Description |
|-----------|-------------|
| **MCP Server** (`servers/email_mcp.py`) | Python/FastMCP server exposing 31 tools (email, Sieve, CalDAV, CardDAV) |
| **Skill** (`skills/email-workflows/`) | Workflow guidance, IMAP search syntax, and Sieve language reference |
| **Commands** | `/email-add-account`, `/email-remove-account`, `/email-list-accounts` |

## Setup

### Requirements

The MCP server uses [uv](https://docs.astral.sh/uv/) with inline script dependencies (PEP 723), so there's no manual install step. Just make sure `uv` is available on your PATH:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Dependencies (`mcp`, `pydantic`, `httpx`, `managesieve`, `caldav`, `vobject`) are resolved and cached automatically by `uv run` on first launch.

### Account Configuration

Accounts are stored in `~/.claude/multi-mail-accounts.json` (created automatically). You can manage accounts using the slash commands or by editing the file directly.

Each account entry looks like:

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
  "imap_allow_insecure": false,
  "smtp_host": "mail.example.com",
  "smtp_port": 587,
  "smtp_security": "starttls",
  "smtp_allow_insecure": false,
  "sieve_host": null,
  "sieve_port": 4190,
  "sieve_security": "starttls",
  "sieve_allow_insecure": false,
  "caldav_url": "https://mail.example.com/dav",
  "carddav_url": "https://mail.example.com/dav",
  "dav_allow_insecure": false
}
```

Sieve fields are optional — `sieve_host` defaults to the IMAP host, port 4190 with STARTTLS.
CalDAV/CardDAV URLs are optional — autodiscovery tries `.well-known` endpoints automatically.

### Security Recommendations

- Use **app-specific passwords** when your mail server supports them
- Set `*_allow_insecure: true` only for self-signed certificates on trusted networks
- The `accounts.json` file contains credentials — keep it private

## Usage

### Slash Commands

- `/email-add-account` — add a new account (runs autodiscovery automatically)
- `/email-remove-account` — remove an account
- `/email-list-accounts` — show all configured accounts

### Natural Language

Just ask Claude things like:

- "Check my work email"
- "Search for emails from alice about the invoice"
- "Send an email to bob@example.com about the meeting tomorrow"
- "Reply to that last message and say I'll be there"
- "Forward this to the team"
- "Create a folder called Archive/2026"
- "Show me my Sieve filters"
- "Create a filter to move GitHub notifications to a GitHub folder"
- "Set up a vacation auto-reply for next week"
- "What's on my calendar this week?"
- "Create a meeting for tomorrow at 2pm"
- "Find Bob's phone number in my contacts"
- "Add a new contact for Alice"

### Available MCP Tools

#### Email (14 tools)

| Tool | Description |
|------|-------------|
| `email_autodiscover` | Auto-detect IMAP/SMTP/CalDAV/CardDAV settings from an email address |
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

#### Sieve (6 tools)

| Tool | Description |
|------|-------------|
| `email_sieve_list` | List Sieve filter scripts |
| `email_sieve_get` | Retrieve a script's content |
| `email_sieve_put` | Upload (create/replace) a Sieve script |
| `email_sieve_activate` | Set active filter or deactivate all |
| `email_sieve_delete` | Delete a Sieve script |
| `email_sieve_rename` | Rename a Sieve script |

#### CalDAV (5 tools)

| Tool | Description |
|------|-------------|
| `cal_list_calendars` | List all calendars |
| `cal_list_events` | List events (optional date range) |
| `cal_get_event` | Get full event details |
| `cal_create_event` | Create a new event |
| `cal_update_event` | Update an existing event |
| `cal_delete_event` | Delete an event |

#### CardDAV (6 tools)

| Tool | Description |
|------|-------------|
| `card_list_addressbooks` | List all address books |
| `card_list_contacts` | List contacts in an address book |
| `card_search_contacts` | Search contacts by name or email |
| `card_get_contact` | Get full contact details |
| `card_create_contact` | Create a new contact |
| `card_update_contact` | Update an existing contact |
| `card_delete_contact` | Delete a contact |

## Security

- Autodiscovery (Mozilla autoconfig, Microsoft Autodiscover, well-known DAV) and all CardDAV requests verify TLS certificates and pass every URL — including each redirect hop — through an SSRF guard that refuses connections to loopback, RFC1918, link-local, multicast, reserved, and known cloud-metadata addresses.
- Set `MULTI_MAIL_ALLOW_PRIVATE_AUTODISCOVER=1` only on lab networks with a known autodiscovery endpoint.
- XML responses from untrusted servers are parsed with `defusedxml`, refusing entity-expansion / billion-laughs payloads.
- Account passwords are still stored in plaintext in `~/.claude/multi-mail-accounts.json`. Migration to OS keychain (`keyring`) is tracked for a future release. The plugin now enforces `0600` on the file and `0700` on its parent directory on POSIX systems; if you edited the file before v0.3.5, double-check the modes manually.
- Inbound HTML email bodies are still returned to the model verbatim — treat them as untrusted input. A forthcoming release will strip HTML and prefix with an "untrusted content" delimiter.

## Test Coverage

The badge at the top of this README reflects the latest coverage run from
[Codecov](https://app.codecov.io/gh/disko/multi-mail). Coverage is intentionally
being increased phase by phase, starting with the modules that have the highest
blast radius (security, autodiscovery, server interaction).

| Module | Coverage | Status |
|--------|----------|--------|
| `servers/_security.py` | 96% | SSRF guard, redirect hook, DAV URL pinning — covered |
| `servers/email_mcp.py` | 30% | Pure helpers, account file IO, outbound message assembly, and vCard/iCal formatters covered; IMAP/DAV read paths still need work |

Coverage roadmap:

1. ✅ **Account management** — `_load_accounts`, `_save_accounts`, `_get_account` (`tests/test_account_io.py`).
2. ✅ **Message formatting** — `_build_message`, `_send_message` recipient/Sent fan-out (`tests/test_message_building.py`).
3. ✅ **vCard / iCal formatting** — `_format_event`, `_format_contact` against canned fixtures (`tests/test_dav_formatting.py`).
4. **IMAP search syntax** — string-builders for search queries (pure).
5. **Tool integration** — mocked IMAP/SMTP/DAV via `aioresponses` / `pytest-imap-server` once the unit floor is solid.

To run coverage locally:

```bash
uv run pytest tests/ --cov --cov-report=term --cov-report=html
open htmlcov/index.html
```

## Claude Desktop Bundle

Claude Desktop installs personal plugins as `.mcpb` bundles, not from a marketplace. To produce one:

```bash
bash scripts/pack-mcpb.sh
```

This reads `manifest.json`, packs the tree (minus `.mcpbignore` entries), and writes `.mcpb-cache/multi-mail-<version>.mcpb`. Drag the resulting file onto Claude Desktop → Customize → Personal plugins (or use the ⋮ menu on an existing entry to replace).

Tagged releases (`vX.Y.Z`) build the bundle in CI (`.github/workflows/release.yml`) and attach it to the GitHub release.

## Changelog

### 0.3.6 — calendar UID fix + DAV formatter tests

- **Fixed:** `_format_event` returned the literal string `"{''}"` as the UID for any iCalendar event missing a UID property. The default for `getattr` was written as a set literal `{getattr(...)}` instead of a string. Now defaults to `""` cleanly.
- **Added:** `tests/test_dav_formatting.py` covering full / minimal / UID-less / parse-error paths for `_format_event` and `_format_contact` (multiple emails/tels, missing optional fields).
- **Coverage:** 31% → 33%.

### 0.3.5 — accounts.json permissions

- **Fixed (security):** `_save_accounts` wrote `accounts.json` (containing plaintext credentials) with the process umask — typically `0644`, world-readable. The file and its parent directory are now created with `0o600` / `0o700` respectively on POSIX systems via `os.open(..., 0o600)` + an explicit `chmod`. No change on Windows (rely on the user profile ACL).
- **Added:** `tests/test_account_io.py` — round-trip persistence, parent-directory creation, permission enforcement, and `_get_account` lookup/not-found paths.

### 0.3.4 — header decoding + helper coverage

- **Fixed:** `_decode_header` produced doubled spaces in mixed encoded/plain headers (`"Hällo  World"`) because the segment join inserted a space on top of segment-internal whitespace. Now concatenates without inserting.
- **Fixed:** `_decode_header` crashed with `LookupError` when an email header declared an unknown charset (common in spam). Now falls back to UTF-8 with replacement.
- **Added:** Unit tests for `_decode_header`, `_get_body`, `_summarise_msg`, `_domain_from_email`, and `_map_socket_type` (`tests/test_helpers.py`). Coverage 23% → 27%.
- **Added:** "Test Coverage" section in README with per-module breakdown and roadmap.

### 0.3.3 — CardDAV host pinning

- **Fixed (security):** CardDAV `<href>` elements returned by the server were used directly to build authenticated PUT/DELETE/REPORT requests. A compromised or MITM'd DAV server could return a cross-origin href and trick the client into sending HTTP Basic credentials to an attacker. The SSRF guard did not catch this because the attacker host is a normal public IP. The new `resolve_dav_url()` helper resolves every href against the configured `carddav_url` base and rejects cross-host results.
- **Added:** Regression tests for the host pin (`tests/test_dav_url_pinning.py`).

### 0.3.2 — Sieve fix

- **Fixed:** ManageSieve connections failed with `[Errno 61] Connection refused` for every account. `_sieve_connect` used `acct.get("sieve_host", acct["imap_host"])`, which returns `None` (not the IMAP host) when the saved JSON has `"sieve_host": null` — the schema's default. `socket.create_connection((None, 4190))` then dialed localhost. Now falls back via truthy-OR. Same fix applied to `sieve_port`, `sieve_security`, and `sieve_allow_insecure`.
- **Added:** Regression tests for Sieve parameter resolution (`tests/test_sieve_config.py`).
- **Added:** `LICENSE` (MIT) and `SECURITY.md`.

### 0.3.1 — packaging

- **Added:** Claude Desktop `.mcpb` bundle (`manifest.json`, `scripts/pack-mcpb.sh`).
- **Added:** GitHub Actions workflow that detects version bumps and publishes a release with the bundle attached.

### 0.3.0 — security release

- **Fixed (security):** Autodiscovery and CardDAV requests now verify TLS certificates by default. Previous releases hardcoded `verify=False`, letting on-path attackers forge XML to redirect IMAP/SMTP/DAV traffic to malicious hosts and harvest credentials.
- **Fixed (security):** Added an SSRF guard that resolves every requested host (and each redirect hop) and refuses connections to loopback, RFC1918, link-local, multicast, reserved, and known cloud-metadata addresses.
- **Fixed (security):** XML parsing of untrusted server responses uses `defusedxml`.
- **Fixed (data loss):** `email_move_message` uses RFC 4315 UIDPLUS `UID EXPUNGE` instead of a bare `EXPUNGE`. The previous behaviour would remove every `\Deleted` message in the source folder. If the server doesn't advertise UIDPLUS, the move is refused after the copy.
- **Fixed (perf):** Blocking IMAP / SMTP / ManageSieve work no longer freezes the FastMCP event loop — each affected tool body now runs in `asyncio.to_thread`.
- **Added:** pytest suite for the SSRF guard, the redirect hook, and the autodiscover XML parsers (incl. a billion-laughs fixture).

### 0.2.0

Initial release.
