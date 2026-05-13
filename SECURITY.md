# Security Policy

Multi-mail handles email credentials and reaches out to user-controlled mail
servers, so security reports are taken seriously.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for suspected vulnerabilities.

Report privately via [GitHub Security
Advisories](https://github.com/disko/multi-mail/security/advisories/new)
("Report a vulnerability" tab on the Security page). A maintainer will respond
within 7 days; expect a fix or mitigation within 30 days for confirmed issues.

## Scope

In scope:

- Credential exposure (account passwords, session tokens) leaking via the MCP
  surface, logs, error messages, or accounts file
- SSRF, command injection, path traversal, or other injection attacks via tool
  arguments
- TLS verification bypasses or downgrade attacks on IMAP / SMTP / ManageSieve /
  CalDAV / CardDAV connections
- Parser issues against untrusted server responses (XML, vCard, iCalendar,
  Sieve)
- Logic bugs that cause data loss (e.g. unintended message deletion)

Out of scope:

- The host operating system's storage of `~/.claude/multi-mail-accounts.json`
  (use file permissions / disk encryption)
- Vulnerabilities in upstream dependencies — please report those upstream
  (`mcp`, `httpx`, `caldav`, `managesieve`, `vobject`, `defusedxml`, `pydantic`)
- Attacks requiring physical access to the user's machine

## Hardening Already in Place

See the README's "Security" section for the threat model and existing
mitigations (SSRF guard, TLS verification, `defusedxml` XML parsing, UIDPLUS
move). Known limitations are documented there.
