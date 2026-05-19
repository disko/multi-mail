"""Tests for the IMAP-backed tool flows: ``email_list_folders``,
``email_search_messages``, and ``email_read_message``.

A ``_FakeIMAP`` records ``select``/``uid``/``list`` calls and returns canned
responses so we can verify:

- the right query/UID/folder gets forwarded,
- markdown output is shaped correctly,
- ``conn.logout()`` is always called (no socket leak),
- error statuses surface as user-visible "Error: ..." strings.
"""
from __future__ import annotations

import asyncio
import email.message
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
    "display_name": "Work",
    "email_address": "me@example.com",
    "imap_host": "imap.example.com",
    "username": "me@example.com",
    "password": "pw",
}


def _msg_bytes(*, frm="alice@example.com", subject="Hi", date="Mon, 13 May 2026 12:00:00 +0000", body="hello"):
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["From"] = frm
    msg["To"] = "me@example.com"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = "<abc@example.com>"
    return msg.as_bytes()


class _FakeIMAP:
    def __init__(self, *, list_resp=None, search_uids=None, fetch_bodies=None):
        # list_resp: list[bytes] returned by .list()
        self.list_resp = list_resp or []
        # search_uids: bytes string like b"1 2 3"
        self.search_uids = search_uids if search_uids is not None else b""
        # fetch_bodies: {uid_str: bytes} mapping
        self.fetch_bodies = fetch_bodies or {}

        self.selected = None
        self.select_readonly = None
        self.search_calls = []
        self.fetch_calls = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected = folder
        self.select_readonly = readonly
        return ("OK", [b""])

    def list(self, directory='""', pattern="*"):
        return ("OK", self.list_resp)

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            # signature: SEARCH None <query>
            self.search_calls.append(args)
            return ("OK", [self.search_uids])
        if cmd == "FETCH":
            uid, _fetch_spec = args[0], args[1]
            self.fetch_calls.append(args)
            body = self.fetch_bodies.get(uid)
            if not body:
                return ("NO", [None])
            return ("OK", [(b"" + uid.encode() + b" (RFC822 {len})", body)])
        return ("NO", [None])

    def logout(self):
        self.logged_out = True


@pytest.fixture
def stub_account(monkeypatch):
    """Make _get_account return ACCT regardless of CONFIG_PATH state."""
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)


def _install_imap(monkeypatch, fake):
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: fake)


def run(coro):
    """Tiny helper — every tool returns a coroutine."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# email_list_folders
# ---------------------------------------------------------------------------

def test_list_folders_returns_sorted_markdown(stub_account, monkeypatch):
    fake = _FakeIMAP(list_resp=[
        b'(\\HasNoChildren) "/" "Sent"',
        b'(\\HasNoChildren) "/" "Drafts"',
        b'(\\HasNoChildren) "/" "INBOX"',
    ])
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    # Sorted alphabetically
    drafts_idx = result.index("- Drafts")
    inbox_idx = result.index("- INBOX")
    sent_idx = result.index("- Sent")
    assert drafts_idx < inbox_idx < sent_idx
    assert fake.logged_out is True


def test_list_folders_parses_atom_form_names(stub_account, monkeypatch):
    """Mailbox names returned as unquoted atoms (RFC 3501) must round-trip.

    The reporter's Dovecot/mailcow server emits LIST responses without quoting
    the mailbox name; the old ``rsplit('"', 2)`` parser then extracts the
    delimiter ``/`` instead of the folder name.
    """
    fake = _FakeIMAP(list_resp=[
        b'(\\HasNoChildren) "/" INBOX',
        b'(\\HasNoChildren) "/" Sent',
    ])
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    assert "- INBOX" in result
    assert "- Sent" in result
    # The delimiter must NOT leak through as a folder name.
    assert "- /" not in result
    # Sorted alphabetically.
    assert result.index("- INBOX") < result.index("- Sent")
    assert fake.logged_out is True


def test_list_folders_parses_literal_form_tuple(stub_account, monkeypatch):
    """imaplib returns literal-form mailbox names as a ``(header, name)`` tuple.

    A mixed list (one tuple + one bytes entry) exercises both branches of the
    parser in a single response.
    """
    fake = _FakeIMAP(list_resp=[
        (b'(\\HasNoChildren) "/" {6}', b'Drafts'),
        b'(\\HasNoChildren) "/" "INBOX"',
    ])
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    assert "- Drafts" in result
    assert "- INBOX" in result
    # Exactly two folder lines — nothing dropped, nothing extra.
    folder_lines = [ln for ln in result.splitlines() if ln.startswith("- ")]
    assert len(folder_lines) == 2


def test_list_folders_parses_quoted_name_with_space(stub_account, monkeypatch):
    """A quoted name containing whitespace must survive intact."""
    fake = _FakeIMAP(list_resp=[
        b'(\\HasNoChildren) "/" "My Folder"',
        b'(\\HasNoChildren) "/" "INBOX"',
    ])
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    assert "- My Folder" in result
    assert "- INBOX" in result


def test_list_folders_heading_falls_back_when_display_name_is_none(monkeypatch):
    """``acct.get('display_name', acct_id)`` returns ``None`` when the field
    is serialized as ``null`` — the heading must fall back to ``account_id``.
    """
    acct_with_null_name = dict(ACCT, display_name=None)
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: acct_with_null_name)
    fake = _FakeIMAP(list_resp=[b'(\\HasNoChildren) "/" "INBOX"'])
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    assert f"# Folders for {ACCT_ID}" in result
    assert "for None" not in result


def test_list_folders_surfaces_imap_error(stub_account, monkeypatch):
    fake = _FakeIMAP()

    def _broken_list(directory='""', pattern="*"):
        return ("NO", [b"server explosion"])
    fake.list = _broken_list
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_list_folders(email_mcp.ListFoldersInput(account_id=ACCT_ID)))
    assert "Error: IMAP LIST failed" in result
    assert fake.logged_out is True


# ---------------------------------------------------------------------------
# email_search_messages
# ---------------------------------------------------------------------------

def test_search_forwards_query_verbatim_to_imap(stub_account, monkeypatch):
    fake = _FakeIMAP(search_uids=b"")
    _install_imap(monkeypatch, fake)

    run(email_mcp.email_search_messages(email_mcp.SearchEmailsInput(
        account_id=ACCT_ID,
        query='FROM "alice@example.com"',
        folder="INBOX",
    )))
    # one SEARCH call, with (None, 'FROM "alice..."')
    assert len(fake.search_calls) == 1
    args = fake.search_calls[0]
    assert args[0] is None  # charset
    assert args[1] == 'FROM "alice@example.com"'
    assert fake.selected == "INBOX"
    assert fake.select_readonly is True  # search must not flip \Seen


def test_search_empty_result_shows_friendly_message(stub_account, monkeypatch):
    _install_imap(monkeypatch, _FakeIMAP(search_uids=b""))
    result = run(email_mcp.email_search_messages(email_mcp.SearchEmailsInput(
        account_id=ACCT_ID, query="UNSEEN",
    )))
    assert "No messages matching: UNSEEN" in result


def test_search_renders_markdown_table_for_hits(stub_account, monkeypatch):
    fake = _FakeIMAP(
        search_uids=b"1 2",
        fetch_bodies={
            "1": _msg_bytes(frm="alice@example.com", subject="invoice 42"),
            "2": _msg_bytes(frm="bob@example.com", subject="re: invoice 42"),
        },
    )
    _install_imap(monkeypatch, fake)
    result = run(email_mcp.email_search_messages(email_mcp.SearchEmailsInput(
        account_id=ACCT_ID, query='SUBJECT "invoice"',
    )))
    assert "| UID | From | Subject | Date |" in result
    assert "alice@example.com" in result
    assert "bob@example.com" in result


def test_search_results_are_newest_first_and_limited(stub_account, monkeypatch):
    # IMAP returns UIDs ascending; the tool must reverse for "newest first"
    # and clamp to params.limit.
    fake = _FakeIMAP(
        search_uids=b"1 2 3 4 5",
        fetch_bodies={str(i): _msg_bytes(subject=f"msg{i}") for i in range(1, 6)},
    )
    _install_imap(monkeypatch, fake)
    result = run(email_mcp.email_search_messages(email_mcp.SearchEmailsInput(
        account_id=ACCT_ID, query="ALL", limit=2,
    )))
    # UIDs 5 and 4 fetched (newest first, limit=2). UIDs 1,2,3 NOT fetched.
    fetched_uids = [c[0] for c in fake.fetch_calls]
    assert fetched_uids == ["5", "4"]


# ---------------------------------------------------------------------------
# email_read_message
# ---------------------------------------------------------------------------

def test_read_message_uses_peek_when_not_marking_read(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={"7": _msg_bytes(body="full body here")})
    _install_imap(monkeypatch, fake)

    run(email_mcp.email_read_message(email_mcp.ReadEmailInput(
        account_id=ACCT_ID, uid="7", folder="INBOX", mark_read=False,
    )))
    # BODY.PEEK[] avoids setting \Seen; RFC822 would set it
    assert fake.fetch_calls[0][1] == "(BODY.PEEK[])"
    assert fake.select_readonly is True


def test_read_message_uses_rfc822_and_writable_select_when_marking_read(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={"7": _msg_bytes()})
    _install_imap(monkeypatch, fake)

    run(email_mcp.email_read_message(email_mcp.ReadEmailInput(
        account_id=ACCT_ID, uid="7", folder="INBOX", mark_read=True,
    )))
    assert fake.fetch_calls[0][1] == "(RFC822)"
    assert fake.select_readonly is False


def test_read_message_includes_headers_and_body(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={"42": _msg_bytes(
        frm="alice@example.com", subject="hi there", body="message contents",
    )})
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_read_message(email_mcp.ReadEmailInput(
        account_id=ACCT_ID, uid="42",
    )))
    assert "**From**: alice@example.com" in result
    assert "**Subject**: hi there" in result
    assert "message contents" in result


def test_read_message_missing_uid_returns_error(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={})  # nothing on the server
    _install_imap(monkeypatch, fake)

    result = run(email_mcp.email_read_message(email_mcp.ReadEmailInput(
        account_id=ACCT_ID, uid="999",
    )))
    assert "Error: Could not fetch message UID 999" in result
    assert fake.logged_out is True
