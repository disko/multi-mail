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
    def __init__(self, *, fetch_bodies=None, capabilities=(b"IMAP4REV1", b"UIDPLUS"),
                 list_resp=None):
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
        # RFC 6154 SPECIAL-USE LIST response; default advertises \Trash on "Trash".
        self.list_resp = list_resp if list_resp is not None else [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren \\Trash) "/" "Trash"',
            b'(\\HasNoChildren \\Sent) "/" "Sent"',
        ]

    def select(self, folder, readonly=False):
        self.selected = (folder, readonly)
        return ("OK", [b""])

    def list(self, directory='""', pattern='"*"'):
        return ("OK", list(self.list_resp))

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


# ---------------------------------------------------------------------------
# email_modify_flags — issue #3
#
# New tool: maps to UID STORE +FLAGS / -FLAGS. The tests below pin the
# wire-format contract: a single IMAP session selects the folder, issues
# adds first then removes (each as one parenthesised flag-list), and always
# logs out. No expunge, no FETCH read-back, no UIDPLUS branch. The
# `_FakeIMAP.stores` recorder above captures the (uid, flags_arg, value)
# tuple shape that the new tool must produce.
# ---------------------------------------------------------------------------

def test_modify_flags_add_only_issues_plus_store(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="42", add_flags=["\\Flagged"],
        )
    ))

    assert imap.selected == ("INBOX", False)
    assert imap.stores == [("42", "+FLAGS", "(\\Flagged)")]
    assert "42" in result
    assert imap.logged_out is True


def test_modify_flags_remove_only_issues_minus_store(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="42", remove_flags=["\\Flagged"],
        )
    ))

    assert imap.selected == ("INBOX", False)
    assert imap.stores == [("42", "-FLAGS", "(\\Flagged)")]
    assert "42" in result
    assert imap.logged_out is True


def test_modify_flags_add_and_remove_in_one_call(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="7",
            add_flags=["\\Answered"],
            remove_flags=["\\Flagged", "\\Seen"],
        )
    ))

    # Adds must precede removes — contract pinned here so callers
    # (and the implementer) can rely on a deterministic order.
    assert imap.stores == [
        ("7", "+FLAGS", "(\\Answered)"),
        ("7", "-FLAGS", "(\\Flagged \\Seen)"),
    ]


def test_modify_flags_idempotent_reapply(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    r1 = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="42", add_flags=["\\Flagged"],
        )
    ))
    r2 = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="42", add_flags=["\\Flagged"],
        )
    ))

    # Both calls reach STORE with no pre-fetch / current-flags lookup.
    assert imap.stores == [
        ("42", "+FLAGS", "(\\Flagged)"),
        ("42", "+FLAGS", "(\\Flagged)"),
    ]
    assert r1 == r2  # same input → same output


def test_modify_flags_respects_folder_argument(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="3",
            folder="Archive", add_flags=["\\Flagged"],
        )
    ))

    assert imap.selected == ("Archive", False)


def test_modify_flags_custom_keyword_passes_through_unprefixed(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="9", add_flags=["follow-up"],
        )
    ))

    assert imap.stores == [("9", "+FLAGS", "(follow-up)")]


def test_modify_flags_rejects_empty_payload(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    # Both lists empty (Pydantic defaults). The model_validator or the
    # _impl runtime check must reject this before issuing STORE.
    from pydantic import ValidationError

    try:
        result = run(email_mcp.email_modify_flags(
            email_mcp.ModifyFlagsInput(account_id=ACCT_ID, uid="1")
        ))
    except ValidationError as e:
        # Pydantic ValidationError path — empty payload rejected at
        # model construction time.
        msg = str(e).lower()
        assert "at least one" in msg or "nothing to do" in msg or "non-empty" in msg
    else:
        # Runtime-check path — tool returns an error string.
        low = result.lower()
        assert "at least one" in low or "nothing to do" in low or (
            "error" in low and "flag" in low
        )

    assert imap.stores == []


def test_modify_flags_rejects_invalid_flag_atom(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    # Whitespace inside a flag atom is illegal per RFC 3501 atom rules.
    # The implementer can enforce this via Pydantic ValidationError
    # (raised at construction) or via a runtime check in _impl that
    # returns an error string. Either way: no STORE call, no crash.
    from pydantic import ValidationError

    def _attempt():
        return run(email_mcp.email_modify_flags(
            email_mcp.ModifyFlagsInput(
                account_id=ACCT_ID, uid="1",
                add_flags=["bad flag with spaces"],
            )
        ))

    try:
        result = _attempt()
    except ValidationError as e:
        msg = str(e).lower()
        assert "invalid" in msg or "whitespace" in msg or "atom" in msg
    else:
        low = result.lower()
        assert "invalid" in low or "error" in low

    assert imap.stores == []


def test_modify_flags_handles_imap_store_failure(stub_account, monkeypatch):
    class _DenyingIMAP(_FakeIMAP):
        def uid(self, cmd, *args):
            if cmd == "STORE":
                # Record so the test can confirm the call was attempted.
                self.stores.append(args)
                return ("NO", [b"PERMISSION DENIED"])
            return super().uid(cmd, *args)

    imap = _DenyingIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="1", add_flags=["\\Flagged"],
        )
    ))

    assert result.lower().startswith("error")
    assert "PERMISSION DENIED" in result or "permission denied" in result.lower()
    assert imap.logged_out is True


def test_modify_flags_unknown_account_returns_error(monkeypatch):
    def _missing(aid):
        raise ValueError(f"Account '{aid}' not found. Use email_list_accounts.")
    monkeypatch.setattr(email_mcp, "_get_account", _missing)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id="nope", uid="1", add_flags=["\\Flagged"],
        )
    ))

    assert result.lower().startswith("error")
    assert "not found" in result.lower()


def test_modify_flags_remove_store_failure_returns_error_after_add_succeeded(
    stub_account, monkeypatch
):
    # Add STORE succeeds, remove STORE fails — second branch (line 2018).
    class _RemoveFailingIMAP(_FakeIMAP):
        def uid(self, cmd, *args):
            self.stores.append(args)
            if cmd == "STORE" and args[1] == "-FLAGS":
                return ("NO", [b"QUOTA"])
            return ("OK", [b""])

    imap = _RemoveFailingIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="9",
            add_flags=["\\Seen"], remove_flags=["\\Flagged"],
        )
    ))

    assert result.lower().startswith("error updating flags")
    assert "QUOTA" in result
    # Both STOREs were attempted; remove was the failing one.
    assert any(a[1] == "+FLAGS" for a in imap.stores)
    assert any(a[1] == "-FLAGS" for a in imap.stores)
    assert imap.logged_out is True


def test_modify_flags_swallows_logout_exception(stub_account, monkeypatch):
    # Server crashes mid-logout after a successful STORE — must not surface
    # the logout error to the caller (lines 2026-2027).
    class _LogoutBoomIMAP(_FakeIMAP):
        def logout(self):
            self.logged_out = True
            raise ConnectionResetError("server hung up during LOGOUT")

    imap = _LogoutBoomIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_modify_flags(
        email_mcp.ModifyFlagsInput(
            account_id=ACCT_ID, uid="1", add_flags=["\\Seen"],
        )
    ))

    assert "Flags updated" in result
    assert "hung up" not in result.lower()
    assert imap.logged_out is True


# ---------------------------------------------------------------------------
# _validate_flag_atom unit tests — direct invocation covers defensive branches
# that pydantic's List[str] type would otherwise prevent reaching.
# ---------------------------------------------------------------------------

def test_validate_flag_atom_rejects_non_string():
    # Defensive isinstance check (line 568) is unreachable through the
    # pydantic model (typed List[str]), but the helper is also called
    # internally; test the contract directly.
    with pytest.raises(ValueError, match="not a string"):
        email_mcp._validate_flag_atom(42)  # type: ignore[arg-type]


def test_validate_flag_atom_rejects_empty_string():
    with pytest.raises(ValueError, match="empty string"):
        email_mcp._validate_flag_atom("")


def test_validate_flag_atom_rejects_bare_backslash():
    with pytest.raises(ValueError, match="bare backslash"):
        email_mcp._validate_flag_atom("\\")


# ---------------------------------------------------------------------------
# email_delete_message + email_expunge — issue #5
#
# Split design: ``email_delete_message`` handles the common workflow (move to
# Trash by default, permanent with ``permanent=True``); ``email_expunge`` is
# the standalone primitive for power users who already marked ``\\Deleted``
# via ``email_modify_flags``. Both share the UIDPLUS-or-refuse guard from
# ``email_move_message``. A new helper ``_resolve_trash_folder`` walks a
# four-step fallback chain: explicit param → per-account config →
# ``\\Trash`` SPECIAL-USE flag → hard-coded "Trash".
#
# All tests below SHOULD FAIL on first run with AttributeError because the
# symbols don't exist yet. Per RUNBOOK §7 narrow-except: the pydantic
# validation test uses ``with pytest.raises(ValidationError)``, never bare
# ``except Exception``, so a missing-symbol AttributeError can't masquerade
# as a passing validation.
# ---------------------------------------------------------------------------


# ----------------------------- Group A: delete-to-trash path -----------------

def test_delete_message_default_moves_to_trash_via_special_use(stub_account, monkeypatch):
    """Default ``permanent=False`` resolves Trash via SPECIAL-USE LIST, COPY+
    STORE+UID EXPUNGE on the source folder. UIDPLUS available."""
    imap = _FakeIMAP()  # default list_resp advertises \Trash on "Trash"
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert imap.copied == [("42", "Trash")]
    assert ("42", "+FLAGS", "(\\Deleted)") in imap.stores
    assert imap.uid_expunges == ["42"]
    assert imap.bare_expunged is False
    assert "Trash" in result
    assert "42" in result
    assert imap.logged_out is True


def test_delete_message_uses_per_account_trash_folder_override(monkeypatch):
    """Per-account ``trash_folder`` config wins over SPECIAL-USE detection."""
    acct = {**ACCT, "trash_folder": "Papierkorb"}
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: acct)
    # list_resp lacks any \Trash flag, so the account config must win.
    imap = _FakeIMAP(list_resp=[
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\Sent) "/" "Sent"',
    ])
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert imap.copied == [("42", "Papierkorb")]
    assert "Papierkorb" in result


def test_delete_message_falls_back_to_hardcoded_trash_when_no_special_use_or_config(
    stub_account, monkeypatch
):
    """No override, no per-account config, no SPECIAL-USE → 'Trash' literal."""
    imap = _FakeIMAP(list_resp=[b'(\\HasNoChildren) "/" "INBOX"'])
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert imap.copied == [("42", "Trash")]


def test_delete_message_explicit_trash_folder_param_wins_over_special_use(
    stub_account, monkeypatch
):
    """An explicit ``trash_folder`` parameter is the highest-priority override."""
    imap = _FakeIMAP()  # advertises \Trash on "Trash"
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
            trash_folder="Junk",
        )
    ))

    assert imap.copied == [("42", "Junk")]


# ----------------------------- Group B: permanent path -----------------------

def test_delete_message_permanent_skips_copy_and_uses_uid_expunge(
    stub_account, monkeypatch
):
    """``permanent=True`` does not COPY; STORE \\Deleted + UID EXPUNGE only."""
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1", b"UIDPLUS"))
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX", permanent=True,
        )
    ))

    assert imap.copied == []
    assert ("42", "+FLAGS", "(\\Deleted)") in imap.stores
    assert imap.uid_expunges == ["42"]
    assert imap.bare_expunged is False
    assert "42" in result
    low = result.lower()
    assert "permanent" in low or "permanently" in low


def test_delete_message_permanent_refuses_and_clears_deleted_without_uidplus(
    stub_account, monkeypatch
):
    """Without UIDPLUS, refuse and roll back the \\Deleted flag."""
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1",))  # no UIDPLUS
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX", permanent=True,
        )
    ))

    assert imap.uid_expunges == []
    assert imap.bare_expunged is False
    flag_ops = [s for s in imap.stores if s[0] == "42"]
    assert ("42", "+FLAGS", "(\\Deleted)") in flag_ops
    assert ("42", "-FLAGS", "(\\Deleted)") in flag_ops
    assert "UIDPLUS" in result
    assert "refusing" in result.lower()


# ----------------------------- Group C: error / validation -------------------

def test_delete_message_uid_not_found_returns_error(stub_account, monkeypatch):
    """If the COPY-to-Trash fails with a non-TRYCREATE error, surface it
    and don't expunge."""
    class _CopyFailIMAP(_FakeIMAP):
        def uid(self, cmd, *args):
            if cmd == "COPY":
                self.copied.append((args[0], args[1]))
                return ("NO", [b"UID not found"])
            return super().uid(cmd, *args)

    imap = _CopyFailIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert result.lower().startswith("error")
    assert "UID not found" in result or "uid not found" in result.lower()
    assert imap.uid_expunges == []


def test_delete_message_unknown_account_returns_error(monkeypatch):
    """Missing account_id surfaces a clean error, no IMAP contact."""
    def _missing(aid):
        raise ValueError(f"Account '{aid}' not found. Use email_list_accounts.")
    monkeypatch.setattr(email_mcp, "_get_account", _missing)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(account_id="nope", uid="1", folder="INBOX")
    ))

    assert result.lower().startswith("error")
    assert "not found" in result.lower()


def test_delete_message_rejects_empty_uid(monkeypatch):
    """Empty ``uid`` is rejected at pydantic construction time.

    Per RUNBOOK §7: narrow except. Catch ``ValidationError`` specifically so
    an ``AttributeError`` from a missing ``DeleteEmailInput`` cannot pass as
    a successful validation rejection.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        email_mcp.DeleteEmailInput(account_id=ACCT_ID, uid="")


def test_delete_message_auto_creates_trash_when_missing(stub_account, monkeypatch):
    """On TRYCREATE, the tool creates the trash folder and retries COPY."""
    class _TryCreateIMAP(_FakeIMAP):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._copy_attempts = 0

        def uid(self, cmd, *args):
            if cmd == "COPY":
                self._copy_attempts += 1
                if self._copy_attempts == 1:
                    # First COPY: server says target doesn't exist.
                    return ("NO", [b"[TRYCREATE] Mailbox doesn't exist"])
                # Second COPY (after create): succeed.
                self.copied.append((args[0], args[1]))
                return ("OK", [b""])
            return super().uid(cmd, *args)

    imap = _TryCreateIMAP()
    _install_imap(monkeypatch, imap)

    run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert "Trash" in imap.created
    assert imap.copied == [("42", "Trash")]
    assert imap.uid_expunges == ["42"]


# ----------------------------- Group D: email_expunge ------------------------

def test_expunge_with_uid_uses_uid_expunge_when_uidplus_supported(
    stub_account, monkeypatch
):
    """Scoped expunge: UID-only, no STORE, UIDPLUS-required."""
    imap = _FakeIMAP()  # UIDPLUS present
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert imap.selected == ("INBOX", False)
    assert imap.uid_expunges == ["42"]
    assert imap.bare_expunged is False
    assert imap.stores == []
    assert "42" in result
    assert "INBOX" in result


def test_expunge_without_uid_refuses_unless_confirm_bare_expunge_true(
    stub_account, monkeypatch
):
    """No uid + no confirm flag → refuse with a clear message."""
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(account_id=ACCT_ID, folder="INBOX")
    ))

    assert result.lower().startswith("error")
    low = result.lower()
    assert "bare" in low or "confirm" in low
    assert imap.bare_expunged is False
    assert imap.uid_expunges == []


def test_expunge_bare_with_confirm_flag_calls_expunge(stub_account, monkeypatch):
    """``confirm_bare_expunge=True`` + no uid → bare EXPUNGE issued."""
    imap = _FakeIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(
            account_id=ACCT_ID, folder="Trash", confirm_bare_expunge=True,
        )
    ))

    assert imap.bare_expunged is True
    assert "Trash" in result


def test_expunge_with_uid_refuses_when_no_uidplus(stub_account, monkeypatch):
    """uid supplied but server lacks UIDPLUS → refuse, do not bare-expunge."""
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1",))
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert "UIDPLUS" in result
    assert "refusing" in result.lower()
    assert imap.uid_expunges == []
    assert imap.bare_expunged is False


def test_expunge_non_ok_response_returns_error(stub_account, monkeypatch):
    """Server returns NO on UID EXPUNGE → tool surfaces the error string."""
    class _PermDeniedIMAP(_FakeIMAP):
        def uid(self, cmd, *args):
            if cmd == "EXPUNGE":
                return ("NO", [b"PERMISSION DENIED"])
            return super().uid(cmd, *args)

    imap = _PermDeniedIMAP()
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(
            account_id=ACCT_ID, uid="42", folder="INBOX",
        )
    ))

    assert result.lower().startswith("error")
    assert imap.logged_out is True


# ----------------------------- Group E: _resolve_trash_folder helper --------

def test_resolve_trash_param_override_wins():
    """``override`` argument trumps every other source."""
    conn = _FakeIMAP()  # advertises \Trash on "Trash"
    assert email_mcp._resolve_trash_folder(
        conn, {"trash_folder": "X"}, override="Y"
    ) == "Y"


def test_resolve_trash_account_config_beats_special_use():
    """Per-account config beats SPECIAL-USE detection."""
    conn = _FakeIMAP()  # advertises \Trash on "Trash"
    assert email_mcp._resolve_trash_folder(
        conn, {"trash_folder": "Papierkorb"}
    ) == "Papierkorb"


def test_resolve_trash_account_config_null_falls_through():
    """Gotcha #1: explicit ``trash_folder: None`` must fall through to
    SPECIAL-USE detection, not return ``None``."""
    conn = _FakeIMAP()  # advertises \Trash on "Trash"
    result = email_mcp._resolve_trash_folder(conn, {"trash_folder": None})
    assert result == "Trash"


def test_resolve_trash_special_use_detected_from_list_response():
    """SPECIAL-USE \\Trash flag is read out of the LIST response."""
    conn = _FakeIMAP(list_resp=[b'(\\HasNoChildren \\Trash) "/" "Bin"'])
    assert email_mcp._resolve_trash_folder(conn, {}) == "Bin"


def test_resolve_trash_hardcoded_fallback_when_nothing_found():
    """Nothing advertises \\Trash → hard-coded 'Trash' default."""
    conn = _FakeIMAP(list_resp=[b'(\\HasNoChildren) "/" "INBOX"'])
    assert email_mcp._resolve_trash_folder(conn, {}) == "Trash"


# ---------------------------------------------------------------------------
# Outer-except tails & body branches — issue #8 iter-5
#
# Every IMAP write/folder/forward/reply/move/delete/expunge tool wraps its
# body in ``try: …; except Exception as e: return f"Error[…]: {e}"``. The
# happy-path tests above exercise the inner try; these tests inject a
# raising ``_imap_connect`` so the outer except fires, plus targeted body-
# branch tests for CREATE-fail / DELETE-fail / COPY-fail / FETCH-fail /
# UIDPLUS-missing arms.
# ---------------------------------------------------------------------------


def _raising_imap(monkeypatch, exc):
    """Override _imap_connect to raise ``exc`` on the next call."""
    def boom(acct):
        raise exc
    monkeypatch.setattr(email_mcp, "_imap_connect", boom)


def _make_logout_raiser(fake, exc):
    """Replace ``fake.logout`` with one that raises ``exc``."""
    def _raise():
        raise exc
    fake.logout = _raise


# --- outer-except tails ---------------------------------------------------


def test_create_folder_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom create"))
    result = run(email_mcp.email_create_folder(
        email_mcp.CreateFolderInput(account_id=ACCT_ID, folder="X")
    ))
    assert result.startswith("Error:")
    assert "boom create" in result


def test_delete_folder_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom delete"))
    result = run(email_mcp.email_delete_folder(
        email_mcp.DeleteFolderInput(account_id=ACCT_ID, folder="X")
    ))
    assert result.startswith("Error:")
    assert "boom delete" in result


def test_move_message_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom move"))
    result = run(email_mcp.email_move_message(
        email_mcp.MoveEmailInput(
            account_id=ACCT_ID, uid="1",
            source_folder="INBOX", dest_folder="Archive",
        )
    ))
    assert result.startswith("Error")
    assert "boom move" in result


def test_delete_message_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom delete msg"))
    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(account_id=ACCT_ID, uid="1")
    ))
    assert result.startswith("Error")
    assert "boom delete msg" in result


def test_expunge_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom expunge"))
    result = run(email_mcp.email_expunge(
        email_mcp.ExpungeInput(account_id=ACCT_ID, uid="1")
    ))
    assert result.startswith("Error")
    assert "boom expunge" in result


def test_reply_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom reply"))
    result = run(email_mcp.email_reply(
        email_mcp.ReplyEmailInput(
            account_id=ACCT_ID, uid="1", body="ack", reply_all=False,
        )
    ))
    assert result.startswith("Error")
    assert "boom reply" in result


def test_forward_outer_except_when_imap_connect_raises(stub_account, monkeypatch):
    _raising_imap(monkeypatch, RuntimeError("boom forward"))
    result = run(email_mcp.email_forward(
        email_mcp.ForwardEmailInput(
            account_id=ACCT_ID, uid="1", to="team@example.com",
        )
    ))
    assert result.startswith("Error")
    assert "boom forward" in result


# --- logout-swallow (inner `except Exception: pass`) ----------------------


def test_create_folder_swallows_logout_exception(stub_account, monkeypatch):
    imap = _FakeIMAP()
    _make_logout_raiser(imap, OSError("logout fail"))
    _install_imap(monkeypatch, imap)
    result = run(email_mcp.email_create_folder(
        email_mcp.CreateFolderInput(account_id=ACCT_ID, folder="Archive")
    ))
    # The happy-path "created" response is still returned — the swallow
    # caught the logout error.
    assert "created" in result.lower()
    assert not result.startswith("Error:")


# --- in-body branches: CREATE/DELETE/COPY/FETCH error arms ---------------


def test_create_folder_returns_error_on_non_ok_status(stub_account, monkeypatch):
    imap = _FakeIMAP()

    def _bad_create(folder):
        return ("NO", [b"server busy"])
    imap.create = _bad_create
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_create_folder(
        email_mcp.CreateFolderInput(account_id=ACCT_ID, folder="X")
    ))
    assert "Error creating folder" in result
    assert "server busy" in result


def test_delete_folder_returns_error_on_non_ok_status(stub_account, monkeypatch):
    imap = _FakeIMAP()

    def _bad_delete(folder):
        return ("NO", [b"server busy"])
    imap.delete = _bad_delete
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_folder(
        email_mcp.DeleteFolderInput(account_id=ACCT_ID, folder="X")
    ))
    assert "Error deleting folder" in result
    assert "server busy" in result


def test_move_message_returns_error_when_copy_fails(stub_account, monkeypatch):
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1", b"UIDPLUS"))
    orig_uid = imap.uid

    def _bad_uid(cmd, *args):
        if cmd == "COPY":
            return ("NO", [b"quota exceeded"])
        return orig_uid(cmd, *args)
    imap.uid = _bad_uid
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_move_message(
        email_mcp.MoveEmailInput(
            account_id=ACCT_ID, uid="1",
            source_folder="INBOX", dest_folder="Archive",
        )
    ))
    assert "Error copying message" in result
    assert "quota exceeded" in result
    # No EXPUNGE issued after the COPY failure.
    assert imap.uid_expunges == []


def test_delete_message_move_to_trash_refuses_without_uidplus(stub_account, monkeypatch):
    imap = _FakeIMAP(capabilities=(b"IMAP4REV1",))  # no UIDPLUS
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_delete_message(
        email_mcp.DeleteEmailInput(
            account_id=ACCT_ID, uid="42", folder="INBOX", permanent=False,
        )
    ))
    # The move-to-trash arm refuses without UIDPLUS, BEFORE resolving trash
    # or issuing COPY. Lines 2118-2123 in servers/email_mcp.py.
    assert "refusing to issue an untargeted EXPUNGE" in result
    assert "UIDPLUS" in result
    assert imap.copied == []
    assert imap.uid_expunges == []
    assert imap.bare_expunged is False


def test_reply_returns_error_when_original_fetch_fails(stub_account, monkeypatch):
    # FETCH returns NO for every UID → "Could not fetch message UID …"
    imap = _FakeIMAP(fetch_bodies={})
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_reply(
        email_mcp.ReplyEmailInput(
            account_id=ACCT_ID, uid="999", body="ack", reply_all=False,
        )
    ))
    assert "Error: Could not fetch message UID 999" in result


def test_forward_returns_error_when_original_fetch_fails(stub_account, monkeypatch):
    imap = _FakeIMAP(fetch_bodies={})
    _install_imap(monkeypatch, imap)

    result = run(email_mcp.email_forward(
        email_mcp.ForwardEmailInput(
            account_id=ACCT_ID, uid="999", to="team@example.com",
        )
    ))
    assert "Error: Could not fetch message UID 999" in result
