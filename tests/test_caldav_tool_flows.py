"""Tests for the CalDAV-backed tool flows: ``cal_list_calendars``,
``cal_list_events``, ``cal_get_event``, ``cal_create_event``,
``cal_update_event``, ``cal_delete_event``.

The ``caldav`` library is large and stateful — we don't drive it for real,
we monkeypatch ``_caldav_client`` and ``_get_calendar`` with hand-rolled
fakes that record what the tool layer asks of them.
"""
from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime
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
    "display_name": "Work",
    "email_address": "me@example.com",
    "username": "me@example.com",
    "password": "pw",
    "imap_host": "imap.example.com",
    "caldav_url": "https://dav.example.com/dav/",
}


def _ical(uid, summary, dtstart="20260513T100000Z", dtend="20260513T110000Z", location="", description=""):
    parts = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//EN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
    ]
    if location:
        parts.append(f"LOCATION:{location}")
    if description:
        parts.append(f"DESCRIPTION:{description}")
    parts.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(parts) + "\r\n"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeEvent:
    """Stand-in for caldav.Event — exposes .data and .save/.delete."""
    def __init__(self, data):
        self.data = data
        self.saved = 0
        self.deleted = False

    def save(self):
        self.saved += 1

    def delete(self):
        self.deleted = True


class _FakeCalendar:
    def __init__(self, name, events=None, url="https://dav.example.com/dav/cal/"):
        self.name = name
        self.url = url
        self._events = events or []
        self.saved_events = []  # serialized iCal strings passed to save_event

    def date_search(self, start, end, expand=True):
        # Honor the start/end range against the event's parsed DTSTART
        out = []
        for ev in self._events:
            line = next((l for l in ev.data.splitlines() if l.startswith("DTSTART")), "")
            if not line:
                out.append(ev)
                continue
            dt_text = line.split(":", 1)[1]
            try:
                dt = datetime.strptime(dt_text, "%Y%m%dT%H%M%SZ")
            except ValueError:
                out.append(ev)
                continue
            if start <= dt <= end:
                out.append(ev)
        return out

    def event_by_uid(self, uid):
        for ev in self._events:
            if f"UID:{uid}" in ev.data:
                return ev
        raise KeyError(f"event {uid} not found")

    def save_event(self, ical_text):
        self.saved_events.append(ical_text)


class _FakePrincipal:
    def __init__(self, calendars):
        self._calendars = calendars

    def calendars(self):
        return self._calendars


class _FakeClient:
    def __init__(self, calendars):
        self._principal = _FakePrincipal(calendars)

    def principal(self):
        return self._principal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stub_account(monkeypatch):
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)


@pytest.fixture
def cal_with_events(monkeypatch, stub_account):
    """Single calendar 'Work' populated with three events in May 2026."""
    cal = _FakeCalendar("Work", events=[
        _FakeEvent(_ical("ev-1", "Standup", "20260513T100000Z", "20260513T103000Z")),
        _FakeEvent(_ical("ev-2", "Review", "20260514T140000Z", "20260514T150000Z", location="Room B")),
        _FakeEvent(_ical("ev-3", "Planning", "20260520T090000Z", "20260520T100000Z")),
    ])
    monkeypatch.setattr(email_mcp, "_get_calendar", lambda acct, name=None: cal)
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient([cal]))
    return cal


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# cal_list_calendars
# ---------------------------------------------------------------------------

def test_list_calendars_renders_names_and_urls(stub_account, monkeypatch):
    cals = [
        _FakeCalendar("Work", url="https://dav.example.com/dav/work/"),
        _FakeCalendar("Personal", url="https://dav.example.com/dav/personal/"),
    ]
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient(cals))
    result = run(email_mcp.cal_list_calendars(
        email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
    ))
    assert "# Calendars" in result
    assert "**Work**" in result
    assert "**Personal**" in result
    assert "https://dav.example.com/dav/work/" in result


def test_list_calendars_empty_returns_friendly_message(stub_account, monkeypatch):
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient([]))
    result = run(email_mcp.cal_list_calendars(
        email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
    ))
    assert "No calendars found" in result


# ---------------------------------------------------------------------------
# cal_list_events
# ---------------------------------------------------------------------------

def test_list_events_filters_by_explicit_range(cal_with_events):
    result = run(email_mcp.cal_list_events(email_mcp.CalListEventsInput(
        account_id=ACCT_ID,
        start="2026-05-13T00:00:00",
        end="2026-05-14T23:59:59",
    )))
    assert "Standup" in result
    assert "Review" in result
    assert "Planning" not in result  # outside the window


def test_list_events_renders_table_header(cal_with_events):
    result = run(email_mcp.cal_list_events(email_mcp.CalListEventsInput(
        account_id=ACCT_ID,
        start="2026-05-01T00:00:00",
        end="2026-05-31T23:59:59",
    )))
    assert "| Start | End | Summary | Location |" in result
    assert "Room B" in result  # location surfaces in the cell


def test_list_events_empty_window_friendly_message(cal_with_events):
    result = run(email_mcp.cal_list_events(email_mcp.CalListEventsInput(
        account_id=ACCT_ID,
        start="2027-01-01T00:00:00",
        end="2027-01-31T00:00:00",
    )))
    assert "No events found" in result


def test_list_events_sorted_by_dtstart(cal_with_events):
    result = run(email_mcp.cal_list_events(email_mcp.CalListEventsInput(
        account_id=ACCT_ID,
        start="2026-05-01T00:00:00",
        end="2026-05-31T23:59:59",
    )))
    standup_idx = result.index("Standup")
    review_idx = result.index("Review")
    planning_idx = result.index("Planning")
    assert standup_idx < review_idx < planning_idx


# ---------------------------------------------------------------------------
# cal_get_event
# ---------------------------------------------------------------------------

def test_get_event_returns_full_details(cal_with_events):
    result = run(email_mcp.cal_get_event(email_mcp.CalGetEventInput(
        account_id=ACCT_ID, uid="ev-2",
    )))
    assert "# Event: Review" in result
    assert "**UID**: ev-2" in result
    assert "Room B" in result


def test_get_event_missing_uid_surfaces_error(cal_with_events):
    result = run(email_mcp.cal_get_event(email_mcp.CalGetEventInput(
        account_id=ACCT_ID, uid="ghost",
    )))
    assert result.lower().startswith("error")


# ---------------------------------------------------------------------------
# cal_create_event
# ---------------------------------------------------------------------------

def test_create_event_calls_save_event_with_serialised_ical(cal_with_events):
    result = run(email_mcp.cal_create_event(email_mcp.CalCreateEventInput(
        account_id=ACCT_ID,
        summary="Coffee chat",
        dtstart="2026-06-01T10:00:00",
        dtend="2026-06-01T10:30:00",
        location="Cafe",
    )))
    assert "Event 'Coffee chat' created" in result
    assert len(cal_with_events.saved_events) == 1
    payload = cal_with_events.saved_events[0]
    assert "BEGIN:VCALENDAR" in payload
    assert "SUMMARY:Coffee chat" in payload
    assert "LOCATION:Cafe" in payload


def test_create_event_rejects_bad_iso_date(cal_with_events):
    result = run(email_mcp.cal_create_event(email_mcp.CalCreateEventInput(
        account_id=ACCT_ID,
        summary="oops",
        dtstart="not-a-date",
        dtend="2026-06-01T11:00:00",
    )))
    assert result.lower().startswith("error")
    assert cal_with_events.saved_events == []


# ---------------------------------------------------------------------------
# cal_update_event
# ---------------------------------------------------------------------------

def test_update_event_changes_summary(cal_with_events):
    target = cal_with_events._events[0]  # ev-1
    result = run(email_mcp.cal_update_event(email_mcp.CalUpdateEventInput(
        account_id=ACCT_ID, uid="ev-1", summary="Daily Standup (renamed)",
    )))
    assert "updated" in result.lower()
    assert "Daily Standup (renamed)" in target.data
    assert target.saved == 1


# ---------------------------------------------------------------------------
# cal_delete_event
# ---------------------------------------------------------------------------

def test_delete_event_marks_event_deleted(cal_with_events):
    target = cal_with_events._events[1]  # ev-2
    result = run(email_mcp.cal_delete_event(email_mcp.CalDeleteEventInput(
        account_id=ACCT_ID, uid="ev-2",
    )))
    assert "deleted" in result.lower()
    assert target.deleted is True


def test_delete_event_missing_uid_surfaces_error(cal_with_events):
    result = run(email_mcp.cal_delete_event(email_mcp.CalDeleteEventInput(
        account_id=ACCT_ID, uid="ghost",
    )))
    assert result.lower().startswith("error")
