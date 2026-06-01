"""Cross-tool guard for the ``display_name=None`` heading regression.

Several tool headings use ``acct.get('display_name', params.account_id)`` as a
"default if missing" idiom — but a Pydantic ``Optional[str] = None`` field
serializes to ``"display_name": null``, so the key IS present after load.
``dict.get`` then returns ``None``, the heading renders ``for None``, and the
fallback never fires. See CLAUDE.md "Recurring gotchas".

Bug already fixed once in ``email_list_accounts`` (line 1139); this test pins
the remaining six sibling sites so the fix can't regress.
"""

from __future__ import annotations

import asyncio
import email.mime.text
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)


ACCT_ID = "work"
ACCT = {
    "id": ACCT_ID,
    "display_name": None,  # the regression trigger
    "email_address": "me@example.com",
    "username": "me@example.com",
    "password": "pw",
    "imap_host": "imap.example.com",
    "caldav_url": "https://dav.example.com/dav/",
    "carddav_url": "https://dav.example.com/dav/",
}


# --- Minimal fakes for each integration seam ------------------------------


def _msg_bytes():
    msg = email.mime.text.MIMEText("body", "plain", "utf-8")
    msg["From"] = "alice@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = "hi"
    msg["Date"] = "Mon, 13 May 2026 12:00:00 +0000"
    msg["Message-ID"] = "<x@example.com>"
    return msg.as_bytes()


class _StubIMAP:
    """IMAP fake that returns one message so the heading path executes."""

    def __init__(self):
        self.logged_out = False

    def select(self, folder, readonly=False):
        return ("OK", [b""])

    def list(self, directory='""', pattern="*"):
        return ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            return ("OK", [b"1"])
        if cmd == "FETCH":
            uid = args[0]
            return ("OK", [(b"" + uid.encode() + b" (RFC822 {len})", _msg_bytes())])
        return ("NO", [None])

    def logout(self):
        self.logged_out = True


class _StubSieve:
    """ManageSieve fake with one inactive script so the listing renders."""

    def listscripts(self):
        return ("OK", [("rules", False)])

    def logout(self):
        pass


class _StubCalendar:
    name = "Work"
    url = "https://dav.example.com/dav/cal/"

    def date_search(self, start, end, expand=True):
        # Return a single in-range event so cal_list_events emits the heading.
        return [_StubEvent()]


class _StubEvent:
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        "UID:ev-1\r\nSUMMARY:Standup\r\n"
        "DTSTART:20260513T100000Z\r\nDTEND:20260513T103000Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class _StubPrincipal:
    def calendars(self):
        return [_StubCalendar()]


class _StubClient:
    def principal(self):
        return _StubPrincipal()


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def stubbed(monkeypatch):
    """Install null-display-name account + happy-path stubs for every seam."""
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: dict(ACCT))
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: _StubIMAP())
    monkeypatch.setattr(email_mcp, "_sieve_connect", lambda acct: _StubSieve())
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _StubClient())
    monkeypatch.setattr(
        email_mcp, "_get_calendar", lambda acct, name=None: _StubCalendar()
    )

    async def fake_propfind(acct):
        return [
            {"name": "Personal", "href": "https://dav.example.com/dav/abooks/personal/"}
        ]

    monkeypatch.setattr(email_mcp, "_carddav_propfind", fake_propfind)


def run(coro):
    return asyncio.run(coro)


# --- The cross-tool guard -------------------------------------------------

# (label, callable returning the coroutine) — one entry per sibling tool that
# renders a heading using ``acct.get('display_name', params.account_id)``.
TOOL_CALLS = [
    (
        "email_list_messages",
        lambda: email_mcp.email_list_messages(
            email_mcp.ListEmailsInput(account_id=ACCT_ID, folder="INBOX")
        ),
    ),
    (
        "email_search_messages",
        lambda: email_mcp.email_search_messages(
            email_mcp.SearchEmailsInput(account_id=ACCT_ID, query="ALL", folder="INBOX")
        ),
    ),
    (
        "email_sieve_list",
        lambda: email_mcp.email_sieve_list(
            email_mcp.SieveListInput(account_id=ACCT_ID)
        ),
    ),
    (
        "cal_list_calendars",
        lambda: email_mcp.cal_list_calendars(
            email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
        ),
    ),
    (
        "cal_list_events",
        lambda: email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2026-05-01T00:00:00",
                end="2026-05-31T23:59:59",
            )
        ),
    ),
    (
        "card_list_addressbooks",
        lambda: email_mcp.card_list_addressbooks(
            email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
        ),
    ),
]


@pytest.mark.parametrize("label,make_coro", TOOL_CALLS, ids=[t[0] for t in TOOL_CALLS])
def test_headings_use_account_id_when_display_name_is_none(stubbed, label, make_coro):
    """All six sibling tools must fall back to ``account_id`` in the heading
    when ``display_name`` is ``None``. ``"None"`` must never appear in the
    rendered output.
    """
    result = run(make_coro())
    assert ACCT_ID in result, (
        f"{label}: expected account_id '{ACCT_ID}' in heading, got: {result!r}"
    )
    assert "None" not in result, (
        f"{label}: literal 'None' leaked into output: {result!r}"
    )
