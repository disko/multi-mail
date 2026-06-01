"""Tests for ``_build_message`` and ``_send_message`` — outbound message
assembly and SMTP/IMAP fan-out.

``_build_message`` is pure and tested directly. ``_send_message`` is exercised
with mocked _smtp_connect / _imap_connect so we cover the recipient-list
construction and the auto-save-to-Sent fallback without touching the network.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)

_build_message = email_mcp._build_message
_send_message = email_mcp._send_message


ACCT = {
    "id": "work",
    "email_address": "me@example.com",
    "imap_host": "imap.example.com",
    "smtp_host": "smtp.example.com",
    "username": "me@example.com",
    "password": "pw",
}


# ---------------------------------------------------------------------------
# _build_message
# ---------------------------------------------------------------------------


def test_build_message_minimal_headers():
    msg = _build_message(ACCT, "alice@example.com", "Hi", "hello")
    assert msg["From"] == "me@example.com"
    assert msg["To"] == "alice@example.com"
    assert msg["Subject"] == "Hi"
    assert msg["Date"]  # auto-populated, format varies
    assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith(">")


def test_build_message_body_is_plain_utf8():
    msg = _build_message(ACCT, "alice@example.com", "S", "Grüße")
    parts = list(msg.walk())
    text_parts = [p for p in parts if p.get_content_type() == "text/plain"]
    assert len(text_parts) == 1
    body = text_parts[0].get_payload(decode=True).decode("utf-8")
    assert body == "Grüße"


def test_build_message_omits_optional_headers_when_none():
    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    assert msg.get("Cc") is None
    assert msg.get("In-Reply-To") is None
    assert msg.get("References") is None


def test_build_message_bcc_is_not_in_headers():
    """BCC must NOT appear in built MIME — that defeats the purpose. It's
    handed separately to _send_message instead."""
    msg = _build_message(ACCT, "a@example.com", "S", "body")
    assert msg.get("Bcc") is None


def test_build_message_threading_headers():
    msg = _build_message(
        ACCT,
        "alice@example.com",
        "Re: hi",
        "yes",
        in_reply_to="<orig@example.com>",
        references="<thread1@example.com> <thread2@example.com>",
    )
    assert msg["In-Reply-To"] == "<orig@example.com>"
    assert msg["References"] == "<thread1@example.com> <thread2@example.com>"


def test_build_message_cc_added_when_provided():
    msg = _build_message(ACCT, "a@example.com", "S", "body", cc="cc@example.com")
    assert msg["Cc"] == "cc@example.com"


def test_build_message_unique_message_id_per_call():
    a = _build_message(ACCT, "x@example.com", "S", "b")
    b = _build_message(ACCT, "x@example.com", "S", "b")
    assert a["Message-ID"] != b["Message-ID"]


# ---------------------------------------------------------------------------
# _send_message — recipient list + SMTP/IMAP fan-out (mocked)
# ---------------------------------------------------------------------------


class _FakeSMTP:
    def __init__(self):
        self.sendmail_calls = []
        self.quit_called = False

    def sendmail(self, from_addr, recipients, body):
        self.sendmail_calls.append((from_addr, recipients, body))

    def quit(self):
        self.quit_called = True


class _FakeIMAP:
    def __init__(self, select_ok_for="Sent"):
        self.select_ok_for = select_ok_for
        self.appends = []
        self.logged_out = False

    def select(self, folder):
        if folder == self.select_ok_for:
            return ("OK", [b""])
        return ("NO", [b"not found"])

    def append(self, folder, flags, when, body):
        self.appends.append((folder, flags, body))

    def logout(self):
        self.logged_out = True


@pytest.fixture
def fakes(monkeypatch):
    """Swap _smtp_connect and _imap_connect for in-memory fakes."""
    smtp = _FakeSMTP()
    imap = _FakeIMAP()
    monkeypatch.setattr(email_mcp, "_smtp_connect", lambda acct: smtp)
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)
    return smtp, imap


def test_send_message_collects_to_recipients(fakes):
    smtp, _ = fakes
    msg = _build_message(ACCT, "alice@example.com, bob@example.com", "S", "body")
    _send_message(ACCT, msg)
    from_addr, recipients, _ = smtp.sendmail_calls[0]
    assert from_addr == "me@example.com"
    assert recipients == ["alice@example.com", "bob@example.com"]


def test_send_message_includes_cc_in_recipients(fakes):
    smtp, _ = fakes
    msg = _build_message(
        ACCT, "alice@example.com", "S", "body", cc="cc1@example.com, cc2@example.com"
    )
    _send_message(ACCT, msg)
    _, recipients, _ = smtp.sendmail_calls[0]
    assert recipients == ["alice@example.com", "cc1@example.com", "cc2@example.com"]


def test_send_message_includes_bcc_in_envelope_only(fakes):
    """BCC recipients must be in the SMTP RCPT TO list but NOT in the message body headers."""
    smtp, _ = fakes
    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    _send_message(ACCT, msg, bcc="bcc@example.com")
    _, recipients, body = smtp.sendmail_calls[0]
    assert "bcc@example.com" in recipients
    assert "Bcc:" not in body  # never leaks into the on-wire message body


def test_send_message_quits_smtp_even_on_success(fakes):
    smtp, _ = fakes
    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    _send_message(ACCT, msg)
    assert smtp.quit_called is True


def test_send_message_appends_to_sent_folder(fakes):
    """When IMAP "Sent" exists, the outbound msg gets appended there."""
    _, imap = fakes
    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    _send_message(ACCT, msg)
    assert len(imap.appends) == 1
    folder, flags, body = imap.appends[0]
    assert folder == "Sent"
    assert flags == "\\Seen"
    assert b"Subject: S" in body


def test_send_message_tries_alternate_sent_folder_names(monkeypatch):
    """If "Sent" doesn't exist, try INBOX.Sent / Sent Items / Sent Messages."""
    smtp = _FakeSMTP()
    imap = _FakeIMAP(select_ok_for="INBOX.Sent")
    monkeypatch.setattr(email_mcp, "_smtp_connect", lambda acct: smtp)
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    _send_message(ACCT, msg)

    assert len(imap.appends) == 1
    assert imap.appends[0][0] == "INBOX.Sent"


def test_send_message_swallows_imap_failure(monkeypatch):
    """A broken Sent-folder save must NOT fail the send — the mail already left."""
    smtp = _FakeSMTP()

    def _imap_broken(acct):
        raise ConnectionError("IMAP server gone")

    monkeypatch.setattr(email_mcp, "_smtp_connect", lambda acct: smtp)
    monkeypatch.setattr(email_mcp, "_imap_connect", _imap_broken)

    msg = _build_message(ACCT, "alice@example.com", "S", "body")
    _send_message(ACCT, msg)  # should not raise
    assert len(smtp.sendmail_calls) == 1
