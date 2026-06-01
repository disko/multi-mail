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


def _ical(
    uid,
    summary,
    dtstart="20260513T100000Z",
    dtend="20260513T110000Z",
    location="",
    description="",
):
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
            line = next(
                (ln for ln in ev.data.splitlines() if ln.startswith("DTSTART")), ""
            )
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
    cal = _FakeCalendar(
        "Work",
        events=[
            _FakeEvent(
                _ical("ev-1", "Standup", "20260513T100000Z", "20260513T103000Z")
            ),
            _FakeEvent(
                _ical(
                    "ev-2",
                    "Review",
                    "20260514T140000Z",
                    "20260514T150000Z",
                    location="Room B",
                )
            ),
            _FakeEvent(
                _ical("ev-3", "Planning", "20260520T090000Z", "20260520T100000Z")
            ),
        ],
    )
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
    result = run(
        email_mcp.cal_list_calendars(
            email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
        )
    )
    assert "# Calendars" in result
    assert "**Work**" in result
    assert "**Personal**" in result
    assert "https://dav.example.com/dav/work/" in result


def test_list_calendars_empty_returns_friendly_message(stub_account, monkeypatch):
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient([]))
    result = run(
        email_mcp.cal_list_calendars(
            email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
        )
    )
    assert "No calendars found" in result


# ---------------------------------------------------------------------------
# cal_list_events
# ---------------------------------------------------------------------------


def test_list_events_filters_by_explicit_range(cal_with_events):
    result = run(
        email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2026-05-13T00:00:00",
                end="2026-05-14T23:59:59",
            )
        )
    )
    assert "Standup" in result
    assert "Review" in result
    assert "Planning" not in result  # outside the window


def test_list_events_renders_table_header(cal_with_events):
    result = run(
        email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2026-05-01T00:00:00",
                end="2026-05-31T23:59:59",
            )
        )
    )
    assert "| Start | End | Summary | Location |" in result
    assert "Room B" in result  # location surfaces in the cell


def test_list_events_empty_window_friendly_message(cal_with_events):
    result = run(
        email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2027-01-01T00:00:00",
                end="2027-01-31T00:00:00",
            )
        )
    )
    assert "No events found" in result


def test_list_events_sorted_by_dtstart(cal_with_events):
    result = run(
        email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2026-05-01T00:00:00",
                end="2026-05-31T23:59:59",
            )
        )
    )
    standup_idx = result.index("Standup")
    review_idx = result.index("Review")
    planning_idx = result.index("Planning")
    assert standup_idx < review_idx < planning_idx


# ---------------------------------------------------------------------------
# cal_get_event
# ---------------------------------------------------------------------------


def test_get_event_returns_full_details(cal_with_events):
    result = run(
        email_mcp.cal_get_event(
            email_mcp.CalGetEventInput(
                account_id=ACCT_ID,
                uid="ev-2",
            )
        )
    )
    assert "# Event: Review" in result
    assert "**UID**: ev-2" in result
    assert "Room B" in result


def test_get_event_missing_uid_surfaces_error(cal_with_events):
    result = run(
        email_mcp.cal_get_event(
            email_mcp.CalGetEventInput(
                account_id=ACCT_ID,
                uid="ghost",
            )
        )
    )
    assert result.lower().startswith("error")


# ---------------------------------------------------------------------------
# cal_create_event
# ---------------------------------------------------------------------------


def test_create_event_calls_save_event_with_serialised_ical(cal_with_events):
    result = run(
        email_mcp.cal_create_event(
            email_mcp.CalCreateEventInput(
                account_id=ACCT_ID,
                summary="Coffee chat",
                dtstart="2026-06-01T10:00:00",
                dtend="2026-06-01T10:30:00",
                location="Cafe",
            )
        )
    )
    assert "Event 'Coffee chat' created" in result
    assert len(cal_with_events.saved_events) == 1
    payload = cal_with_events.saved_events[0]
    assert "BEGIN:VCALENDAR" in payload
    assert "SUMMARY:Coffee chat" in payload
    assert "LOCATION:Cafe" in payload


def test_create_event_rejects_bad_iso_date(cal_with_events):
    result = run(
        email_mcp.cal_create_event(
            email_mcp.CalCreateEventInput(
                account_id=ACCT_ID,
                summary="oops",
                dtstart="not-a-date",
                dtend="2026-06-01T11:00:00",
            )
        )
    )
    assert result.lower().startswith("error")
    assert cal_with_events.saved_events == []


# ---------------------------------------------------------------------------
# cal_update_event
# ---------------------------------------------------------------------------


def test_update_event_changes_summary(cal_with_events):
    target = cal_with_events._events[0]  # ev-1
    result = run(
        email_mcp.cal_update_event(
            email_mcp.CalUpdateEventInput(
                account_id=ACCT_ID,
                uid="ev-1",
                summary="Daily Standup (renamed)",
            )
        )
    )
    assert "updated" in result.lower()
    assert "Daily Standup (renamed)" in target.data
    assert target.saved == 1


# ---------------------------------------------------------------------------
# cal_delete_event
# ---------------------------------------------------------------------------


def test_delete_event_marks_event_deleted(cal_with_events):
    target = cal_with_events._events[1]  # ev-2
    result = run(
        email_mcp.cal_delete_event(
            email_mcp.CalDeleteEventInput(
                account_id=ACCT_ID,
                uid="ev-2",
            )
        )
    )
    assert "deleted" in result.lower()
    assert target.deleted is True


def test_delete_event_missing_uid_surfaces_error(cal_with_events):
    result = run(
        email_mcp.cal_delete_event(
            email_mcp.CalDeleteEventInput(
                account_id=ACCT_ID,
                uid="ghost",
            )
        )
    )
    assert result.lower().startswith("error")


# ---------------------------------------------------------------------------
# Outer-except tail sweep + body-branch coverage (coverage iter-4, issue #8)
#
# Each cal_* tool wraps its body in a ``try: ... except Exception as e:``
# return. The happy-path tests above exercise the inner try; these tests
# inject a raising connect helper so the outer except fires. We also fold
# in three body-branch tests that are too cheap to defer:
#   - cal_list_events default-window when no dates supplied
#   - cal_get_event description-line branch
#   - cal_update_event non-summary field branches
# ---------------------------------------------------------------------------


def _raising_caldav_client(monkeypatch, exc):
    def boom(acct):
        raise exc

    monkeypatch.setattr(email_mcp, "_caldav_client", boom)


def _raising_get_calendar(monkeypatch, exc):
    def boom(acct, name=None):
        raise exc

    monkeypatch.setattr(email_mcp, "_get_calendar", boom)


def test_list_calendars_outer_except_returns_error(stub_account, monkeypatch):
    _raising_caldav_client(monkeypatch, RuntimeError("caldav unreachable"))
    result = run(
        email_mcp.cal_list_calendars(
            email_mcp.CalListCalendarsInput(account_id=ACCT_ID)
        )
    )
    assert result.startswith("Error:")
    assert "caldav unreachable" in result


def test_list_events_outer_except_returns_error(stub_account, monkeypatch):
    _raising_get_calendar(monkeypatch, RuntimeError("get-cal boom"))
    result = run(
        email_mcp.cal_list_events(
            email_mcp.CalListEventsInput(
                account_id=ACCT_ID,
                start="2026-05-01T00:00:00",
                end="2026-05-31T23:59:59",
            )
        )
    )
    assert result.startswith("Error:")
    assert "get-cal boom" in result


def test_list_events_default_window_when_no_dates_given(cal_with_events):
    """No start/end → defaults to (now, now + 30d). Fixture events are
    fixed-date in May 2026, so unless 'now' falls inside that window the
    result is the empty-window message; either way the default-window
    body branches execute (lines 2672, 2676-2677)."""
    result = run(
        email_mcp.cal_list_events(email_mcp.CalListEventsInput(account_id=ACCT_ID))
    )
    # Either "No events found ..." OR the table header — both prove the
    # default-window arms ran without raising.
    assert ("No events found" in result) or ("| Start | End |" in result)


def test_create_event_outer_except_returns_error(stub_account, monkeypatch):
    _raising_get_calendar(monkeypatch, RuntimeError("create boom"))
    result = run(
        email_mcp.cal_create_event(
            email_mcp.CalCreateEventInput(
                account_id=ACCT_ID,
                summary="x",
                dtstart="2026-06-01T10:00:00",
                dtend="2026-06-01T11:00:00",
            )
        )
    )
    assert result.startswith("Error creating event")
    assert "create boom" in result


def test_get_event_outer_except_returns_error_via_helper(stub_account, monkeypatch):
    """Helper-raises variant of get_event's outer except — distinct from
    the existing test_get_event_missing_uid_surfaces_error which reaches
    the same except via cal.event_by_uid raising."""
    _raising_get_calendar(monkeypatch, RuntimeError("getevt boom"))
    result = run(
        email_mcp.cal_get_event(
            email_mcp.CalGetEventInput(
                account_id=ACCT_ID,
                uid="any",
            )
        )
    )
    assert result.startswith("Error:")
    assert "getevt boom" in result


def test_get_event_renders_description_when_present(stub_account, monkeypatch):
    """Pin the ``if e.get('description'): ...`` branch (lines 2735-2736)."""
    cal = _FakeCalendar(
        "Work",
        events=[
            _FakeEvent(
                _ical(
                    "with-desc",
                    "Detailed event",
                    description="Lots of details on this one",
                )
            ),
        ],
    )
    monkeypatch.setattr(email_mcp, "_get_calendar", lambda acct, name=None: cal)
    result = run(
        email_mcp.cal_get_event(
            email_mcp.CalGetEventInput(
                account_id=ACCT_ID,
                uid="with-desc",
            )
        )
    )
    assert "Lots of details on this one" in result
    assert "---" in result  # separator before description block


def test_update_event_outer_except_returns_error(stub_account, monkeypatch):
    _raising_get_calendar(monkeypatch, RuntimeError("update boom"))
    result = run(
        email_mcp.cal_update_event(
            email_mcp.CalUpdateEventInput(
                account_id=ACCT_ID,
                uid="x",
                summary="x",
            )
        )
    )
    assert result.startswith("Error updating event")
    assert "update boom" in result


def test_update_event_changes_dtstart_dtend_location_description(cal_with_events):
    """Pin the non-summary body branches (lines 2821-2834): when
    dtstart/dtend/location/description are supplied, each branch's
    add/overwrite arm executes. ev-1 has no LOCATION or DESCRIPTION so the
    'add' (no hasattr) arm runs for those; dtstart/dtend always exist so
    the 'overwrite' arm runs."""
    target = cal_with_events._events[0]  # ev-1, no location, no description
    result = run(
        email_mcp.cal_update_event(
            email_mcp.CalUpdateEventInput(
                account_id=ACCT_ID,
                uid="ev-1",
                dtstart="2026-05-13T12:00:00",
                dtend="2026-05-13T13:00:00",
                location="Building A",
                description="Updated description",
            )
        )
    )
    assert "updated" in result.lower()
    assert target.saved == 1
    # vobject may reformat output; check the substrings made it into the data.
    assert "Building A" in target.data
    assert "Updated description" in target.data


def test_delete_event_outer_except_returns_error(stub_account, monkeypatch):
    _raising_get_calendar(monkeypatch, RuntimeError("del boom"))
    result = run(
        email_mcp.cal_delete_event(
            email_mcp.CalDeleteEventInput(
                account_id=ACCT_ID,
                uid="x",
            )
        )
    )
    assert result.startswith("Error deleting event")
    assert "del boom" in result


# ---------------------------------------------------------------------------
# _get_calendar + cal_create_event + cal_update_event mop-up (#8 iter-6)
# ---------------------------------------------------------------------------


def test_get_calendar_raises_when_no_calendars(stub_account, monkeypatch):
    """Empty principal.calendars() → ValueError("No calendars found …").
    Pins lines 2596-2597."""
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient([]))
    with pytest.raises(ValueError, match="No calendars found"):
        email_mcp._get_calendar(ACCT, name=None)


def test_get_calendar_raises_when_named_calendar_missing(stub_account, monkeypatch):
    """Requested name not in the calendar list → ValueError listing the
    available names. Pins lines 2602-2606."""
    cals = [
        _FakeCalendar("Work", url="https://dav.example.com/dav/work/"),
        _FakeCalendar("Personal", url="https://dav.example.com/dav/personal/"),
    ]
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient(cals))
    with pytest.raises(ValueError) as excinfo:
        email_mcp._get_calendar(ACCT, name="Nonexistent")
    msg = str(excinfo.value)
    assert "Nonexistent" in msg
    assert "Available" in msg
    assert "Work" in msg
    assert "Personal" in msg


def test_get_calendar_returns_first_when_no_name(stub_account, monkeypatch):
    """No name supplied → first calendar is returned. Sanity-pins the
    happy path of the helper through a direct call."""
    cals = [
        _FakeCalendar("First", url="https://dav.example.com/dav/first/"),
        _FakeCalendar("Second", url="https://dav.example.com/dav/second/"),
    ]
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient(cals))
    assert email_mcp._get_calendar(ACCT, name=None).name == "First"


def test_create_event_without_location_or_description_skips_add(cal_with_events):
    """No ``location`` and no ``description`` → those add()s never fire.
    Pins partials 2776->2778 and 2779."""
    cal_with_events.saved_events.clear()
    result = run(
        email_mcp.cal_create_event(
            email_mcp.CalCreateEventInput(
                account_id=ACCT_ID,
                summary="Bare event",
                dtstart="2026-06-01T10:00:00",
                dtend="2026-06-01T10:30:00",
            )
        )
    )
    assert "Event 'Bare event' created" in result
    assert len(cal_with_events.saved_events) == 1
    payload = cal_with_events.saved_events[0]
    assert "SUMMARY:Bare event" in payload
    assert "LOCATION:" not in payload
    assert "DESCRIPTION:" not in payload


def test_update_event_adds_summary_when_missing(stub_account, monkeypatch):
    """An event whose vobject parse has no ``summary`` attribute → the
    `else: vevent.add("summary").value = …` arm fires. Pins line 2820.
    """
    # Craft an iCal blob with UID/DTSTART/DTEND but NO SUMMARY.
    bare_ical = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:no-summary\r\n"
        "DTSTART:20260601T100000Z\r\n"
        "DTEND:20260601T110000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    target = _FakeEvent(bare_ical)
    cal = _FakeCalendar("Work", events=[target])
    monkeypatch.setattr(email_mcp, "_get_calendar", lambda acct, name=None: cal)

    result = run(
        email_mcp.cal_update_event(
            email_mcp.CalUpdateEventInput(
                account_id=ACCT_ID,
                uid="no-summary",
                summary="Now has one",
            )
        )
    )
    assert "updated" in result.lower()
    assert "SUMMARY:Now has one" in target.data
    assert target.saved == 1


# ---------------------------------------------------------------------------
# Final coverage gaps (#8 iter-7).
# ---------------------------------------------------------------------------


def test_get_calendar_matches_named_calendar_case_insensitively(
    stub_account,
    monkeypatch,
):
    """Two calendars; request the second one by name. Returns the matched
    calendar — both exact-case and lowercase requests succeed. Pins line
    2601 (the ``return cal`` inside the match loop)."""
    cals = [
        _FakeCalendar("Work", url="https://dav.example.com/dav/work/"),
        _FakeCalendar("Personal", url="https://dav.example.com/dav/personal/"),
    ]
    monkeypatch.setattr(email_mcp, "_caldav_client", lambda acct: _FakeClient(cals))
    assert email_mcp._get_calendar(ACCT, name="Personal").name == "Personal"
    # Case-insensitive match works too.
    assert email_mcp._get_calendar(ACCT, name="personal").name == "Personal"


def test_create_event_with_location_and_description_writes_both_lines(cal_with_events):
    """Both ``location`` and ``description`` supplied → both add() calls
    fire. Pins line 2779 (``vevent.add("description")…``) explicitly,
    complementing the iter-6 ``without_location_or_description`` test."""
    cal_with_events.saved_events.clear()
    run(
        email_mcp.cal_create_event(
            email_mcp.CalCreateEventInput(
                account_id=ACCT_ID,
                summary="Furnished event",
                dtstart="2026-06-01T10:00:00",
                dtend="2026-06-01T10:30:00",
                location="Room X",
                description="My description",
            )
        )
    )
    payload = cal_with_events.saved_events[0]
    assert "SUMMARY:Furnished event" in payload
    assert "LOCATION:Room X" in payload
    assert "DESCRIPTION:My description" in payload


def test_update_event_overwrites_existing_location_and_description(
    stub_account,
    monkeypatch,
):
    """Event already has LOCATION and DESCRIPTION → the hasattr arms are
    True → ``vevent.location.value`` / ``vevent.description.value``
    overwrite paths fire. Pins lines 2827 and 2832 (the True arms of the
    location and description hasattr checks)."""
    populated = _FakeEvent(
        _ical(
            "ev-pop",
            "Original",
            location="Room A",
            description="Original desc",
        )
    )
    cal = _FakeCalendar("Work", events=[populated])
    monkeypatch.setattr(email_mcp, "_get_calendar", lambda acct, name=None: cal)

    result = run(
        email_mcp.cal_update_event(
            email_mcp.CalUpdateEventInput(
                account_id=ACCT_ID,
                uid="ev-pop",
                location="Room B replacement",
                description="Replacement desc",
            )
        )
    )
    assert "updated" in result.lower()
    assert "Room B replacement" in populated.data
    assert "Replacement desc" in populated.data
    # Originals were overwritten, not appended.
    assert "Room A" not in populated.data
    assert "Original desc" not in populated.data
