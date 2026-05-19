#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "mcp",
#     "pydantic",
#     "httpx",
#     "managesieve",
#     "caldav",
#     "vobject",
#     "defusedxml>=0.7.1",
# ]
# ///
"""
MCP Server for multi-account IMAP/SMTP email.

Connects to multiple self-hosted mail servers, supporting read, search,
send, reply, forward, and folder management across all configured accounts.
Accounts are stored in a JSON config file and can be added/removed dynamically.
"""

import asyncio
import email
import email.header
import email.mime.multipart
import email.mime.text
import email.utils
import imaplib
import json
import os
import re
import smtplib
import socket
import ssl
from defusedxml import ElementTree as ET
from xml.etree.ElementTree import Element as _XmlElement  # type-only; parsing goes through defusedxml
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import caldav
import httpx
import managesieve as ms
import vobject
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from _security import resolve_dav_url, safe_async_client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = os.environ.get(
    "EMAIL_ACCOUNTS_CONFIG",
    os.path.join(os.path.dirname(__file__), "accounts.json"),
)

mcp = FastMCP("email_mcp")


# ---------------------------------------------------------------------------
# Account storage helpers
# ---------------------------------------------------------------------------

def _load_accounts() -> List[Dict[str, Any]]:
    """Load accounts from the JSON config file."""
    path = Path(CONFIG_PATH).expanduser()
    if not path.exists():
        return []
    with open(path, "r") as f:
        data = json.load(f)
    return data.get("accounts", [])


def _save_accounts(accounts: List[Dict[str, Any]]) -> None:
    """Persist accounts to the JSON config file.

    Enforces ``0700`` on the parent directory and ``0600`` on the file because
    the JSON contains plaintext credentials. POSIX-only; on Windows the chmod
    is a no-op and you should rely on the user profile ACL.
    """
    path = Path(CONFIG_PATH).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 0o700 on the dir / 0o600 on the file is owner-only access. semgrep's
    # default-permission rule treats this as "widely permissive" — wrong here:
    # the file holds plaintext credentials and the user is the only legitimate
    # reader. See SECURITY.md.
    if os.name == "posix":
        try:
            os.chmod(path.parent, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        except OSError:
            pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"accounts": accounts}, f, indent=2)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _get_account(account_id: str) -> Dict[str, Any]:
    """Look up a single account by its id. Raises ValueError if not found."""
    for acct in _load_accounts():
        if acct["id"] == account_id:
            return acct
    raise ValueError(
        f"Account '{account_id}' not found. "
        "Use email_list_accounts to see available accounts."
    )


# ---------------------------------------------------------------------------
# IMAP / SMTP connection helpers
# ---------------------------------------------------------------------------

def _imap_connect(acct: Dict[str, Any]) -> imaplib.IMAP4:
    """Return an authenticated IMAP connection for *acct*."""
    host = acct["imap_host"]
    port = acct.get("imap_port", 993)
    security = acct.get("imap_security", "ssl").lower()

    if security == "ssl":
        ctx = ssl.create_default_context()
        if acct.get("imap_allow_insecure"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ctx)
    elif security == "starttls":
        conn = imaplib.IMAP4(host, port)
        ctx = ssl.create_default_context()
        if acct.get("imap_allow_insecure"):
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        conn.starttls(ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(host, port)

    conn.login(acct["username"], acct["password"])
    return conn


def _smtp_connect(acct: Dict[str, Any]) -> smtplib.SMTP:
    """Return an authenticated SMTP connection for *acct*."""
    host = acct["smtp_host"]
    port = acct.get("smtp_port", 587)
    security = acct.get("smtp_security", "starttls").lower()

    ctx = ssl.create_default_context()
    if acct.get("smtp_allow_insecure"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    if security == "ssl":
        conn = smtplib.SMTP_SSL(host, port, context=ctx)
    else:
        conn = smtplib.SMTP(host, port)
        conn.ehlo()
        if security == "starttls":
            conn.starttls(context=ctx)
            conn.ehlo()

    conn.login(acct["username"], acct["password"])
    return conn


def _resolve_sieve_params(acct: Dict[str, Any]) -> Tuple[str, int, str, bool]:
    """Return (host, port, security, allow_insecure) for a ManageSieve connection.

    Treats explicit ``None`` for ``sieve_host`` (the schema default) the same as
    a missing key — falls back to the IMAP host. ``dict.get(key, default)`` does
    NOT do that: it returns ``None`` when the key exists with a null value, so
    ``socket.create_connection`` ends up dialing localhost and the user sees
    a misleading ``Connection refused``.
    """
    host = acct.get("sieve_host") or acct["imap_host"]
    port = acct.get("sieve_port") or 4190
    security = (acct.get("sieve_security") or "starttls").lower()
    allow_insecure = acct.get("sieve_allow_insecure")
    if allow_insecure is None:
        allow_insecure = acct.get("imap_allow_insecure", False)
    return host, port, security, allow_insecure


def _sieve_connect(acct: Dict[str, Any]) -> ms.MANAGESIEVE:
    """Return an authenticated ManageSieve connection for *acct*.

    Errors surface a small diagnostic blob — which security mode was used,
    the host:port, and whether the server advertised any SASL mechanisms —
    so users don't have to read the managesieve library source to figure out
    why ``No matching authentication mechanism found`` fired.
    """
    host, port, security, allow_insecure = _resolve_sieve_params(acct)
    use_tls = security == "starttls"

    def _ctx() -> str:
        mechs = getattr(conn, "loginmechs", [])
        return (
            f"sieve://{host}:{port} security={security!r} "
            f"tls_verify={not allow_insecure} "
            f"server_mechanisms={mechs or 'none advertised'}"
        )

    conn = ms.MANAGESIEVE(
        host,
        port=port,
        use_tls=use_tls,
        tls_verify=not allow_insecure,
        timeout=30,
    )

    if not conn.loginmechs:
        # Empty mechanism list after construction means either (a) security
        # is "none" / wrong and we're sitting on a plaintext connection where
        # the server refuses to advertise SASL until STARTTLS, or (b) the
        # server failed to re-advertise post-STARTTLS. Either way, an
        # AUTHENTICATE call is doomed.
        hint = ""
        if security != "starttls":
            hint = (
                f" The account has sieve_security={security!r}; most servers "
                f"require STARTTLS before advertising SASL mechanisms. Try "
                f"setting sieve_security to \"starttls\" in accounts.json."
            )
        raise ConnectionError(
            f"ManageSieve server advertised no SASL mechanisms — cannot "
            f"authenticate.{hint} [{_ctx()}]"
        )

    try:
        result = conn.login("PLAIN", acct["username"], acct["password"])
    except ms.MANAGESIEVE.error as e:
        raise ConnectionError(
            f"ManageSieve authentication error: {e}. [{_ctx()}]"
        ) from e
    if result != "OK":
        raise ConnectionError(
            f"ManageSieve authentication failed (server returned {result!r}). "
            f"Check that the username/password match what works for IMAP. "
            f"[{_ctx()}]"
        )
    return conn


def _caldav_client(acct: Dict[str, Any]) -> caldav.DAVClient:
    """Return an authenticated CalDAV client for *acct*."""
    url = acct.get("caldav_url")
    if not url:
        raise ValueError(
            f"No CalDAV URL configured for account '{acct['id']}'. "
            "Set caldav_url in the account config or re-run autodiscovery."
        )
    ssl_verify = not acct.get("dav_allow_insecure", False)
    return caldav.DAVClient(
        url=url,
        username=acct["username"],
        password=acct["password"],
        ssl_verify_cert=ssl_verify,
    )


def _carddav_headers(acct: Dict[str, Any]) -> Tuple[str, Dict[str, str], httpx.BasicAuth]:
    """Return (base_url, headers, auth) for CardDAV requests."""
    url = acct.get("carddav_url")
    if not url:
        raise ValueError(
            f"No CardDAV URL configured for account '{acct['id']}'. "
            "Set carddav_url in the account config or re-run autodiscovery."
        )
    auth = httpx.BasicAuth(acct["username"], acct["password"])
    headers = {"Content-Type": "application/xml; charset=utf-8"}
    return url, headers, auth


# ---------------------------------------------------------------------------
# CalDAV / iCalendar helpers
# ---------------------------------------------------------------------------

def _format_event(event: caldav.Event) -> Dict[str, str]:
    """Extract a summary dict from a caldav Event."""
    try:
        vcal = vobject.readOne(event.data)
        vevent = vcal.vevent
        result = {
            "uid": "",
            "summary": str(getattr(vevent.summary, "value", "")) if hasattr(vevent, "summary") else "",
            "dtstart": "",
            "dtend": "",
            "location": str(getattr(vevent.location, "value", "")) if hasattr(vevent, "location") else "",
            "description": str(getattr(vevent.description, "value", "")) if hasattr(vevent, "description") else "",
        }
        if hasattr(vevent, "uid"):
            result["uid"] = str(vevent.uid.value)
        if hasattr(vevent, "dtstart"):
            result["dtstart"] = str(vevent.dtstart.value)
        if hasattr(vevent, "dtend"):
            result["dtend"] = str(vevent.dtend.value)
        return result
    except Exception:
        return {"uid": "", "summary": "(parse error)", "raw": str(event.data)[:200]}


def _format_contact(vcard_data: str) -> Dict[str, str]:
    """Extract a summary dict from a vCard string."""
    try:
        vc = vobject.readOne(vcard_data)
        result: Dict[str, str] = {}
        result["uid"] = str(vc.uid.value) if hasattr(vc, "uid") else ""
        result["fn"] = str(vc.fn.value) if hasattr(vc, "fn") else ""
        if hasattr(vc, "email"):
            emails = vc.contents.get("email", [])
            result["email"] = ", ".join(str(e.value) for e in emails)
        if hasattr(vc, "tel"):
            tels = vc.contents.get("tel", [])
            result["tel"] = ", ".join(str(t.value) for t in tels)
        if hasattr(vc, "org"):
            result["org"] = str(vc.org.value[0]) if vc.org.value else ""
        if hasattr(vc, "title"):
            result["title"] = str(vc.title.value)
        return result
    except Exception:
        return {"fn": "(parse error)", "raw": vcard_data[:200]}


# ---------------------------------------------------------------------------
# Mail parsing helpers
# ---------------------------------------------------------------------------

def _decode_header(raw: Optional[str]) -> str:
    """Decode an RFC-2047 encoded header value.

    Falls back to UTF-8 for unknown charsets so a spammy header doesn't crash
    the whole tool call.
    """
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            try:
                decoded.append(data.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(data.decode("utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def _get_body(msg: email.message.Message) -> str:
    """Extract a plain-text body from *msg*, falling back to stripped HTML."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # fallback: first text/html
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _summarise_msg(msg: email.message.Message, uid: str) -> Dict[str, str]:
    """Return a compact dict summarising a message (no body)."""
    return {
        "uid": uid,
        "from": _decode_header(msg.get("From")),
        "to": _decode_header(msg.get("To")),
        "subject": _decode_header(msg.get("Subject")),
        "date": _decode_header(msg.get("Date")),
        "message_id": msg.get("Message-ID", ""),
    }


def _parse_imap_list_line(item: Any) -> Optional[str]:
    """Extract the mailbox name from a single ``IMAP4.list()`` response item.

    RFC 3501 §9 allows the mailbox name in a ``LIST`` response to be:
      - a quoted string:  ``(\\HasNoChildren) "/" "INBOX"``
      - an atom:          ``(\\HasNoChildren) "/" INBOX``  (Dovecot/mailcow do this)
      - a literal:        ``(\\HasNoChildren) "/" {5}\\r\\nINBOX``
    imaplib returns literal-form responses as a tuple ``(header_bytes,
    literal_bytes)``; everything else arrives as a single bytes object.

    Returns the mailbox name as a ``str``, or ``None`` if the line is empty
    or unparseable (caller should skip it).
    """
    # Literal-form: imaplib already separated the literal payload.
    if isinstance(item, tuple):
        if len(item) >= 2 and item[1] is not None:
            try:
                return item[1].decode("utf-8", errors="replace")
            except (AttributeError, UnicodeDecodeError):
                return None
        return None

    if not isinstance(item, (bytes, bytearray)):
        return None

    text = bytes(item).decode("utf-8", errors="replace").strip()
    if not text:
        return None

    # Skip the leading parenthesised flags group: "(\HasNoChildren \Marked)".
    if text.startswith("("):
        depth = 0
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    text = text[i + 1 :].lstrip()
                    break
        else:
            # Unbalanced parens — give up rather than emit garbage.
            return None

    # Next token is the delimiter: either a quoted string or the atom NIL.
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return None
        text = text[end + 1 :].lstrip()
    else:
        # Atom delimiter (e.g. NIL). Consume until next whitespace.
        parts = text.split(None, 1)
        if len(parts) < 2:
            return None
        text = parts[1].lstrip()

    if not text:
        return None

    # Finally, the mailbox name: quoted string or atom (rest of the line).
    if text.startswith('"'):
        end = text.find('"', 1)
        if end == -1:
            return None
        return text[1:end]
    # Atom: per RFC 3501 §9, atoms contain no whitespace; take the last
    # token to be defensive against trailing whitespace/CRLF residue.
    tokens = text.split()
    if not tokens:
        return None
    return tokens[-1]


def _resolve_trash_folder(
    conn: Any,
    acct: Dict[str, Any],
    override: Optional[str] = None,
) -> str:
    """Resolve the Trash folder name for a delete-to-trash workflow.

    Priority chain:
      1. ``override`` (per-tool-call ``trash_folder`` param).
      2. ``acct['trash_folder']`` — per-account config. Treats explicit
         ``None`` as missing (CLAUDE.md Gotcha #1: Pydantic serialises
         ``Optional[str] = None`` as ``null``; ``dict.get(k, default)``
         would return that ``None`` instead of the fallback).
      3. ``\\Trash`` SPECIAL-USE flag from the server's LIST response
         (RFC 6154). Substring-scan on the header bytes — the flag group
         lives in the ``(\\HasNoChildren \\Trash) "/" "Name"`` prefix that
         ``_parse_imap_list_line`` otherwise discards. Literal-form LIST
         tuples (header bytes + literal name bytes) are out of scope here:
         their flag group sits in the header tuple element which we don't
         currently re-scan. Revisit if a user reports it.
      4. Hard-coded default: ``"Trash"``.
    """
    if override:
        return override
    configured = acct.get("trash_folder")
    if configured:
        return configured
    try:
        status, data = conn.list()
        if status == "OK" and data:
            for item in data:
                if isinstance(item, (bytes, bytearray)):
                    text = bytes(item).decode("utf-8", errors="replace")
                elif isinstance(item, tuple) and item and isinstance(item[0], (bytes, bytearray)):
                    text = bytes(item[0]).decode("utf-8", errors="replace")
                else:
                    continue
                if "\\Trash" in text:
                    name = _parse_imap_list_line(item)
                    if name:
                        return name
    except Exception:
        # Graceful degradation — pinned by the hardcoded-fallback test.
        pass
    return "Trash"


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------

class AccountIdMixin(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    account_id: str = Field(
        ...,
        description="Account identifier (use email_list_accounts to see ids)",
        min_length=1,
    )


class ListFoldersInput(AccountIdMixin):
    """Input for listing IMAP folders."""
    pass


class ListEmailsInput(AccountIdMixin):
    """Input for listing emails in a folder."""
    folder: str = Field(default="INBOX", description="IMAP folder name")
    limit: int = Field(default=20, description="Max messages to return", ge=1, le=200)
    offset: int = Field(default=0, description="Number of messages to skip (from most recent)", ge=0)


class SearchEmailsInput(AccountIdMixin):
    """Input for searching emails via IMAP SEARCH."""
    query: str = Field(
        ...,
        description=(
            "IMAP search query. Examples: "
            "'FROM \"alice@example.com\"', "
            "'SUBJECT \"invoice\"', "
            "'SINCE 01-Jan-2025', "
            "'UNSEEN', "
            "'OR FROM \"bob\" SUBJECT \"urgent\"'"
        ),
        min_length=1,
    )
    folder: str = Field(default="INBOX", description="IMAP folder to search in")
    limit: int = Field(default=20, description="Max results", ge=1, le=200)


class ReadEmailInput(AccountIdMixin):
    """Input for reading a single email by UID."""
    uid: str = Field(..., description="Message UID (from list or search results)", min_length=1)
    folder: str = Field(default="INBOX", description="IMAP folder containing the message")
    mark_read: bool = Field(default=True, description="Mark the message as read (\\Seen flag)")


class SendEmailInput(AccountIdMixin):
    """Input for composing and sending a new email."""
    to: str = Field(..., description="Recipient address(es), comma-separated", min_length=1)
    subject: str = Field(..., description="Email subject line")
    body: str = Field(..., description="Plain-text email body", min_length=1)
    cc: Optional[str] = Field(default=None, description="CC address(es), comma-separated")
    bcc: Optional[str] = Field(default=None, description="BCC address(es), comma-separated")


class ReplyEmailInput(AccountIdMixin):
    """Input for replying to an existing email."""
    uid: str = Field(..., description="UID of the message to reply to", min_length=1)
    folder: str = Field(default="INBOX", description="Folder of the original message")
    body: str = Field(..., description="Reply body text", min_length=1)
    reply_all: bool = Field(default=False, description="Reply to all recipients")


class ForwardEmailInput(AccountIdMixin):
    """Input for forwarding an existing email."""
    uid: str = Field(..., description="UID of the message to forward", min_length=1)
    folder: str = Field(default="INBOX", description="Folder of the original message")
    to: str = Field(..., description="Recipient address(es), comma-separated", min_length=1)
    body: Optional[str] = Field(default=None, description="Optional note to prepend")


class MoveEmailInput(AccountIdMixin):
    """Input for moving an email between folders."""
    uid: str = Field(..., description="UID of the message to move", min_length=1)
    source_folder: str = Field(default="INBOX", description="Current folder")
    dest_folder: str = Field(..., description="Destination folder", min_length=1)


class DeleteEmailInput(AccountIdMixin):
    """Input for deleting a message — move to Trash by default, permanent if flagged."""
    uid: str = Field(..., description="UID of the message to delete", min_length=1)
    folder: str = Field(default="INBOX", description="Folder containing the message")
    permanent: bool = Field(
        default=False,
        description=(
            "If True: STORE +\\Deleted + UID EXPUNGE in the source folder. "
            "If False (default): COPY to the resolved Trash folder, then "
            "STORE +\\Deleted + UID EXPUNGE on the original."
        ),
    )
    trash_folder: Optional[str] = Field(
        default=None,
        description=(
            "Override trash folder name. If unset: per-account "
            "`trash_folder` config → SPECIAL-USE \\Trash → 'Trash'."
        ),
    )


class ExpungeInput(AccountIdMixin):
    """Input for issuing a (UID-scoped or bare) EXPUNGE on an IMAP folder."""
    uid: Optional[str] = Field(
        default=None,
        description=(
            "UID to expunge (requires UIDPLUS). If unset, requires "
            "confirm_bare_expunge=True."
        ),
    )
    folder: str = Field(default="INBOX", description="Folder to operate on")
    confirm_bare_expunge: bool = Field(
        default=False,
        description=(
            "Required to issue a bare EXPUNGE (no UID). A bare EXPUNGE "
            "removes EVERY \\Deleted message in the folder."
        ),
    )


# Characters that may not appear inside an IMAP flag atom (RFC 3501 §9
# `atom-specials`). Whitespace, parens, brace literals, and the listed
# control characters all break the wire protocol if sent verbatim.
_FLAG_ATOM_FORBIDDEN = set(" \t\r\n()[]{}\"%*")


def _validate_flag_atom(flag: str) -> str:
    """Validate a single IMAP flag atom; return it unchanged or raise.

    System flags start with ``\\`` (e.g. ``\\Flagged``, ``\\Seen``); custom
    keywords are bare atoms (e.g. ``follow-up``). Both must be non-empty
    and free of IMAP-disallowed characters.
    """
    if not isinstance(flag, str):
        raise ValueError(f"invalid flag atom: {flag!r} is not a string")
    if not flag:
        raise ValueError("invalid flag atom: empty string")
    if flag.startswith("\\") and len(flag) < 2:
        raise ValueError(f"invalid flag atom: {flag!r} is a bare backslash")
    bad = _FLAG_ATOM_FORBIDDEN.intersection(flag)
    if bad:
        raise ValueError(
            f"invalid flag atom {flag!r}: contains disallowed character(s) "
            f"{sorted(bad)!r} (whitespace, parens, braces, quotes, or wildcards)"
        )
    return flag


class ModifyFlagsInput(AccountIdMixin):
    """Input for adding and/or removing IMAP flags on a message.

    Either or both of ``add_flags`` and ``remove_flags`` may be supplied;
    at least one must be non-empty. Each entry must be a valid IMAP flag
    atom (RFC 3501) — a system flag like ``\\Flagged`` / ``\\Seen`` /
    ``\\Answered`` / ``\\Draft``, or a bare custom keyword like
    ``follow-up``. The tool does NOT expunge: setting ``\\Deleted`` here
    sticks until a later move/expunge.
    """
    uid: str = Field(..., description="UID of the message to modify", min_length=1)
    folder: str = Field(default="INBOX", description="Folder containing the message")
    add_flags: List[str] = Field(
        default_factory=list,
        description="Flags to set (e.g. ['\\\\Flagged', '\\\\Answered'])",
    )
    remove_flags: List[str] = Field(
        default_factory=list,
        description="Flags to clear (e.g. ['\\\\Flagged'])",
    )

    @field_validator("add_flags", "remove_flags")
    @classmethod
    def _validate_flag_atoms(cls, v: List[str]) -> List[str]:
        return [_validate_flag_atom(f) for f in v]

    @model_validator(mode="after")
    def _require_at_least_one_flag(self) -> "ModifyFlagsInput":
        if not self.add_flags and not self.remove_flags:
            raise ValueError(
                "at least one of add_flags or remove_flags must be non-empty"
            )
        return self


class CreateFolderInput(AccountIdMixin):
    """Input for creating an IMAP folder."""
    folder: str = Field(..., description="New folder name (use / for hierarchy)", min_length=1)


class DeleteFolderInput(AccountIdMixin):
    """Input for deleting an IMAP folder."""
    folder: str = Field(..., description="Folder to delete", min_length=1)


class AddAccountInput(BaseModel):
    """Input for adding a new email account."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    id: str = Field(..., description="Short unique identifier for this account (e.g. 'work', 'personal')", min_length=1, max_length=64)
    display_name: Optional[str] = Field(default=None, description="Friendly display name")
    email_address: str = Field(..., description="Full email address", min_length=3)
    username: str = Field(..., description="Login username (often the email address)", min_length=1)
    password: str = Field(..., description="Login password or app password", min_length=1)
    imap_host: str = Field(..., description="IMAP server hostname", min_length=1)
    imap_port: int = Field(default=993, description="IMAP port", ge=1, le=65535)
    imap_security: str = Field(default="ssl", description="IMAP security: 'ssl', 'starttls', or 'none'")
    imap_allow_insecure: bool = Field(default=False, description="Skip TLS certificate verification for IMAP")
    smtp_host: str = Field(..., description="SMTP server hostname", min_length=1)
    smtp_port: int = Field(default=587, description="SMTP port", ge=1, le=65535)
    smtp_security: str = Field(default="starttls", description="SMTP security: 'ssl', 'starttls', or 'none'")
    smtp_allow_insecure: bool = Field(default=False, description="Skip TLS certificate verification for SMTP")
    trash_folder: Optional[str] = Field(default=None, description="Override server-side Trash folder name (falls back to SPECIAL-USE \\Trash, then 'Trash')")
    sieve_host: Optional[str] = Field(default=None, description="ManageSieve server hostname (defaults to IMAP host)")
    sieve_port: int = Field(default=4190, description="ManageSieve port", ge=1, le=65535)
    sieve_security: str = Field(default="starttls", description="ManageSieve security: 'starttls' or 'none'")
    sieve_allow_insecure: bool = Field(default=False, description="Skip TLS certificate verification for ManageSieve")
    caldav_url: Optional[str] = Field(default=None, description="CalDAV server URL (e.g. 'https://mail.example.com/SOGo/dav/user/Calendar')")
    carddav_url: Optional[str] = Field(default=None, description="CardDAV server URL (e.g. 'https://mail.example.com/SOGo/dav/user/Contacts')")
    dav_allow_insecure: bool = Field(default=False, description="Skip TLS certificate verification for CalDAV/CardDAV")

    @field_validator("imap_security", "smtp_security", "sieve_security")
    @classmethod
    def _validate_security(cls, v: str) -> str:
        v = v.lower()
        if v not in ("ssl", "starttls", "none"):
            raise ValueError("Must be 'ssl', 'starttls', or 'none'")
        return v


class RemoveAccountInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    account_id: str = Field(..., description="Account id to remove", min_length=1)


class AutodiscoverInput(BaseModel):
    """Input for email autodiscovery."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    email_address: str = Field(
        ...,
        description="Email address to discover settings for (e.g. 'user@example.com')",
        min_length=3,
    )


class SieveListInput(AccountIdMixin):
    """Input for listing Sieve scripts."""
    pass


class SieveGetInput(AccountIdMixin):
    """Input for retrieving a Sieve script."""
    script_name: str = Field(..., description="Name of the Sieve script to retrieve", min_length=1)


class SievePutInput(AccountIdMixin):
    """Input for uploading a Sieve script."""
    script_name: str = Field(..., description="Name for the script on the server", min_length=1)
    script_content: str = Field(
        ...,
        description=(
            "Sieve script content. Example:\n"
            'require ["fileinto"];\n'
            'if address :is "from" "spam@example.com" {\n'
            '  fileinto "Junk";\n'
            "}"
        ),
        min_length=1,
    )
    activate: bool = Field(default=False, description="Immediately set this script as the active script")


class SieveActivateInput(AccountIdMixin):
    """Input for activating a Sieve script."""
    script_name: str = Field(
        ...,
        description="Name of the script to activate (empty string to deactivate all)",
    )


class SieveDeleteInput(AccountIdMixin):
    """Input for deleting a Sieve script."""
    script_name: str = Field(..., description="Name of the script to delete", min_length=1)


class SieveRenameInput(AccountIdMixin):
    """Input for renaming a Sieve script."""
    old_name: str = Field(..., description="Current script name", min_length=1)
    new_name: str = Field(..., description="New script name", min_length=1)


# -- CalDAV input models --

class CalListCalendarsInput(AccountIdMixin):
    """Input for listing CalDAV calendars."""
    pass


class CalListEventsInput(AccountIdMixin):
    """Input for listing events in a calendar."""
    calendar_name: Optional[str] = Field(default=None, description="Calendar name (default: first/primary calendar)")
    start: Optional[str] = Field(default=None, description="Start date in YYYY-MM-DD format (default: today)")
    end: Optional[str] = Field(default=None, description="End date in YYYY-MM-DD format (default: 30 days from start)")


class CalGetEventInput(AccountIdMixin):
    """Input for reading a single event."""
    uid: str = Field(..., description="Event UID", min_length=1)
    calendar_name: Optional[str] = Field(default=None, description="Calendar name")


class CalCreateEventInput(AccountIdMixin):
    """Input for creating a calendar event."""
    summary: str = Field(..., description="Event title", min_length=1)
    dtstart: str = Field(..., description="Start datetime in ISO format (e.g. '2026-03-20T14:00:00')")
    dtend: str = Field(..., description="End datetime in ISO format (e.g. '2026-03-20T15:00:00')")
    calendar_name: Optional[str] = Field(default=None, description="Calendar name")
    location: Optional[str] = Field(default=None, description="Event location")
    description: Optional[str] = Field(default=None, description="Event description")


class CalUpdateEventInput(AccountIdMixin):
    """Input for updating an existing event."""
    uid: str = Field(..., description="UID of the event to update", min_length=1)
    calendar_name: Optional[str] = Field(default=None, description="Calendar name")
    summary: Optional[str] = Field(default=None, description="New title")
    dtstart: Optional[str] = Field(default=None, description="New start in ISO format")
    dtend: Optional[str] = Field(default=None, description="New end in ISO format")
    location: Optional[str] = Field(default=None, description="New location")
    description: Optional[str] = Field(default=None, description="New description")


class CalDeleteEventInput(AccountIdMixin):
    """Input for deleting an event."""
    uid: str = Field(..., description="UID of the event to delete", min_length=1)
    calendar_name: Optional[str] = Field(default=None, description="Calendar name")


# -- CardDAV input models --

class CardListAddressBooksInput(AccountIdMixin):
    """Input for listing CardDAV address books."""
    pass


class CardListContactsInput(AccountIdMixin):
    """Input for listing contacts."""
    addressbook_name: Optional[str] = Field(default=None, description="Address book name (default: first/primary)")
    limit: int = Field(default=50, description="Max contacts to return", ge=1, le=500)


class CardSearchContactsInput(AccountIdMixin):
    """Input for searching contacts."""
    query: str = Field(..., description="Search string (matches against name, email, phone)", min_length=1)
    addressbook_name: Optional[str] = Field(default=None, description="Address book to search")
    limit: int = Field(default=50, description="Max results", ge=1, le=500)


class CardGetContactInput(AccountIdMixin):
    """Input for reading a single contact."""
    uid: str = Field(..., description="Contact UID", min_length=1)
    addressbook_name: Optional[str] = Field(default=None, description="Address book name")


class CardCreateContactInput(AccountIdMixin):
    """Input for creating a contact."""
    fn: str = Field(..., description="Full name", min_length=1)
    email: Optional[str] = Field(default=None, description="Email address(es), comma-separated")
    tel: Optional[str] = Field(default=None, description="Phone number(s), comma-separated")
    org: Optional[str] = Field(default=None, description="Organization")
    title: Optional[str] = Field(default=None, description="Job title")
    addressbook_name: Optional[str] = Field(default=None, description="Address book name")


class CardUpdateContactInput(AccountIdMixin):
    """Input for updating a contact."""
    uid: str = Field(..., description="UID of the contact to update", min_length=1)
    addressbook_name: Optional[str] = Field(default=None, description="Address book name")
    fn: Optional[str] = Field(default=None, description="New full name")
    email: Optional[str] = Field(default=None, description="New email(s)")
    tel: Optional[str] = Field(default=None, description="New phone(s)")
    org: Optional[str] = Field(default=None, description="New organization")
    title: Optional[str] = Field(default=None, description="New job title")


class CardDeleteContactInput(AccountIdMixin):
    """Input for deleting a contact."""
    uid: str = Field(..., description="UID of the contact to delete", min_length=1)
    addressbook_name: Optional[str] = Field(default=None, description="Address book name")


# ---------------------------------------------------------------------------
# Autodiscovery helpers
# ---------------------------------------------------------------------------

_AUTODISCOVER_TIMEOUT = 10.0  # seconds per HTTP request


def _domain_from_email(addr: str) -> str:
    """Extract the domain part of an email address."""
    return addr.rsplit("@", 1)[-1].strip().lower()


def _map_socket_type(raw: str) -> str:
    """Normalise socketType / SSL values from autoconfig XML to our enum."""
    raw = (raw or "").strip().upper()
    if raw in ("SSL", "TLS"):
        return "ssl"
    if raw in ("STARTTLS",):
        return "starttls"
    return "none"


async def _try_mozilla_autoconfig(domain: str) -> Optional[Dict[str, Any]]:
    """Try Mozilla-style autoconfig (also used by Mailcow, Stalwart, etc.).

    Checks (in order):
      1. https://autoconfig.{domain}/mail/config-v1.1.xml
      2. https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml
    """
    urls = [
        f"https://autoconfig.{domain}/mail/config-v1.1.xml",
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    ]

    async with safe_async_client(timeout=_AUTODISCOVER_TIMEOUT, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue
                return _parse_mozilla_autoconfig(resp.text)
            except Exception:
                continue
    return None


def _parse_mozilla_autoconfig(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse a Mozilla autoconfig XML response into our settings dict."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # Namespace-agnostic: strip any namespace prefixes
    def _strip_ns(tag: str) -> str:
        return re.sub(r"\{[^}]+\}", "", tag)

    def _find(parent: _XmlElement, tag: str) -> Optional[_XmlElement]:
        for child in parent.iter():
            if _strip_ns(child.tag) == tag:
                return child
        return None

    def _findall(parent: _XmlElement, tag: str) -> List[_XmlElement]:
        return [c for c in parent.iter() if _strip_ns(c.tag) == tag]

    result: Dict[str, Any] = {"source": "mozilla-autoconfig"}

    for server in _findall(root, "incomingServer"):
        stype = server.get("type", "").lower()
        if stype != "imap":
            continue
        hostname_el = _find(server, "hostname")
        port_el = _find(server, "port")
        socket_el = _find(server, "socketType")
        user_el = _find(server, "username")
        if hostname_el is not None and hostname_el.text:
            result["imap_host"] = hostname_el.text.strip()
        if port_el is not None and port_el.text:
            result["imap_port"] = int(port_el.text.strip())
        if socket_el is not None and socket_el.text:
            result["imap_security"] = _map_socket_type(socket_el.text)
        if user_el is not None and user_el.text:
            result["username_template"] = user_el.text.strip()
        break  # take first IMAP server

    for server in _findall(root, "outgoingServer"):
        stype = server.get("type", "").lower()
        if stype != "smtp":
            continue
        hostname_el = _find(server, "hostname")
        port_el = _find(server, "port")
        socket_el = _find(server, "socketType")
        if hostname_el is not None and hostname_el.text:
            result["smtp_host"] = hostname_el.text.strip()
        if port_el is not None and port_el.text:
            result["smtp_port"] = int(port_el.text.strip())
        if socket_el is not None and socket_el.text:
            result["smtp_security"] = _map_socket_type(socket_el.text)
        break

    # Display name from provider
    provider = _find(root, "emailProvider")
    if provider is not None:
        display_el = _find(provider, "displayName")
        if display_el is not None and display_el.text:
            result["provider_name"] = display_el.text.strip()

    if "imap_host" not in result and "smtp_host" not in result:
        return None
    return result


async def _try_microsoft_autodiscover(domain: str, email_addr: str) -> Optional[Dict[str, Any]]:
    """Try Microsoft / Mailcow Autodiscover (POX protocol).

    Posts an Autodiscover XML request to:
      1. https://autodiscover.{domain}/autodiscover/autodiscover.xml
      2. https://{domain}/autodiscover/autodiscover.xml
    """
    body = f"""<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/requestschema/2006">
  <Request>
    <EMailAddress>{email_addr}</EMailAddress>
    <AcceptableResponseSchema>http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a</AcceptableResponseSchema>
  </Request>
</Autodiscover>"""

    urls = [
        f"https://autodiscover.{domain}/autodiscover/autodiscover.xml",
        f"https://{domain}/autodiscover/autodiscover.xml",
    ]

    headers = {"Content-Type": "text/xml; charset=utf-8"}

    async with safe_async_client(timeout=_AUTODISCOVER_TIMEOUT, follow_redirects=True) as client:
        for url in urls:
            try:
                resp = await client.post(url, content=body, headers=headers)
                if resp.status_code != 200:
                    continue
                result = _parse_microsoft_autodiscover(resp.text)
                if result:
                    return result
            except Exception:
                continue
    return None


def _parse_microsoft_autodiscover(xml_text: str) -> Optional[Dict[str, Any]]:
    """Parse a Microsoft Autodiscover POX XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    # Strip namespaces for easier parsing
    def _strip_ns(tag: str) -> str:
        return re.sub(r"\{[^}]+\}", "", tag)

    def _findall(parent: _XmlElement, tag: str) -> List[_XmlElement]:
        return [c for c in parent.iter() if _strip_ns(c.tag) == tag]

    def _find_text(parent: _XmlElement, tag: str) -> Optional[str]:
        for c in parent.iter():
            if _strip_ns(c.tag) == tag and c.text:
                return c.text.strip()
        return None

    result: Dict[str, Any] = {"source": "microsoft-autodiscover"}

    for protocol in _findall(root, "Protocol"):
        ptype = (_find_text(protocol, "Type") or "").upper()
        server = _find_text(protocol, "Server")
        port = _find_text(protocol, "Port")
        ssl_val = (_find_text(protocol, "SSL") or "").lower()
        encryption = (_find_text(protocol, "Encryption") or "").upper()

        # Determine security
        if encryption in ("SSL", "TLS"):
            security = "ssl"
        elif encryption == "STARTTLS" or encryption == "Auto":
            security = "starttls"
        elif ssl_val in ("on", "true", "1"):
            security = "ssl"
        else:
            security = "starttls"

        if ptype == "IMAP" and server:
            result["imap_host"] = server
            if port:
                result["imap_port"] = int(port)
            result["imap_security"] = security
        elif ptype == "SMTP" and server:
            result["smtp_host"] = server
            if port:
                result["smtp_port"] = int(port)
            result["smtp_security"] = security

    if "imap_host" not in result and "smtp_host" not in result:
        return None
    return result


async def _try_dns_srv(domain: str) -> Optional[Dict[str, Any]]:
    """Try DNS SRV records for IMAP and SMTP (submission).

    Looks up:
      _imaps._tcp.{domain}   (IMAP over SSL, port 993)
      _imap._tcp.{domain}    (IMAP with STARTTLS, port 143)
      _submissions._tcp.{domain}  (SMTP over SSL, port 465)
      _submission._tcp.{domain}   (SMTP with STARTTLS, port 587)
    """
    result: Dict[str, Any] = {"source": "dns-srv"}

    srv_queries = [
        # (record, key_host, key_port, key_security, security_value)
        (f"_imaps._tcp.{domain}", "imap_host", "imap_port", "imap_security", "ssl"),
        (f"_imap._tcp.{domain}", "imap_host", "imap_port", "imap_security", "starttls"),
        (f"_submissions._tcp.{domain}", "smtp_host", "smtp_port", "smtp_security", "ssl"),
        (f"_submission._tcp.{domain}", "smtp_host", "smtp_port", "smtp_security", "starttls"),
    ]

    loop = asyncio.get_event_loop()
    for srv_name, hkey, pkey, skey, sval in srv_queries:
        if hkey in result:
            continue  # prefer earlier (more secure) variant
        try:
            answers = await loop.run_in_executor(
                None, lambda name=srv_name: socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
            )
            # getaddrinfo won't give us SRV data, fall back to socket resolver
        except Exception:
            pass
        # Use the resolver module approach via subprocess for SRV
        try:
            proc = await asyncio.create_subprocess_exec(
                "dig", "+short", "SRV", srv_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            lines = stdout.decode().strip().splitlines()
            if lines:
                # SRV format: priority weight port target
                parts = lines[0].split()
                if len(parts) >= 4:
                    port = int(parts[2])
                    host = parts[3].rstrip(".")
                    if host and host != ".":
                        result[hkey] = host
                        result[pkey] = port
                        result[skey] = sval
        except Exception:
            continue

    if "imap_host" not in result and "smtp_host" not in result:
        return None
    return result


async def _try_wellknown_dav(domain: str) -> Optional[Dict[str, Any]]:
    """Discover CalDAV/CardDAV endpoints via .well-known (RFC 6764)."""
    result: Dict[str, Any] = {"source": "well-known-dav"}
    checks = [
        (f"https://{domain}/.well-known/caldav", "caldav_url"),
        (f"https://{domain}/.well-known/carddav", "carddav_url"),
    ]
    async with safe_async_client(
        timeout=_AUTODISCOVER_TIMEOUT, follow_redirects=True
    ) as client:
        for url, key in checks:
            try:
                resp = await client.request("PROPFIND", url, headers={"Depth": "0"})
                # A redirect (even after following) or 207 means it's valid
                final_url = str(resp.url)
                if resp.status_code in (200, 207):
                    result[key] = final_url
                elif resp.status_code == 301 or resp.status_code == 302:
                    loc = resp.headers.get("location")
                    if loc:
                        result[key] = loc
            except Exception:
                continue
            if key not in result:
                # Try a simple GET — many servers redirect .well-known to the DAV root
                try:
                    resp = await client.get(url)
                    final_url = str(resp.url)
                    if final_url != url and resp.status_code in (200, 207):
                        result[key] = final_url
                except Exception:
                    continue

    if "caldav_url" not in result and "carddav_url" not in result:
        return None
    return result


async def _autodiscover(email_addr: str) -> Dict[str, Any]:
    """Run all autodiscovery methods and merge results.

    Priority: Mozilla autoconfig > Microsoft autodiscover > DNS SRV > well-known DAV.
    Earlier results win; later results fill in gaps.
    """
    domain = _domain_from_email(email_addr)
    local_part = email_addr.rsplit("@", 1)[0]

    # Run all methods concurrently
    mozilla, microsoft, dns, dav = await asyncio.gather(
        _try_mozilla_autoconfig(domain),
        _try_microsoft_autodiscover(domain, email_addr),
        _try_dns_srv(domain),
        _try_wellknown_dav(domain),
        return_exceptions=True,
    )

    merged: Dict[str, Any] = {}
    sources: List[str] = []

    for result in (mozilla, microsoft, dns, dav):
        if isinstance(result, Exception) or result is None:
            continue
        source = result.pop("source", "unknown")
        sources.append(source)
        for k, v in result.items():
            if k not in merged:
                merged[k] = v

    if not merged:
        return {"error": f"No autodiscovery results for {domain}. You may need to configure this account manually."}

    merged["sources"] = sources

    # Expand username template placeholders
    template = merged.pop("username_template", None)
    if template:
        username = (
            template
            .replace("%EMAILADDRESS%", email_addr)
            .replace("%EMAILLOCALPART%", local_part)
            .replace("%EMAILDOMAIN%", domain)
        )
        merged["suggested_username"] = username

    return merged


# ---------------------------------------------------------------------------
# Tools — Account management
# ---------------------------------------------------------------------------

@mcp.tool(
    name="email_autodiscover",
    annotations={
        "title": "Autodiscover Email Settings",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_autodiscover(params: AutodiscoverInput) -> str:
    """Discover IMAP/SMTP server settings from an email address.

    Tries multiple autodiscovery protocols in parallel:
    1. Mozilla autoconfig (autoconfig.{domain}, .well-known/autoconfig)
    2. Microsoft/Mailcow Autodiscover (POX protocol)
    3. DNS SRV records (_imaps._tcp, _submission._tcp, etc.)

    Results are merged — the first protocol to return a value wins.
    Use the discovered settings with email_add_account.

    Args:
        params: Contains the email_address to discover settings for.

    Returns:
        Markdown summary of discovered IMAP and SMTP settings,
        or an error message if no settings could be found.
    """
    result = await _autodiscover(params.email_address)

    if "error" in result:
        return result["error"]

    domain = _domain_from_email(params.email_address)
    sources = result.get("sources", [])

    lines = [
        f"# Autodiscovery Results for {params.email_address}",
        f"Domain: `{domain}` — discovered via: {', '.join(sources)}\n",
    ]

    if result.get("provider_name"):
        lines.append(f"**Provider**: {result['provider_name']}\n")

    if result.get("imap_host"):
        lines.append("## IMAP (Incoming)")
        lines.append(f"- **Host**: {result['imap_host']}")
        lines.append(f"- **Port**: {result.get('imap_port', 993)}")
        lines.append(f"- **Security**: {result.get('imap_security', 'ssl')}")
        lines.append("")

    if result.get("smtp_host"):
        lines.append("## SMTP (Outgoing)")
        lines.append(f"- **Host**: {result['smtp_host']}")
        lines.append(f"- **Port**: {result.get('smtp_port', 587)}")
        lines.append(f"- **Security**: {result.get('smtp_security', 'starttls')}")
        lines.append("")

    if result.get("caldav_url"):
        lines.append("## CalDAV (Calendar)")
        lines.append(f"- **URL**: {result['caldav_url']}")
        lines.append("")

    if result.get("carddav_url"):
        lines.append("## CardDAV (Contacts)")
        lines.append(f"- **URL**: {result['carddav_url']}")
        lines.append("")

    if result.get("suggested_username"):
        lines.append(f"**Suggested username**: `{result['suggested_username']}`")
    else:
        lines.append(f"**Suggested username**: `{params.email_address}` (default)")

    lines.append("")
    lines.append("Use these settings with `email_add_account` to complete setup. "
                  "You will still need to provide a password or app password.")

    return "\n".join(lines)


@mcp.tool(
    name="email_list_accounts",
    annotations={
        "title": "List Email Accounts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def email_list_accounts() -> str:
    """List all configured email accounts.

    Returns a summary of every account: id, display name, email address,
    IMAP host, and SMTP host. Passwords are never shown.
    """
    accounts = _load_accounts()
    if not accounts:
        return "No email accounts configured. Use email_add_account to add one."

    lines = [f"# Configured Email Accounts ({len(accounts)})\n"]
    for a in accounts:
        name = a.get("display_name") or a["id"]
        lines.append(f"## {name} (`{a['id']}`)")
        lines.append(f"- **Email**: {a['email_address']}")
        lines.append(f"- **IMAP**: {a['imap_host']}:{a.get('imap_port', 993)} ({a.get('imap_security', 'ssl')})")
        lines.append(f"- **SMTP**: {a['smtp_host']}:{a.get('smtp_port', 587)} ({a.get('smtp_security', 'starttls')})")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(
    name="email_add_account",
    annotations={
        "title": "Add Email Account",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def email_add_account(params: AddAccountInput) -> str:
    """Add a new IMAP/SMTP email account to the configuration.

    The account will be persisted in the accounts.json config file and
    immediately available for use with other email tools.

    Args:
        params: Account connection details (host, port, credentials, security).

    Returns:
        Confirmation message or error if the id already exists.
    """
    accounts = _load_accounts()
    if any(a["id"] == params.id for a in accounts):
        return f"Error: An account with id '{params.id}' already exists."

    accounts.append(params.model_dump())
    _save_accounts(accounts)
    return f"Account '{params.id}' ({params.email_address}) added successfully."


@mcp.tool(
    name="email_remove_account",
    annotations={
        "title": "Remove Email Account",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
async def email_remove_account(params: RemoveAccountInput) -> str:
    """Remove an email account from the configuration.

    Args:
        params: Contains the account_id to remove.

    Returns:
        Confirmation or error if the account was not found.
    """
    accounts = _load_accounts()
    before = len(accounts)
    accounts = [a for a in accounts if a["id"] != params.account_id]
    if len(accounts) == before:
        return f"Error: Account '{params.account_id}' not found."
    _save_accounts(accounts)
    return f"Account '{params.account_id}' removed."


# ---------------------------------------------------------------------------
# Tools — Folder operations
# ---------------------------------------------------------------------------

@mcp.tool(
    name="email_list_folders",
    annotations={
        "title": "List IMAP Folders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_list_folders(params: ListFoldersInput) -> str:
    """List all IMAP folders (mailboxes) for an account.

    Args:
        params: Contains account_id.

    Returns:
        Markdown list of folder names.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                status, data = conn.list()
                if status != "OK":
                    return f"Error: IMAP LIST failed: {status}"
                folders = []
                for item in data:
                    name = _parse_imap_list_line(item)
                    if name:
                        folders.append(name)
                folders.sort()
                lines = [f"# Folders for {acct.get('display_name') or params.account_id}\n"]
                for f in folders:
                    lines.append(f"- {f}")
                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_create_folder",
    annotations={
        "title": "Create IMAP Folder",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_create_folder(params: CreateFolderInput) -> str:
    """Create a new IMAP folder.

    Args:
        params: Contains account_id and folder name.

    Returns:
        Confirmation or error message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                status, data = conn.create(params.folder)
                if status != "OK":
                    return f"Error creating folder: {data}"
                return f"Folder '{params.folder}' created on {params.account_id}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_delete_folder",
    annotations={
        "title": "Delete IMAP Folder",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_delete_folder(params: DeleteFolderInput) -> str:
    """Delete an IMAP folder and all messages in it.

    Args:
        params: Contains account_id and folder name.

    Returns:
        Confirmation or error message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                status, data = conn.delete(params.folder)
                if status != "OK":
                    return f"Error deleting folder: {data}"
                return f"Folder '{params.folder}' deleted from {params.account_id}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


# ---------------------------------------------------------------------------
# Tools — Reading mail
# ---------------------------------------------------------------------------

@mcp.tool(
    name="email_list_messages",
    annotations={
        "title": "List Emails in Folder",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_list_messages(params: ListEmailsInput) -> str:
    """List recent emails in an IMAP folder.

    Returns message UIDs, senders, subjects, and dates — most recent first.
    Use email_read_message with a UID to read the full body.

    Args:
        params: account_id, folder, limit, offset.

    Returns:
        Markdown table of messages or an empty-folder notice.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder, readonly=True)
                status, data = conn.uid("SEARCH", None, "ALL")
                if status != "OK":
                    return f"Error: SEARCH failed: {status}"
                uids = data[0].split() if data[0] else []
                if not uids:
                    return f"No messages in {params.folder}."

                # Most recent first
                uids = list(reversed(uids))
                page = uids[params.offset : params.offset + params.limit]

                results = []
                for uid_bytes in page:
                    uid = uid_bytes.decode()
                    status2, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
                    if status2 != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                    msg = email.message_from_bytes(raw)
                    results.append(_summarise_msg(msg, uid))

                total = len(uids)
                showing = len(results)
                lines = [
                    f"# {params.folder} — {acct.get('display_name') or params.account_id}",
                    f"Showing {showing} of {total} messages (offset {params.offset})\n",
                    "| UID | From | Subject | Date |",
                    "|-----|------|---------|------|",
                ]
                for r in results:
                    lines.append(f"| {r['uid']} | {r['from'][:40]} | {r['subject'][:50]} | {r['date'][:25]} |")

                if params.offset + params.limit < total:
                    lines.append(f"\n*More messages available — use offset={params.offset + params.limit}*")

                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_search_messages",
    annotations={
        "title": "Search Emails",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_search_messages(params: SearchEmailsInput) -> str:
    """Search emails using IMAP SEARCH criteria.

    Common queries:
    - FROM "alice@example.com"
    - SUBJECT "invoice"
    - SINCE 01-Jan-2025
    - UNSEEN
    - OR FROM "bob" SUBJECT "urgent"
    - FLAGGED

    Args:
        params: account_id, query (IMAP search string), folder, limit.

    Returns:
        Markdown table of matching messages.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder, readonly=True)
                status, data = conn.uid("SEARCH", None, params.query)
                if status != "OK":
                    return f"Error: SEARCH failed: {status}. Check your query syntax."
                uids = data[0].split() if data[0] else []
                if not uids:
                    return f"No messages matching: {params.query}"

                uids = list(reversed(uids))[: params.limit]
                results = []
                for uid_bytes in uids:
                    uid = uid_bytes.decode()
                    status2, msg_data = conn.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])")
                    if status2 != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1] if isinstance(msg_data[0], tuple) else msg_data[0]
                    msg = email.message_from_bytes(raw)
                    results.append(_summarise_msg(msg, uid))

                lines = [
                    f"# Search Results — {acct.get('display_name') or params.account_id}",
                    f"Query: `{params.query}` in {params.folder} ({len(results)} results)\n",
                    "| UID | From | Subject | Date |",
                    "|-----|------|---------|------|",
                ]
                for r in results:
                    lines.append(f"| {r['uid']} | {r['from'][:40]} | {r['subject'][:50]} | {r['date'][:25]} |")
                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_read_message",
    annotations={
        "title": "Read Email Message",
        "readOnlyHint": False,  # can set \Seen flag
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_read_message(params: ReadEmailInput) -> str:
    """Read the full content of an email by UID.

    Args:
        params: account_id, uid, folder, mark_read.

    Returns:
        Full message headers and plain-text body.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder, readonly=not params.mark_read)
                fetch_cmd = "(RFC822)" if params.mark_read else "(BODY.PEEK[])"
                status, data = conn.uid("FETCH", params.uid, fetch_cmd)
                if status != "OK" or not data or not data[0]:
                    return f"Error: Could not fetch message UID {params.uid}."
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                msg = email.message_from_bytes(raw)
                body = _get_body(msg)

                lines = [
                    f"# Message UID {params.uid}",
                    f"**From**: {_decode_header(msg.get('From'))}",
                    f"**To**: {_decode_header(msg.get('To'))}",
                ]
                if msg.get("Cc"):
                    lines.append(f"**CC**: {_decode_header(msg.get('Cc'))}")
                lines.append(f"**Subject**: {_decode_header(msg.get('Subject'))}")
                lines.append(f"**Date**: {_decode_header(msg.get('Date'))}")
                lines.append(f"**Message-ID**: {msg.get('Message-ID', 'N/A')}")
                lines.append("")
                lines.append("---")
                lines.append("")
                lines.append(body[:50000])  # cap very large bodies

                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


# ---------------------------------------------------------------------------
# Tools — Sending mail
# ---------------------------------------------------------------------------

def _build_message(
    acct: Dict[str, Any],
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> email.mime.multipart.MIMEMultipart:
    """Build a MIME message."""
    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = acct["email_address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    if cc:
        msg["Cc"] = cc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
    return msg


def _send_message(acct: Dict[str, Any], msg: email.mime.multipart.MIMEMultipart, bcc: Optional[str] = None) -> None:
    """Send *msg* via SMTP and save to Sent folder via IMAP."""
    recipients = []
    for hdr in ("To", "Cc"):
        if msg[hdr]:
            recipients.extend([a.strip() for a in msg[hdr].split(",")])
    if bcc:
        recipients.extend([a.strip() for a in bcc.split(",")])

    conn = _smtp_connect(acct)
    try:
        conn.sendmail(acct["email_address"], recipients, msg.as_string())
    finally:
        conn.quit()

    # Try to save to Sent folder
    try:
        imap = _imap_connect(acct)
        try:
            # Try common Sent folder names
            for folder in ("Sent", "INBOX.Sent", "Sent Items", "Sent Messages"):
                status, _ = imap.select(folder)
                if status == "OK":
                    imap.append(
                        folder,
                        "\\Seen",
                        imaplib.Time2Internaldate(datetime.now(timezone.utc).timestamp()),
                        msg.as_bytes(),
                    )
                    break
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception:
        pass  # Non-fatal: message was sent even if save to Sent fails


@mcp.tool(
    name="email_send_message",
    annotations={
        "title": "Send Email",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def email_send_message(params: SendEmailInput) -> str:
    """Compose and send a new email.

    Args:
        params: account_id, to, subject, body, optional cc/bcc.

    Returns:
        Confirmation with recipient and subject.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            msg = _build_message(acct, params.to, params.subject, params.body, params.cc, params.bcc)
            _send_message(acct, msg, params.bcc)
            return f"Email sent from {acct['email_address']} to {params.to}: \"{params.subject}\""
        except Exception as e:
            return f"Error sending email: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_reply",
    annotations={
        "title": "Reply to Email",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def email_reply(params: ReplyEmailInput) -> str:
    """Reply to an existing email.

    Fetches the original message to get the correct headers (In-Reply-To,
    References, subject) and sends the reply.

    Args:
        params: account_id, uid, folder, body, reply_all.

    Returns:
        Confirmation message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder, readonly=True)
                status, data = conn.uid("FETCH", params.uid, "(BODY.PEEK[])")
                if status != "OK" or not data or not data[0]:
                    return f"Error: Could not fetch message UID {params.uid}."
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                original = email.message_from_bytes(raw)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

            # Determine recipients
            to = _decode_header(original.get("Reply-To") or original.get("From"))
            cc = None
            if params.reply_all:
                orig_to = _decode_header(original.get("To") or "")
                orig_cc = _decode_header(original.get("Cc") or "")
                all_addrs = [a.strip() for a in (orig_to + "," + orig_cc).split(",") if a.strip()]
                # Remove self
                all_addrs = [a for a in all_addrs if acct["email_address"].lower() not in a.lower()]
                cc = ", ".join(all_addrs) if all_addrs else None

            subject = _decode_header(original.get("Subject") or "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"

            message_id = original.get("Message-ID", "")
            references = original.get("References", "")
            if message_id:
                references = f"{references} {message_id}".strip()

            msg = _build_message(acct, to, subject, params.body, cc=cc, in_reply_to=message_id, references=references)
            _send_message(acct, msg)
            return f"Reply sent to {to}" + (f" (CC: {cc})" if cc else "")
        except Exception as e:
            return f"Error replying: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_forward",
    annotations={
        "title": "Forward Email",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def email_forward(params: ForwardEmailInput) -> str:
    """Forward an existing email to new recipients.

    Fetches the original message, prepends an optional note, and sends
    it to the specified addresses.

    Args:
        params: account_id, uid, folder, to, optional body note.

    Returns:
        Confirmation message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder, readonly=True)
                status, data = conn.uid("FETCH", params.uid, "(BODY.PEEK[])")
                if status != "OK" or not data or not data[0]:
                    return f"Error: Could not fetch message UID {params.uid}."
                raw = data[0][1] if isinstance(data[0], tuple) else data[0]
                original = email.message_from_bytes(raw)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass

            orig_body = _get_body(original)
            fwd_body = ""
            if params.body:
                fwd_body += params.body + "\n\n"
            fwd_body += "---------- Forwarded message ----------\n"
            fwd_body += f"From: {_decode_header(original.get('From'))}\n"
            fwd_body += f"Date: {_decode_header(original.get('Date'))}\n"
            fwd_body += f"Subject: {_decode_header(original.get('Subject'))}\n"
            fwd_body += f"To: {_decode_header(original.get('To'))}\n\n"
            fwd_body += orig_body

            subject = _decode_header(original.get("Subject") or "")
            if not subject.lower().startswith("fwd:"):
                subject = f"Fwd: {subject}"

            msg = _build_message(acct, params.to, subject, fwd_body)
            _send_message(acct, msg)
            return f"Forwarded to {params.to}: \"{subject}\""
        except Exception as e:
            return f"Error forwarding: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_move_message",
    annotations={
        "title": "Move Email to Folder",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_move_message(params: MoveEmailInput) -> str:
    """Move an email from one IMAP folder to another.

    Uses IMAP COPY + delete to move the message.

    Args:
        params: account_id, uid, source_folder, dest_folder.

    Returns:
        Confirmation message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.source_folder)
                # Copy to destination
                status, data = conn.uid("COPY", params.uid, params.dest_folder)
                if status != "OK":
                    return f"Error copying message: {data}"
                # Mark original for deletion
                conn.uid("STORE", params.uid, "+FLAGS", "(\\Deleted)")
                # Use UID EXPUNGE (RFC 4315 UIDPLUS) so we only remove this
                # UID, not every \Deleted message in the folder. If the
                # server doesn't advertise UIDPLUS, refuse rather than risk
                # destroying the user's other \Deleted messages.
                caps = b" ".join(conn.capabilities).upper() if hasattr(conn, "capabilities") else b""
                if b"UIDPLUS" in caps:
                    conn.uid("EXPUNGE", params.uid)
                else:
                    # Clear the \Deleted flag so a later untargeted
                    # expunge from another client doesn't remove this
                    # message either.
                    conn.uid("STORE", params.uid, "-FLAGS", "(\\Deleted)")
                    return (
                        f"Error: server does not advertise UIDPLUS capability; "
                        f"refusing to issue an untargeted EXPUNGE. Message UID "
                        f"{params.uid} was copied to {params.dest_folder} but the "
                        f"original in {params.source_folder} was left in place "
                        f"(its \\Deleted flag has been cleared)."
                    )
                return f"Message UID {params.uid} moved from {params.source_folder} to {params.dest_folder}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error moving message: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_delete_message",
    annotations={
        "title": "Delete Email (Move to Trash or Permanent)",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_delete_message(params: DeleteEmailInput) -> str:
    """Delete a message — move it to Trash (default) or expunge permanently.

    Default (``permanent=False``): COPY the message to the resolved Trash
    folder, then STORE ``\\Deleted`` and ``UID EXPUNGE`` on the original.
    The Trash folder is resolved in priority order: explicit
    ``trash_folder`` param → per-account ``trash_folder`` config →
    SPECIAL-USE ``\\Trash`` flag on a LIST response (RFC 6154) →
    hard-coded ``"Trash"`` literal. If the COPY returns ``TRYCREATE``
    (folder does not exist), the tool creates the target and retries once.

    With ``permanent=True``: STORE ``\\Deleted`` + ``UID EXPUNGE`` only —
    no copy hop. Both paths require UIDPLUS; without it, the tool refuses
    and rolls back the ``\\Deleted`` flag on the permanent path rather
    than risk an untargeted EXPUNGE removing the user's other flagged
    messages.

    Args:
        params: account_id, uid, folder (default INBOX), permanent
            (default False), trash_folder (optional override).

    Returns:
        Confirmation string naming UID + destination, or an ``Error: …``
        message on failure.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder)
                caps = b" ".join(conn.capabilities).upper() if hasattr(conn, "capabilities") else b""
                uidplus = b"UIDPLUS" in caps
                if params.permanent:
                    # STORE \Deleted, then UID EXPUNGE (gated by UIDPLUS).
                    conn.uid("STORE", params.uid, "+FLAGS", "(\\Deleted)")
                    if not uidplus:
                        # Roll back the \Deleted flag so another client's
                        # untargeted EXPUNGE can't pick it up either.
                        conn.uid("STORE", params.uid, "-FLAGS", "(\\Deleted)")
                        return (
                            f"Error: server does not advertise UIDPLUS capability; "
                            f"refusing to issue an untargeted EXPUNGE. Message UID "
                            f"{params.uid} was left untouched in {params.folder} "
                            f"(its \\Deleted flag has been cleared)."
                        )
                    conn.uid("EXPUNGE", params.uid)
                    return f"Message UID {params.uid} permanently deleted from {params.folder}."
                # Move-to-trash path: resolve trash, COPY (auto-create on
                # TRYCREATE), STORE \Deleted, UID EXPUNGE.
                if not uidplus:
                    return (
                        f"Error: server does not advertise UIDPLUS capability; "
                        f"refusing to issue an untargeted EXPUNGE. Message UID "
                        f"{params.uid} was left untouched in {params.folder}."
                    )
                trash = _resolve_trash_folder(conn, acct, params.trash_folder)
                status, data = conn.uid("COPY", params.uid, trash)
                if status == "NO" and data and any(
                    isinstance(d, (bytes, bytearray)) and b"TRYCREATE" in bytes(d).upper()
                    for d in data
                ):
                    conn.create(trash)
                    status, data = conn.uid("COPY", params.uid, trash)
                if status != "OK":
                    return f"Error deleting message: {data}"
                conn.uid("STORE", params.uid, "+FLAGS", "(\\Deleted)")
                conn.uid("EXPUNGE", params.uid)
                return f"Message UID {params.uid} moved from {params.folder} to {trash} (Trash)."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error deleting message: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_expunge",
    annotations={
        "title": "Expunge \\Deleted Messages",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_expunge(params: ExpungeInput) -> str:
    """Expunge messages already marked ``\\Deleted`` from a folder.

    Two modes: (1) UID-scoped — supply ``uid`` to expunge just that
    message (requires UIDPLUS). (2) Bare expunge — omit ``uid`` and pass
    ``confirm_bare_expunge=True`` to remove EVERY ``\\Deleted`` message
    in the folder. The tool itself does not mark anything ``\\Deleted``;
    compose with ``email_modify_flags`` first to mark, then call this to
    expunge. Use ``email_delete_message`` for the simpler one-shot
    trash/permanent workflow.

    Args:
        params: account_id, folder (default INBOX), optional uid,
            confirm_bare_expunge (required when uid is unset).

    Returns:
        Confirmation message, or an ``Error: …`` string on refusal/failure.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder)
                caps = b" ".join(conn.capabilities).upper() if hasattr(conn, "capabilities") else b""
                if params.uid:
                    if b"UIDPLUS" not in caps:
                        return (
                            f"Error: server does not advertise UIDPLUS capability; "
                            f"refusing to issue an untargeted EXPUNGE. UID "
                            f"{params.uid} was left untouched in {params.folder}."
                        )
                    status, data = conn.uid("EXPUNGE", params.uid)
                    if status != "OK":
                        return f"Error expunging: {data}"
                    return f"Expunged UID {params.uid} from {params.folder}."
                if not params.confirm_bare_expunge:
                    return (
                        f"Error: a bare EXPUNGE removes every \\Deleted message in "
                        f"{params.folder}. Pass confirm_bare_expunge=True to proceed, "
                        f"or supply a uid for a scoped expunge."
                    )
                conn.expunge()
                return f"Bare EXPUNGE issued on {params.folder}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error expunging: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_modify_flags",
    annotations={
        "title": "Modify IMAP Message Flags",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_modify_flags(params: ModifyFlagsInput) -> str:
    """Add and/or remove IMAP flags on a single message.

    Maps to one or two ``UID STORE`` commands in a single IMAP session:
    ``+FLAGS`` for adds (run first) then ``-FLAGS`` for removes. Accepts
    system flags (``\\Flagged``, ``\\Seen``, ``\\Answered``, ``\\Draft``)
    and bare custom keywords (``follow-up``). The tool does NOT expunge —
    setting ``\\Deleted`` here will mark the message but leave it in
    place; use ``email_delete_message`` for the trash/permanent workflow
    or ``email_expunge`` to remove already-flagged messages.

    Args:
        params: account_id, uid, folder (default INBOX), add_flags,
            remove_flags. At least one of the flag lists must be
            non-empty.

    Returns:
        Confirmation message naming the UID, folder, and applied flag
        deltas, or an ``Error: …`` string on failure.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _imap_connect(acct)
            try:
                conn.select(params.folder)
                # Adds first, then removes — order pinned by tests and
                # documented for callers. A single tool invocation can
                # carry both deltas; the IMAP wire format puts each
                # group in its own STORE call.
                if params.add_flags:
                    flags = "(" + " ".join(params.add_flags) + ")"
                    status, data = conn.uid("STORE", params.uid, "+FLAGS", flags)
                    if status != "OK":
                        return f"Error updating flags: {data}"
                if params.remove_flags:
                    flags = "(" + " ".join(params.remove_flags) + ")"
                    status, data = conn.uid("STORE", params.uid, "-FLAGS", flags)
                    if status != "OK":
                        return f"Error updating flags: {data}"
                return (
                    f"Flags updated on UID {params.uid} in {params.folder}: "
                    f"+{params.add_flags} -{params.remove_flags}"
                )
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error updating flags: {e}"
    return await asyncio.to_thread(_impl)


# ---------------------------------------------------------------------------
# Tools — Sieve / ManageSieve
# ---------------------------------------------------------------------------

@mcp.tool(
    name="email_sieve_list",
    annotations={
        "title": "List Sieve Scripts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_list(params: SieveListInput) -> str:
    """List all Sieve filter scripts on the server for an account.

    Shows script names and which one (if any) is currently active.
    ManageSieve typically runs on port 4190 of the IMAP host.

    Args:
        params: Contains account_id.

    Returns:
        Markdown list of scripts with active indicator.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                result, scripts = conn.listscripts()
                if result != "OK":
                    return f"Error listing scripts: {result}"
                if not scripts:
                    return f"No Sieve scripts on {params.account_id}."

                lines = [f"# Sieve Scripts — {acct.get('display_name') or params.account_id}\n"]
                for name, active in scripts:
                    marker = " **(active)**" if active else ""
                    lines.append(f"- `{name}`{marker}")
                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_sieve_get",
    annotations={
        "title": "Get Sieve Script",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_get(params: SieveGetInput) -> str:
    """Retrieve the content of a Sieve script from the server.

    Args:
        params: Contains account_id and script_name.

    Returns:
        The full Sieve script content.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                result, script_data = conn.getscript(params.script_name)
                if result != "OK":
                    return f"Error: Could not retrieve script '{params.script_name}': {result}"
                lines = [
                    f"# Sieve Script: `{params.script_name}`",
                    "",
                    "```sieve",
                    script_data,
                    "```",
                ]
                return "\n".join(lines)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_sieve_put",
    annotations={
        "title": "Upload Sieve Script",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_put(params: SievePutInput) -> str:
    """Upload (create or replace) a Sieve script on the server.

    The server validates the script syntax before accepting it.
    If validation fails, the error message from the server is returned.
    Optionally activates the script immediately.

    Args:
        params: Contains account_id, script_name, script_content, activate.

    Returns:
        Confirmation or server-side validation error.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                result = conn.putscript(params.script_name, params.script_content)
                if result != "OK":
                    return f"Error uploading script: {result}. The server rejected the script — check syntax."

                msg = f"Script '{params.script_name}' uploaded to {params.account_id}."

                if params.activate:
                    act_result = conn.setactive(params.script_name)
                    if act_result != "OK":
                        msg += f"\nWarning: upload succeeded but activation failed: {act_result}"
                    else:
                        msg += " Script is now **active**."

                return msg
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_sieve_activate",
    annotations={
        "title": "Activate Sieve Script",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_activate(params: SieveActivateInput) -> str:
    """Set a Sieve script as the active filter, or deactivate all scripts.

    Only one script can be active at a time. Pass an empty string
    for script_name to deactivate all filtering.

    Args:
        params: Contains account_id and script_name.

    Returns:
        Confirmation message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                result = conn.setactive(params.script_name)
                if result != "OK":
                    return f"Error activating script: {result}"
                if params.script_name:
                    return f"Script '{params.script_name}' is now the active filter on {params.account_id}."
                else:
                    return f"All Sieve scripts deactivated on {params.account_id}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_sieve_delete",
    annotations={
        "title": "Delete Sieve Script",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_delete(params: SieveDeleteInput) -> str:
    """Delete a Sieve script from the server.

    The active script cannot be deleted — deactivate it first
    by calling email_sieve_activate with an empty script_name.

    Args:
        params: Contains account_id and script_name.

    Returns:
        Confirmation or error message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                result = conn.deletescript(params.script_name)
                if result != "OK":
                    return (
                        f"Error deleting script '{params.script_name}': {result}. "
                        "If it's the active script, deactivate it first with email_sieve_activate."
                    )
                return f"Script '{params.script_name}' deleted from {params.account_id}."
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


@mcp.tool(
    name="email_sieve_rename",
    annotations={
        "title": "Rename Sieve Script",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def email_sieve_rename(params: SieveRenameInput) -> str:
    """Rename a Sieve script (download, re-upload with new name, delete old).

    ManageSieve has no native rename — this performs a get + put + delete.
    If the old script was active, the new one is activated automatically.

    Args:
        params: Contains account_id, old_name, new_name.

    Returns:
        Confirmation or error message.
    """
    def _impl():
        try:
            acct = _get_account(params.account_id)
            conn = _sieve_connect(acct)
            try:
                # Check if the old script is active
                list_result, scripts = conn.listscripts()
                was_active = False
                if list_result == "OK":
                    for name, active in scripts:
                        if name == params.old_name and active:
                            was_active = True
                            break

                # Get the old script content
                get_result, content = conn.getscript(params.old_name)
                if get_result != "OK":
                    return f"Error: Could not retrieve script '{params.old_name}': {get_result}"

                # Upload with new name
                put_result = conn.putscript(params.new_name, content)
                if put_result != "OK":
                    return f"Error uploading script with new name: {put_result}"

                # Activate the new script if old was active
                if was_active:
                    conn.setactive(params.new_name)

                # Delete the old script (deactivate first if it was active)
                if was_active:
                    # Already deactivated by setting new one active
                    pass
                del_result = conn.deletescript(params.old_name)
                if del_result != "OK":
                    return (
                        f"Script uploaded as '{params.new_name}' but could not "
                        f"delete old '{params.old_name}': {del_result}"
                    )

                msg = f"Script renamed from '{params.old_name}' to '{params.new_name}'."
                if was_active:
                    msg += " The new script is active."
                return msg
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            return f"Error: {e}"
    return await asyncio.to_thread(_impl)


# ---------------------------------------------------------------------------
# Tools — CalDAV (Calendars)
# ---------------------------------------------------------------------------

def _get_calendar(acct: Dict[str, Any], name: Optional[str]) -> caldav.Calendar:
    """Return a calendar by name, or the first available calendar."""
    client = _caldav_client(acct)
    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise ValueError(f"No calendars found for account '{acct['id']}'.")
    if name:
        for cal in calendars:
            if cal.name and cal.name.lower() == name.lower():
                return cal
        raise ValueError(
            f"Calendar '{name}' not found. Available: "
            + ", ".join(c.name or str(c.url) for c in calendars)
        )
    return calendars[0]


@mcp.tool(
    name="cal_list_calendars",
    annotations={
        "title": "List CalDAV Calendars",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def cal_list_calendars(params: CalListCalendarsInput) -> str:
    """List all CalDAV calendars for an account.

    Args:
        params: Contains account_id.

    Returns:
        Markdown list of calendar names.
    """
    try:
        acct = _get_account(params.account_id)
        client = _caldav_client(acct)
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            return f"No calendars found for {params.account_id}."
        lines = [f"# Calendars — {acct.get('display_name') or params.account_id}\n"]
        for cal in calendars:
            name = cal.name or "(unnamed)"
            lines.append(f"- **{name}** — `{cal.url}`")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="cal_list_events",
    annotations={
        "title": "List Calendar Events",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def cal_list_events(params: CalListEventsInput) -> str:
    """List events in a CalDAV calendar within a date range.

    Defaults to today + 30 days if no dates are specified.

    Args:
        params: account_id, calendar_name (optional), start, end.

    Returns:
        Markdown table of events.
    """
    try:
        acct = _get_account(params.account_id)
        cal = _get_calendar(acct, params.calendar_name)

        if params.start:
            start = datetime.fromisoformat(params.start)
        else:
            start = datetime.now()
        if params.end:
            end = datetime.fromisoformat(params.end)
        else:
            from datetime import timedelta
            end = start + timedelta(days=30)

        events = cal.date_search(start=start, end=end, expand=True)
        if not events:
            return f"No events found between {start.date()} and {end.date()}."

        formatted = [_format_event(e) for e in events]
        formatted.sort(key=lambda x: x.get("dtstart", ""))

        cal_name = cal.name or "(default)"
        lines = [
            f"# Events — {cal_name} ({acct.get('display_name') or params.account_id})",
            f"{start.date()} to {end.date()} ({len(formatted)} events)\n",
            "| Start | End | Summary | Location |",
            "|-------|-----|---------|----------|",
        ]
        for e in formatted:
            lines.append(
                f"| {e.get('dtstart', '')[:16]} | {e.get('dtend', '')[:16]} "
                f"| {e.get('summary', '')[:40]} | {e.get('location', '')[:30]} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="cal_get_event",
    annotations={
        "title": "Get Calendar Event",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def cal_get_event(params: CalGetEventInput) -> str:
    """Read the full details of a calendar event by UID.

    Args:
        params: account_id, uid, calendar_name.

    Returns:
        Full event details.
    """
    try:
        acct = _get_account(params.account_id)
        cal = _get_calendar(acct, params.calendar_name)
        event = cal.event_by_uid(params.uid)
        e = _format_event(event)
        lines = [
            f"# Event: {e.get('summary', 'N/A')}",
            f"**UID**: {e.get('uid', 'N/A')}",
            f"**Start**: {e.get('dtstart', 'N/A')}",
            f"**End**: {e.get('dtend', 'N/A')}",
        ]
        if e.get("location"):
            lines.append(f"**Location**: {e['location']}")
        if e.get("description"):
            lines.append(f"\n---\n\n{e['description']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="cal_create_event",
    annotations={
        "title": "Create Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def cal_create_event(params: CalCreateEventInput) -> str:
    """Create a new event on a CalDAV calendar.

    Args:
        params: account_id, summary, dtstart, dtend, calendar_name, location, description.

    Returns:
        Confirmation with event UID.
    """
    try:
        acct = _get_account(params.account_id)
        cal = _get_calendar(acct, params.calendar_name)

        import uuid
        uid = str(uuid.uuid4())
        dtstart = datetime.fromisoformat(params.dtstart)
        dtend = datetime.fromisoformat(params.dtend)

        vcal = vobject.iCalendar()
        vevent = vcal.add("vevent")
        vevent.add("uid").value = uid
        vevent.add("summary").value = params.summary
        vevent.add("dtstart").value = dtstart
        vevent.add("dtend").value = dtend
        if params.location:
            vevent.add("location").value = params.location
        if params.description:
            vevent.add("description").value = params.description

        cal.save_event(vcal.serialize())
        cal_name = cal.name or "(default)"
        return f"Event '{params.summary}' created on {cal_name} (UID: {uid})."
    except Exception as e:
        return f"Error creating event: {e}"


@mcp.tool(
    name="cal_update_event",
    annotations={
        "title": "Update Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def cal_update_event(params: CalUpdateEventInput) -> str:
    """Update an existing calendar event.

    Only the fields you provide will be changed.

    Args:
        params: account_id, uid, calendar_name, and optional fields to update.

    Returns:
        Confirmation message.
    """
    try:
        acct = _get_account(params.account_id)
        cal = _get_calendar(acct, params.calendar_name)
        event = cal.event_by_uid(params.uid)
        vcal = vobject.readOne(event.data)
        vevent = vcal.vevent

        if params.summary is not None:
            if hasattr(vevent, "summary"):
                vevent.summary.value = params.summary
            else:
                vevent.add("summary").value = params.summary
        if params.dtstart is not None:
            vevent.dtstart.value = datetime.fromisoformat(params.dtstart)
        if params.dtend is not None:
            vevent.dtend.value = datetime.fromisoformat(params.dtend)
        if params.location is not None:
            if hasattr(vevent, "location"):
                vevent.location.value = params.location
            else:
                vevent.add("location").value = params.location
        if params.description is not None:
            if hasattr(vevent, "description"):
                vevent.description.value = params.description
            else:
                vevent.add("description").value = params.description

        event.data = vcal.serialize()
        event.save()
        return f"Event '{params.uid}' updated."
    except Exception as e:
        return f"Error updating event: {e}"


@mcp.tool(
    name="cal_delete_event",
    annotations={
        "title": "Delete Calendar Event",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def cal_delete_event(params: CalDeleteEventInput) -> str:
    """Delete a calendar event by UID.

    Args:
        params: account_id, uid, calendar_name.

    Returns:
        Confirmation message.
    """
    try:
        acct = _get_account(params.account_id)
        cal = _get_calendar(acct, params.calendar_name)
        event = cal.event_by_uid(params.uid)
        event.delete()
        return f"Event '{params.uid}' deleted."
    except Exception as e:
        return f"Error deleting event: {e}"


# ---------------------------------------------------------------------------
# Tools — CardDAV (Contacts)
# ---------------------------------------------------------------------------

async def _carddav_propfind(acct: Dict[str, Any]) -> List[Dict[str, str]]:
    """List address books via PROPFIND on the CardDAV URL."""
    url, headers, auth = _carddav_headers(acct)
    body = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:displayname/>
    <d:resourcetype/>
  </d:prop>
</d:propfind>"""
    ssl_verify = not acct.get("dav_allow_insecure", False)
    async with safe_async_client(timeout=30, verify=ssl_verify, auth=auth) as client:
        resp = await client.request("PROPFIND", url, content=body, headers={**headers, "Depth": "1"})
        resp.raise_for_status()
    books = []
    try:
        root = ET.fromstring(resp.text)
        for response in root.iter("{DAV:}response"):
            href_el = response.find("{DAV:}href")
            name_el = response.find(".//{DAV:}displayname")
            rt = response.find(".//{urn:ietf:params:xml:ns:carddav}addressbook")
            if rt is not None and href_el is not None:
                href = href_el.text or ""
                name = name_el.text if name_el is not None else ""
                books.append({"name": name or "(unnamed)", "href": href})
    except ET.ParseError:
        pass
    return books


async def _carddav_list_vcards(acct: Dict[str, Any], book_href: str) -> List[Tuple[str, str]]:
    """List all vCards in an address book. Returns [(href, vcard_data), ...]."""
    base_url = acct.get("carddav_url", "")
    full_url = resolve_dav_url(base_url, book_href)

    body = """<?xml version="1.0" encoding="utf-8"?>
<card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:prop>
    <d:getetag/>
    <card:address-data/>
  </d:prop>
</card:addressbook-query>"""

    _, headers, auth = _carddav_headers(acct)
    ssl_verify = not acct.get("dav_allow_insecure", False)
    async with safe_async_client(timeout=30, verify=ssl_verify, auth=auth) as client:
        resp = await client.request("REPORT", full_url, content=body, headers={**headers, "Depth": "1"})
        resp.raise_for_status()

    vcards = []
    try:
        root = ET.fromstring(resp.text)
        for response in root.iter("{DAV:}response"):
            href_el = response.find("{DAV:}href")
            data_el = response.find(".//{urn:ietf:params:xml:ns:carddav}address-data")
            if href_el is not None and data_el is not None and data_el.text:
                vcards.append((href_el.text or "", data_el.text))
    except ET.ParseError:
        pass
    return vcards


async def _get_addressbook_href(acct: Dict[str, Any], name: Optional[str]) -> str:
    """Get the href of an address book by name, or the first available."""
    books = await _carddav_propfind(acct)
    if not books:
        raise ValueError(f"No address books found for account '{acct['id']}'.")
    if name:
        for b in books:
            if b["name"].lower() == name.lower():
                return b["href"]
        raise ValueError(
            f"Address book '{name}' not found. Available: "
            + ", ".join(b["name"] for b in books)
        )
    return books[0]["href"]


@mcp.tool(
    name="card_list_addressbooks",
    annotations={
        "title": "List Address Books",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_list_addressbooks(params: CardListAddressBooksInput) -> str:
    """List all CardDAV address books for an account.

    Args:
        params: Contains account_id.

    Returns:
        Markdown list of address book names.
    """
    try:
        acct = _get_account(params.account_id)
        books = await _carddav_propfind(acct)
        if not books:
            return f"No address books found for {params.account_id}."
        lines = [f"# Address Books — {acct.get('display_name') or params.account_id}\n"]
        for b in books:
            lines.append(f"- **{b['name']}**")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="card_list_contacts",
    annotations={
        "title": "List Contacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_list_contacts(params: CardListContactsInput) -> str:
    """List contacts in a CardDAV address book.

    Args:
        params: account_id, addressbook_name, limit.

    Returns:
        Markdown table of contacts.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)
        vcards = await _carddav_list_vcards(acct, book_href)

        if not vcards:
            return "No contacts found."

        contacts = [_format_contact(data) for _, data in vcards[:params.limit]]
        contacts.sort(key=lambda c: c.get("fn", "").lower())

        lines = [
            f"# Contacts ({len(contacts)} of {len(vcards)})\n",
            "| Name | Email | Phone | UID |",
            "|------|-------|-------|-----|",
        ]
        for c in contacts:
            lines.append(
                f"| {c.get('fn', '')[:30]} | {c.get('email', '')[:35]} "
                f"| {c.get('tel', '')[:20]} | {c.get('uid', '')[:15]} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="card_search_contacts",
    annotations={
        "title": "Search Contacts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_search_contacts(params: CardSearchContactsInput) -> str:
    """Search contacts by name, email, or phone.

    Client-side filtering against all contacts in the address book.

    Args:
        params: account_id, query, addressbook_name, limit.

    Returns:
        Markdown table of matching contacts.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)
        vcards = await _carddav_list_vcards(acct, book_href)
        if not vcards:
            return "No contacts found."

        q = params.query.lower()
        matches = []
        for _, data in vcards:
            if q in data.lower():
                matches.append(_format_contact(data))
            if len(matches) >= params.limit:
                break

        if not matches:
            return f"No contacts matching '{params.query}'."

        lines = [
            f"# Search Results: '{params.query}' ({len(matches)} matches)\n",
            "| Name | Email | Phone | UID |",
            "|------|-------|-------|-----|",
        ]
        for c in matches:
            lines.append(
                f"| {c.get('fn', '')[:30]} | {c.get('email', '')[:35]} "
                f"| {c.get('tel', '')[:20]} | {c.get('uid', '')[:15]} |"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="card_get_contact",
    annotations={
        "title": "Get Contact Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_get_contact(params: CardGetContactInput) -> str:
    """Read the full details of a contact by UID.

    Args:
        params: account_id, uid, addressbook_name.

    Returns:
        Full contact details.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)
        vcards = await _carddav_list_vcards(acct, book_href)
        for _, data in vcards:
            try:
                vc = vobject.readOne(data)
                if hasattr(vc, "uid") and str(vc.uid.value) == params.uid:
                    c = _format_contact(data)
                    lines = [f"# Contact: {c.get('fn', 'N/A')}"]
                    lines.append(f"**UID**: {c.get('uid', 'N/A')}")
                    if c.get("email"):
                        lines.append(f"**Email**: {c['email']}")
                    if c.get("tel"):
                        lines.append(f"**Phone**: {c['tel']}")
                    if c.get("org"):
                        lines.append(f"**Organization**: {c['org']}")
                    if c.get("title"):
                        lines.append(f"**Title**: {c['title']}")
                    return "\n".join(lines)
            except Exception:
                continue
        return f"Contact with UID '{params.uid}' not found."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool(
    name="card_create_contact",
    annotations={
        "title": "Create Contact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def card_create_contact(params: CardCreateContactInput) -> str:
    """Create a new contact in a CardDAV address book.

    Args:
        params: account_id, fn, email, tel, org, title, addressbook_name.

    Returns:
        Confirmation with contact UID.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)

        import uuid
        uid = str(uuid.uuid4())

        vc = vobject.vCard()
        vc.add("uid").value = uid
        vc.add("fn").value = params.fn
        # Structured name
        n = vc.add("n")
        parts = params.fn.rsplit(" ", 1)
        if len(parts) == 2:
            n.value = vobject.vcard.Name(family=parts[1], given=parts[0])
        else:
            n.value = vobject.vcard.Name(family=params.fn)

        if params.email:
            for addr in params.email.split(","):
                vc.add("email").value = addr.strip()
        if params.tel:
            for num in params.tel.split(","):
                vc.add("tel").value = num.strip()
        if params.org:
            vc.add("org").value = [params.org]
        if params.title:
            vc.add("title").value = params.title

        vcard_data = vc.serialize()

        base_url = acct.get("carddav_url", "")
        put_url = resolve_dav_url(base_url, f"{book_href.rstrip('/')}/{uid}.vcf")

        _, _, auth_obj = _carddav_headers(acct)
        ssl_verify = not acct.get("dav_allow_insecure", False)
        # resolve_dav_url() pins the URL host to the configured carddav_url, so
        # a compromised DAV server cannot redirect this auth'd PUT to an attacker.
        # Covered by tests/test_dav_url_pinning.py.
        async with safe_async_client(timeout=30, verify=ssl_verify, auth=auth_obj) as client:
            resp = await client.put(  # nosemgrep: python.mcp.mcp-auth-passthrough-taint.mcp-auth-passthrough-taint
                put_url,
                content=vcard_data,
                headers={"Content-Type": "text/vcard; charset=utf-8"},
            )
            if resp.status_code not in (200, 201, 204):
                return f"Error: Server returned {resp.status_code}: {resp.text[:200]}"

        return f"Contact '{params.fn}' created (UID: {uid})."
    except Exception as e:
        return f"Error creating contact: {e}"


@mcp.tool(
    name="card_update_contact",
    annotations={
        "title": "Update Contact",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_update_contact(params: CardUpdateContactInput) -> str:
    """Update an existing contact's fields.

    Only the fields you provide will be changed.

    Args:
        params: account_id, uid, addressbook_name, and optional fields.

    Returns:
        Confirmation message.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)
        vcards = await _carddav_list_vcards(acct, book_href)

        target_href = None
        target_data = None
        for href, data in vcards:
            try:
                vc = vobject.readOne(data)
                if hasattr(vc, "uid") and str(vc.uid.value) == params.uid:
                    target_href = href
                    target_data = data
                    break
            except Exception:
                continue

        if not target_data:
            return f"Contact with UID '{params.uid}' not found."

        vc = vobject.readOne(target_data)
        if params.fn is not None:
            vc.fn.value = params.fn
        if params.email is not None:
            # Remove existing emails and add new ones
            vc.contents["email"] = []
            for addr in params.email.split(","):
                vc.add("email").value = addr.strip()
        if params.tel is not None:
            vc.contents["tel"] = []
            for num in params.tel.split(","):
                vc.add("tel").value = num.strip()
        if params.org is not None:
            if hasattr(vc, "org"):
                vc.org.value = [params.org]
            else:
                vc.add("org").value = [params.org]
        if params.title is not None:
            if hasattr(vc, "title"):
                vc.title.value = params.title
            else:
                vc.add("title").value = params.title

        # PUT updated vCard
        base_url = acct.get("carddav_url", "")
        put_url = resolve_dav_url(
            base_url,
            target_href or f"{book_href.rstrip('/')}/{params.uid}.vcf",
        )

        _, _, auth_obj = _carddav_headers(acct)
        ssl_verify = not acct.get("dav_allow_insecure", False)
        # resolve_dav_url() pins the URL host to the configured carddav_url, so
        # a compromised DAV server cannot redirect this auth'd PUT to an attacker.
        # Covered by tests/test_dav_url_pinning.py.
        async with safe_async_client(timeout=30, verify=ssl_verify, auth=auth_obj) as client:
            resp = await client.put(  # nosemgrep: python.mcp.mcp-auth-passthrough-taint.mcp-auth-passthrough-taint
                put_url,
                content=vc.serialize(),
                headers={"Content-Type": "text/vcard; charset=utf-8"},
            )
            if resp.status_code not in (200, 201, 204):
                return f"Error: Server returned {resp.status_code}: {resp.text[:200]}"

        return f"Contact '{params.uid}' updated."
    except Exception as e:
        return f"Error updating contact: {e}"


@mcp.tool(
    name="card_delete_contact",
    annotations={
        "title": "Delete Contact",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def card_delete_contact(params: CardDeleteContactInput) -> str:
    """Delete a contact by UID.

    Args:
        params: account_id, uid, addressbook_name.

    Returns:
        Confirmation message.
    """
    try:
        acct = _get_account(params.account_id)
        book_href = await _get_addressbook_href(acct, params.addressbook_name)
        vcards = await _carddav_list_vcards(acct, book_href)

        target_href = None
        for href, data in vcards:
            try:
                vc = vobject.readOne(data)
                if hasattr(vc, "uid") and str(vc.uid.value) == params.uid:
                    target_href = href
                    break
            except Exception:
                continue

        if not target_href:
            return f"Contact with UID '{params.uid}' not found."

        base_url = acct.get("carddav_url", "")
        del_url = resolve_dav_url(base_url, target_href)

        _, _, auth_obj = _carddav_headers(acct)
        ssl_verify = not acct.get("dav_allow_insecure", False)
        async with safe_async_client(timeout=30, verify=ssl_verify, auth=auth_obj) as client:
            resp = await client.delete(del_url, headers={})
            if resp.status_code not in (200, 204):
                return f"Error: Server returned {resp.status_code}: {resp.text[:200]}"

        return f"Contact '{params.uid}' deleted."
    except Exception as e:
        return f"Error deleting contact: {e}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
