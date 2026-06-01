"""Tests for ``_format_event`` and ``_format_contact`` — the iCalendar /
vCard summary formatters that feed the calendar and contact MCP tools.

We construct minimal real iCalendar / vCard payloads (vobject can parse them)
and wrap them in a tiny ``_FakeEvent`` for the CalDAV side.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)

_format_event = email_mcp._format_event
_format_contact = email_mcp._format_contact


class _FakeEvent:
    """Stand-in for caldav.Event — only .data is read by _format_event."""

    def __init__(self, data):
        self.data = data


# ---------------------------------------------------------------------------
# _format_event — iCalendar
# ---------------------------------------------------------------------------

ICAL_FULL = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:event-1@example.com
SUMMARY:Team standup
DTSTART:20260513T100000Z
DTEND:20260513T103000Z
LOCATION:Conference room B
DESCRIPTION:Daily sync
END:VEVENT
END:VCALENDAR
"""


def test_format_event_full_record():
    out = _format_event(_FakeEvent(ICAL_FULL))
    assert out["uid"] == "event-1@example.com"
    assert out["summary"] == "Team standup"
    assert "2026-05-13" in out["dtstart"]
    assert "2026-05-13" in out["dtend"]
    assert out["location"] == "Conference room B"
    assert out["description"] == "Daily sync"


def test_format_event_minimal_record_has_blank_optionals():
    ical = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:bare@example.com
SUMMARY:Just a title
END:VEVENT
END:VCALENDAR
"""
    out = _format_event(_FakeEvent(ical))
    assert out["uid"] == "bare@example.com"
    assert out["summary"] == "Just a title"
    assert out["dtstart"] == ""
    assert out["dtend"] == ""
    assert out["location"] == ""
    assert out["description"] == ""


def test_format_event_missing_uid_returns_blank_uid():
    """Regression: previous code used ``{getattr(...)}`` (a set literal) as the
    getattr default, so a UID-less event returned the string ``"{''}"`` instead
    of ``""``."""
    ical = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
SUMMARY:No UID here
END:VEVENT
END:VCALENDAR
"""
    out = _format_event(_FakeEvent(ical))
    assert out["uid"] == ""
    assert out["summary"] == "No UID here"


def test_format_event_garbage_returns_parse_error():
    out = _format_event(_FakeEvent("not a calendar"))
    assert out["summary"] == "(parse error)"
    assert "raw" in out


# ---------------------------------------------------------------------------
# _format_contact — vCard
# ---------------------------------------------------------------------------

VCARD_FULL = """BEGIN:VCARD
VERSION:3.0
UID:contact-1
FN:Alice Example
N:Example;Alice;;;
EMAIL;TYPE=INTERNET:alice@example.com
TEL;TYPE=CELL:+15551234567
ORG:Acme Co
TITLE:Engineer
END:VCARD
"""


def test_format_contact_full_record():
    out = _format_contact(VCARD_FULL)
    assert out["uid"] == "contact-1"
    assert out["fn"] == "Alice Example"
    assert out["email"] == "alice@example.com"
    assert out["tel"] == "+15551234567"
    assert out["org"] == "Acme Co"
    assert out["title"] == "Engineer"


def test_format_contact_multiple_emails_and_tels_joined():
    vcard = """BEGIN:VCARD
VERSION:3.0
UID:c-multi
FN:Bob Sample
N:Sample;Bob;;;
EMAIL;TYPE=INTERNET:bob@example.com
EMAIL;TYPE=INTERNET:bob.alt@example.org
TEL;TYPE=CELL:+15550001
TEL;TYPE=WORK:+15550002
END:VCARD
"""
    out = _format_contact(vcard)
    assert out["email"] == "bob@example.com, bob.alt@example.org"
    assert out["tel"] == "+15550001, +15550002"


def test_format_contact_minimal_only_required_fields():
    """Just FN, no UID / email / tel / org / title."""
    vcard = """BEGIN:VCARD
VERSION:3.0
FN:Just A Name
N:Name;Just A;;;
END:VCARD
"""
    out = _format_contact(vcard)
    assert out["fn"] == "Just A Name"
    assert out["uid"] == ""
    assert "email" not in out  # not added when missing — caller can use .get()
    assert "tel" not in out
    assert "org" not in out
    assert "title" not in out


def test_format_contact_garbage_returns_parse_error():
    out = _format_contact("not a vcard")
    assert out["fn"] == "(parse error)"
    assert "raw" in out
