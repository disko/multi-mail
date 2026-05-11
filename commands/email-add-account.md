---
description: Add a new IMAP/SMTP email account (with autodiscovery)
allowed-tools: ["mcp__multi_mail__email_autodiscover", "mcp__multi_mail__email_add_account", "mcp__multi_mail__email_list_accounts"]
---

The user wants to add a new email account. Follow this flow:

## Step 1: Gather basics

Ask for:
1. **Email address** — their full email address
2. **Account id** — a short unique name like "work" or "personal"

## Step 2: Autodiscover server settings

Call `email_autodiscover` with the email address. This will try:
- Mozilla autoconfig (used by Mailcow, Stalwart, etc.)
- Microsoft/Mailcow Autodiscover (POX protocol)
- DNS SRV records

If autodiscovery succeeds, present the discovered settings and ask the user
to confirm they look correct. Pre-fill all fields from the discovery results.

If autodiscovery fails, tell the user and fall back to asking for manual
server details (see Step 3).

## Step 3: Manual fallback (only if autodiscovery failed or user overrides)

Gather these details by asking interactively:
- **IMAP host and port** — server hostname, default port 993
- **IMAP security** — ssl (default), starttls, or none
- **SMTP host and port** — server hostname, default port 587
- **SMTP security** — starttls (default), ssl, or none

Optional:
- **Display name** — friendly name for the account
- **Allow insecure** — skip TLS cert verification (for self-signed certs)
- **CalDAV URL** — CalDAV server URL (autodiscovery tries `.well-known/caldav`)
- **CardDAV URL** — CardDAV server URL (autodiscovery tries `.well-known/carddav`)

## Step 4: Credentials

Ask for:
- **Username** — login username (suggest the autodiscovered username or the email address)
- **Password** — their password or app password

## Step 5: Save

Call `email_add_account` with all collected settings.
Then call `email_list_accounts` to confirm the account appears.

If the user provides all information at once (e.g. in $ARGUMENTS), parse it and
proceed directly — still run autodiscovery to fill in any gaps.
