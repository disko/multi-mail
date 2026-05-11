---
name: email-workflows
description: >
  This skill should be used when the user asks to "check my email",
  "send an email", "read my messages", "search email", "reply to",
  "forward", "manage email folders", "list accounts", "sieve filter",
  "mail filter", "mail rule", "filter rule", "check my calendar",
  "list events", "create event", "list contacts", "search contacts",
  "add contact", or otherwise work with their IMAP/SMTP email accounts,
  CalDAV calendars, or CardDAV contacts. It provides guidance on composing
  effective searches, managing multiple accounts, managing server-side
  Sieve filter rules, working with calendars and contacts, and handling
  common email workflows.
version: 0.2.0
---

# Multi-Mail Workflows

Guide for working with multiple self-hosted mail accounts — email (IMAP/SMTP), Sieve filters, CalDAV calendars, and CardDAV contacts.

## Available Tools

### Email tools (prefixed `email_`)

- **email_autodiscover** — auto-detect IMAP/SMTP/CalDAV/CardDAV settings from an email address (Mozilla autoconfig, Microsoft/Mailcow autodiscover, DNS SRV, .well-known)
- **email_list_accounts** — show configured accounts (ids, hosts, addresses)
- **email_add_account** / **email_remove_account** — manage accounts dynamically
- **email_list_folders** — list IMAP folders for an account
- **email_create_folder** / **email_delete_folder** — manage IMAP folders
- **email_list_messages** — list recent messages (paginated)
- **email_search_messages** — IMAP SEARCH with criteria
- **email_read_message** — read full message by UID
- **email_send_message** — compose and send a new email
- **email_reply** — reply (or reply-all) to a message
- **email_forward** — forward a message to new recipients
- **email_move_message** — move a message between folders

### Sieve tools (prefixed `email_sieve_`)

- **email_sieve_list** — list Sieve filter scripts on the server
- **email_sieve_get** — retrieve a Sieve script's content
- **email_sieve_put** — upload (create/replace) a Sieve script
- **email_sieve_activate** — set a script as the active filter (or deactivate all)
- **email_sieve_delete** — delete a Sieve script
- **email_sieve_rename** — rename a Sieve script

### CalDAV tools (prefixed `cal_`)

- **cal_list_calendars** — list all calendars for an account
- **cal_list_events** — list events in a calendar (optional date range)
- **cal_get_event** — get full details of a single event by UID
- **cal_create_event** — create a new calendar event
- **cal_update_event** — update an existing event
- **cal_delete_event** — delete an event

### CardDAV tools (prefixed `card_`)

- **card_list_addressbooks** — list all address books for an account
- **card_list_contacts** — list contacts in an address book
- **card_search_contacts** — search contacts by name or email
- **card_get_contact** — get full vCard details of a contact
- **card_create_contact** — create a new contact
- **card_update_contact** — update an existing contact
- **card_delete_contact** — delete a contact

## Workflow: Checking Mail

1. Call `email_list_accounts` to confirm which account to use.
2. Call `email_list_messages` with the account id and folder (default INBOX).
3. Summarise what arrived — group by sender or topic if helpful.
4. For any message the user wants to read, call `email_read_message` with the UID.

## Workflow: Searching

Use `email_search_messages` with IMAP SEARCH syntax. Common patterns:

| Goal | Query |
|------|-------|
| From a person | `FROM "alice@example.com"` |
| By subject | `SUBJECT "invoice"` |
| Unread only | `UNSEEN` |
| Since a date | `SINCE 01-Mar-2026` |
| Flagged | `FLAGGED` |
| Combined | `FROM "bob" SINCE 01-Jan-2026 UNSEEN` |
| OR logic | `OR FROM "bob" SUBJECT "urgent"` |

Dates use `DD-Mon-YYYY` format (e.g., `01-Jan-2026`).

## Workflow: Sending

1. Confirm which account to send from.
2. Call `email_send_message` with to, subject, and body.
3. CC and BCC are optional.
4. The message is automatically saved to the Sent folder.

## Workflow: Reply / Forward

- **Reply**: call `email_reply` with the UID. Set `reply_all: true` to include all original recipients.
- **Forward**: call `email_forward` with the UID and new recipient(s). Optionally include a note.

## Workflow: Adding an Account

1. Ask the user for their email address.
2. Call `email_autodiscover` to auto-detect IMAP/SMTP/CalDAV/CardDAV settings.
3. If autodiscovery succeeds, present the settings for confirmation.
4. Ask for username (suggest the autodiscovered one or the email address) and password.
5. Call `email_add_account` with all settings.
6. If autodiscovery fails, fall back to asking for server details manually.

Autodiscovery supports Mozilla autoconfig (Mailcow, Stalwart, standard ISPs),
Microsoft Autodiscover (Exchange, Mailcow), DNS SRV records, and `.well-known`
CalDAV/CardDAV endpoints (RFC 6764).

## Workflow: Sieve Filters

Sieve is a server-side mail filtering language (RFC 5228). ManageSieve (RFC 5804,
port 4190) is the protocol for managing Sieve scripts remotely.

### Listing and viewing filters

1. Call `email_sieve_list` to see all scripts and which is active.
2. Call `email_sieve_get` to read a script's content.

### Creating a filter

1. Write the Sieve script content. The server validates syntax on upload.
2. Call `email_sieve_put` with the script name and content.
3. Set `activate: true` to make it the active filter immediately.
4. If the server rejects the script, it returns a syntax error — fix and retry.

### Common Sieve patterns

Move mail from a sender to a folder:
```sieve
require ["fileinto"];
if address :is "from" "notifications@github.com" {
  fileinto "GitHub";
}
```

Discard spam with a subject keyword:
```sieve
require ["reject"];
if header :contains "subject" "win a prize" {
  discard;
}
```

Vacation auto-reply:
```sieve
require ["vacation"];
vacation :days 7 :subject "Out of office"
  "I'm currently away. I'll respond when I return.";
```

Redirect a copy to another address:
```sieve
require ["copy"];
redirect :copy "backup@example.com";
```

### Activating / deactivating

- Only one script can be active at a time.
- Call `email_sieve_activate` with a script name to switch.
- Call `email_sieve_activate` with an empty `script_name` to disable all filtering.
- You cannot delete the active script — deactivate it first.

### Connection details

ManageSieve defaults to the IMAP host on port 4190 with STARTTLS.
Override per account with `sieve_host`, `sieve_port`, `sieve_security` fields.

For Sieve language reference, see `references/sieve-language.md`.

## Workflow: CalDAV Calendars

CalDAV requires a `caldav_url` set on the account. Autodiscovery tries
`.well-known/caldav` on the mail server automatically.

### Listing calendars and events

1. Call `cal_list_calendars` to see all calendars for an account.
2. Call `cal_list_events` with an optional date range (`start` / `end` in ISO 8601).
3. Call `cal_get_event` with a UID for full event details.

### Creating events

Call `cal_create_event` with:
- **summary** (required) — event title
- **dtstart** — start datetime in ISO 8601 (e.g., `2026-03-15T10:00:00`)
- **dtend** — end datetime
- **description** — event description/notes
- **location** — event location
- **calendar_name** — which calendar to use (defaults to the first one)

### Updating events

Call `cal_update_event` with the event UID and any fields to change
(summary, dtstart, dtend, description, location).

### Deleting events

Call `cal_delete_event` with the event UID.

### Date formats

All dates use ISO 8601 format: `YYYY-MM-DDTHH:MM:SS` (e.g., `2026-03-15T14:30:00`).
For all-day events, use just the date: `2026-03-15`.

## Workflow: CardDAV Contacts

CardDAV requires a `carddav_url` set on the account. Autodiscovery tries
`.well-known/carddav` on the mail server automatically.

### Listing contacts

1. Call `card_list_addressbooks` to see all address books.
2. Call `card_list_contacts` to list contacts in an address book (defaults to the first one).

### Searching contacts

Call `card_search_contacts` with a query string. It searches across names and email addresses.

### Creating contacts

Call `card_create_contact` with:
- **full_name** (required)
- **email** — email address
- **phone** — phone number
- **organization** — company/org name
- **addressbook_name** — which address book (defaults to the first one)

### Updating contacts

Call `card_update_contact` with the contact href and any fields to update
(full_name, email, phone, organization).

### Deleting contacts

Call `card_delete_contact` with the contact href.

## Multi-Account Tips

- Always clarify which account the user means if they have more than one.
- When listing messages across accounts, iterate over each account and present results grouped by account.
- Account ids are short strings like "work" or "personal" — use these in all tool calls.

## Security Notes

- Passwords are stored in the local accounts.json config file. Never display them.
- When adding accounts, remind the user to use app-specific passwords where available.
- The `allow_insecure` flags skip TLS verification — only use for self-signed certs on trusted networks.

## Reference

- IMAP SEARCH syntax: `references/imap-search-syntax.md`
- Sieve language: `references/sieve-language.md`
