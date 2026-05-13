"""Tests for the IMAP/account write-side tool flows that step 4 left out:

- ``email_add_account`` (validation, dedupe, persistence)
- ``email_create_folder`` / ``email_delete_folder``
- ``email_move_message`` (UIDPLUS path + UIDPLUS-absent refuse path)
- ``email_reply`` (threading headers, reply-all addressee filtering)
- ``email_forward`` (quoted-original assembly)

Strategy reuses the patterns from earlier tests: a ``_FakeIMAP`` for the
mailbox side; a ``_FakeSMTP`` to capture the outbound message; ``CONFIG_PATH``
monkeypatched to a tmp file so ``_save_accounts`` / ``_load_accounts`` round
trip through real disk.
"""
from __future__ import annotations

import asyncio
import email
import email.mime.text
import importlib.util
import json
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
    "smtp_host": "smtp.example.com",
    "username": "me@example.com",
    "password": "pw",
}


def _msg_bytes(*, frm="alice@example.com", to="me@example.com",
               cc="", subject="invoice", body="please pay",
               message_id="<orig@example.com>", references=""):
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = "Mon, 13 May 2026 12:00:00 +0000"
    msg["Message-ID"] = message_id
    if references:
        msg["References"] = references
    return msg.as_bytes()


class _FakeIMAP:
    def __init__(self, *, fetch_bodies=None, capabilities=(b"IMAP4REV1", b"UIDPLUS")):
        self.fetch_bodies = fetch_bodies or {}
        self.capabilities = capabilities
        self.selected = None
        self.created = []
        self.deleted = []
        self.copied = []  # [(uid, dest)]
        self.stores = []  # [(uid, flags_arg, value)]
        self.uid_expunges = []  # [uid]
        self.bare_expunged = False
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return ("OK", [b""])

    def create(self, folder):
        self.created.append(folder)
        return ("OK", [b""])

    def delete(self, folder):
        self.deleted.append(folder)
        return ("OK", [b""])

    def uid(self, cmd, *args):
        if cmd == "FETCH":
            uid, _spec = args[0], args[1]
            body = self.fetch_bodies.get(uid)
            if not body:
                return ("NO", [None])
            return ("OK", [(b"hdr", body)])
        if cmd == "COPY":
            self.copied.append((args[0], args[1]))
            return ("OK", [b""])
        if cmd == "STORE":
            self.stores.append(args)
            return ("OK", [b""])
        if cmd == "EXPUNGE":
            self.uid_expunges.append(args[0])
            return ("OK", [b""])
        return ("NO", [None])

    def expunge(self):
        self.bare_expunged = True
        return ("OK", [b""])

    def logout(self):
        self.logged_out = True


class _FakeSMTP:
    def __init__(self):
        self.sendmail_calls = []
        self.quit_called = False

    def sendmail(self, from_addr, recipients, body):
        self.sendmail_calls.append((from_addr, recipients, body))

    def quit(self):
        self.quit_called = True


@pytest.fixture
def stub_account(monkeypatch):
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)


def _install_imap(monkeypatch, imap):
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)


def _install_smtp(monkeypatch, smtp):
    monkeypatch.setattr(email_mcp, "_smtp_connect", lambda acct: smtp)


@pytest.fixture
def fake_smtp(monkeypatch):
    smtp = _FakeSMTP()
    _install_smtp(monkeypatch, smtp)
    # Suppress Sent-folder save (it does its own _imap_connect)
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: _FakeIMAP())
    return smtp


def run(coro):
    return asyncio.run(coro)


def _decoded_body(wire):
    """Return the plain-text body of a built MIME message (handles base64 transport encoding)."""
    msg = email.message_from_string(wire)
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            return payload.decode(part.get_content_charset() or "utf-8")
    return ""


# ---------------------------------------------------------------------------
# email_add_account — write to disk, dedupe by id
# ---------------------------------------------------------------------------

def _input_kwargs(**overrides):
    base = dict(
        id="acct1",
        email_address="me@example.com",
        username="me@example.com",
        password="pw",
        imap_host="imap.example.com",
        smtp_host="smtp.example.com",
    )
    base.update(overrides)
    return base


def test_add_account_persists_to_disk(tmp_path, monkeypatch):
    config = tmp_path / "accounts.json"
    monkeypatch.setattr(email_mcp, "CONFIG_PATH", str(config))

    result = run(email_mcp.email_add_account(
        email_mcp.AddAccountInput(**_input_kwargs(id="first"))
    ))
    assert "added successfully" in result
    data = json.loads(config.read_text())
    ids = [a["id"] for a in data["accounts"]]
    assert ids == ["first"]


def test_add_account_rejects_duplicate_id(tmp_path, monkeypatch):
    config = tmp_path / "accounts.json"
    monkeypatch.setattr(email_mcp, "CONFIG_PATH", str(config))

    run(email_mcp.email_add_account(
        email_mcp.AddAccountInput(**_input_kwargs(id="dup"))
    ))
    result = run(email_mcp.email_add_account(
        email_mcp.AddAccountInput(**_input_kwargs(id="dup", email_address="other@example.com"))
    ))
    assert "already exists" in result
    data = json.loads(config.read_text())
    assert len(data["accounts"]) == 1  # second add did not append


# ---------------------------------------------------------------------------
# email_create_folder / email_delete_folder
# ---------------------------------------------------------------------------

def test_create_folder_forwards_name_to_imap(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)
    result = run(email_mcp.email_create_folder(
        email_mcp.CreateFolderInput(account_id=ACCT_ID, folder="Archive/2026")
    ))
    assert imap.created == ["Archive/2026"]
    assert "created" in result.lower()
    assert imap.logged_out


def test_delete_folder_forwards_name_to_imap(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)
    result = run(email_mcp.email_delete_folder(
        email_mcp.DeleteFolderInput(account_id=ACCT_ID, folder="Old/2020")
    ))
    assert imap.deleted == ["Old/2020"]
    assert "deleted" in result.lower()


# ---------------------------------------------------------------------------
# email_move_message — UIDPLUS path vs refusal
# ---------------------------------------------------------------------------

def test_move_uses_uid_expunge_when_uidplus_supported(stub_account, monkeypatch):
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1", b"UIDPLUS"))
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_move_message(
        email_mcp.MoveEmailInput(
            account_id=ACCT_ID, uid="42",
            source_folder="INBOX", dest_folder="Archive",
        )
    ))
    assert imap.copied == [("42", "Archive")]
    assert imap.uid_expunges == ["42"]
    assert imap.bare_expunged is False  # NEVER bare EXPUNGE
    assert "moved" in result.lower()


def test_move_refuses_and_clears_deleted_when_no_uidplus(stub_account, monkeypatch):
    """Without UIDPLUS, a bare EXPUNGE would wipe every \\Deleted message in
    the source folder. The fix is to refuse and roll back the \\Deleted flag."""
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1",))  # no UIDPLUS
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_move_message(
        email_mcp.MoveEmailInput(
            account_id=ACCT_ID, uid="99",
            source_folder="INBOX", dest_folder="Archive",
        )
    ))
    assert imap.copied == [("99", "Archive")]
    # First +FLAGS \Deleted then -FLAGS \Deleted to roll back
    flag_ops = [s for s in imap.stores if s[0] == "99"]
    assert ("99", "+FLAGS", "(\\Deleted)") in flag_ops
    assert ("99", "-FLAGS", "(\\Deleted)") in flag_ops
    assert imap.uid_expunges == []  # never expunged
    assert imap.bare_expunged is False
    assert "UIDPLUS" in result
    assert "refusing" in result.lower()


# ---------------------------------------------------------------------------
# email_reply
# ---------------------------------------------------------------------------

def test_reply_sets_threading_headers(stub_account, monkeypatch, fake_smtp):
    original = _msg_bytes(message_id="<orig@example.com>", references="<root@example.com>")
    imap = _FakeIMAP(fetch_bodies={"5": original})
    _install_imap(monkeypatch, imap)
    # Reuse the smtp fixture by re-installing (fake_smtp re-points _imap_connect)
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    result = run(email_mcp.email_reply(email_mcp.ReplyEmailInput(
        account_id=ACCT_ID, uid="5", body="thanks", reply_all=False,
    )))
    assert "Reply sent to" in result
    _, recipients, wire = fake_smtp.sendmail_calls[0]
    assert "alice@example.com" in recipients[0]
    assert "In-Reply-To: <orig@example.com>" in wire
    # References chains old refs + the message-id
    assert "<root@example.com>" in wire
    assert "<orig@example.com>" in wire
    assert "Subject: Re: invoice" in wire


def test_reply_all_includes_orig_cc_minus_self(stub_account, monkeypatch, fake_smtp):
    original = _msg_bytes(
        frm="alice@example.com",
        to="me@example.com, bob@example.com",
        cc="carol@example.com, me@example.com",
    )
    imap = _FakeIMAP(fetch_bodies={"5": original})
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    run(email_mcp.email_reply(email_mcp.ReplyEmailInput(
        account_id=ACCT_ID, uid="5", body="...", reply_all=True,
    )))

    _, recipients, _ = fake_smtp.sendmail_calls[0]
    # Primary recipient = Reply-To/From (alice)
    assert "alice@example.com" in recipients[0]
    # CC includes everyone EXCEPT self
    cc_addrs = " ".join(recipients[1:])
    assert "bob@example.com" in cc_addrs
    assert "carol@example.com" in cc_addrs
    assert "me@example.com" not in cc_addrs


def test_reply_prefixes_subject_only_when_needed(stub_account, monkeypatch, fake_smtp):
    """If the subject already starts with 'Re:', don't add another."""
    original = _msg_bytes(subject="Re: thread")
    imap = _FakeIMAP(fetch_bodies={"1": original})
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    run(email_mcp.email_reply(email_mcp.ReplyEmailInput(
        account_id=ACCT_ID, uid="1", body="ack", reply_all=False,
    )))
    _, _, wire = fake_smtp.sendmail_calls[0]
    # Exactly one "Re:" — not "Re: Re: thread"
    assert "Subject: Re: thread" in wire
    assert "Re: Re:" not in wire


# ---------------------------------------------------------------------------
# email_forward
# ---------------------------------------------------------------------------

def test_forward_includes_quoted_original_and_fwd_subject(stub_account, monkeypatch, fake_smtp):
    original = _msg_bytes(
        frm="alice@example.com", subject="status", body="all green",
    )
    imap = _FakeIMAP(fetch_bodies={"7": original})
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    result = run(email_mcp.email_forward(email_mcp.ForwardEmailInput(
        account_id=ACCT_ID, uid="7", to="team@example.com",
        body="fyi",
    )))
    _, recipients, wire = fake_smtp.sendmail_calls[0]
    assert "team@example.com" in recipients
    body = _decoded_body(wire)
    # Note prepended, then quoted block, then original body
    assert "fyi" in body
    assert "Forwarded message" in body
    assert "From: alice@example.com" in body
    assert "all green" in body
    # Subject gets the Fwd: prefix once (header, plain on the wire)
    assert "Subject: Fwd: status" in wire
    assert "Forwarded" in result


def test_forward_does_not_double_fwd_prefix(stub_account, monkeypatch, fake_smtp):
    original = _msg_bytes(subject="Fwd: status")
    imap = _FakeIMAP(fetch_bodies={"1": original})
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: imap)

    run(email_mcp.email_forward(email_mcp.ForwardEmailInput(
        account_id=ACCT_ID, uid="1", to="team@example.com",
    )))
    _, _, wire = fake_smtp.sendmail_calls[0]
    assert "Subject: Fwd: status" in wire
    assert "Fwd: Fwd:" not in wire
