"""Tests for the attachment-handling tool flows:
``email_list_attachments`` and ``email_get_attachment``.

Reuses the in-process ``_FakeIMAP`` pattern from ``test_imap_tool_flows`` —
no real socket touched, all I/O lives in tmp_path.
"""

from __future__ import annotations

import asyncio
import email.mime.base
import email.mime.multipart
import email.mime.text
import email.encoders
import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError


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


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def _plain_msg_bytes(body: str = "hello, no attachments") -> bytes:
    """Single-part text/plain message — no attachments."""
    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["From"] = "alice@example.com"
    msg["To"] = "me@example.com"
    msg["Subject"] = "no attachments"
    msg["Message-ID"] = "<plain@example.com>"
    return msg.as_bytes()


def _multipart_with_attachments(parts: list[tuple[str, str, bytes]]) -> bytes:
    """Build a multipart/mixed message with one text body + given attachments.

    ``parts`` items are ``(filename, mime_subtype, payload_bytes)``. Setting
    ``filename`` to None creates an inline part *without* a filename, which
    should be skipped by the attachment iterator.
    """
    outer = email.mime.multipart.MIMEMultipart("mixed")
    outer["From"] = "alice@example.com"
    outer["To"] = "me@example.com"
    outer["Subject"] = "with attachments"
    outer["Message-ID"] = "<multi@example.com>"
    outer.attach(email.mime.text.MIMEText("see attached", "plain", "utf-8"))
    for filename, subtype, data in parts:
        part = email.mime.base.MIMEBase("application", subtype)
        part.set_payload(data)
        email.encoders.encode_base64(part)
        if filename is not None:
            part.add_header("Content-Disposition", "attachment", filename=filename)
        outer.attach(part)
    return outer.as_bytes()


def _inline_with_filename(filename: str, payload: bytes) -> bytes:
    """Inline part that nevertheless carries a filename (the Schotten case)."""
    outer = email.mime.multipart.MIMEMultipart("mixed")
    outer["From"] = "alice@example.com"
    outer["To"] = "me@example.com"
    outer["Subject"] = "inline pdf"
    outer["Message-ID"] = "<inline@example.com>"
    outer.attach(email.mime.text.MIMEText("see embedded", "plain", "utf-8"))
    part = email.mime.base.MIMEBase("application", "pdf")
    part.set_payload(payload)
    email.encoders.encode_base64(part)
    # Note: 'inline' disposition, but a filename present — should still count.
    part.add_header("Content-Disposition", "inline", filename=filename)
    outer.attach(part)
    return outer.as_bytes()


def _attachment_without_filename(payload: bytes) -> bytes:
    """Attachment-disposition part that carries no filename at all.

    Exercises the ``attachment-{idx}`` placeholder branch in
    ``_attachment_filename``.
    """
    outer = email.mime.multipart.MIMEMultipart("mixed")
    outer["From"] = "alice@example.com"
    outer["To"] = "me@example.com"
    outer["Subject"] = "nameless attachment"
    outer["Message-ID"] = "<nameless@example.com>"
    outer.attach(email.mime.text.MIMEText("body", "plain", "utf-8"))
    part = email.mime.base.MIMEBase("application", "octet-stream")
    part.set_payload(payload)
    email.encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment")  # no filename
    outer.attach(part)
    return outer.as_bytes()


# ---------------------------------------------------------------------------
# Fake IMAP (minimal — only what these tools call)
# ---------------------------------------------------------------------------


class _FakeIMAP:
    def __init__(self, *, fetch_bodies=None):
        self.fetch_bodies = fetch_bodies or {}
        self.selected = None
        self.select_readonly = None
        self.fetch_calls = []
        self.logged_out = False

    def select(self, folder, readonly=False):
        self.selected = folder
        self.select_readonly = readonly
        return ("OK", [b""])

    def uid(self, cmd, *args):
        if cmd == "FETCH":
            uid, _fetch_spec = args[0], args[1]
            self.fetch_calls.append(args)
            body = self.fetch_bodies.get(uid)
            if not body:
                return ("NO", [None])
            return ("OK", [(b"" + uid.encode() + b" (BODY[] {len})", body)])
        return ("NO", [None])

    def logout(self):
        self.logged_out = True


@pytest.fixture
def stub_account(monkeypatch):
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)


def _install_imap(monkeypatch, fake):
    monkeypatch.setattr(email_mcp, "_imap_connect", lambda acct: fake)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# email_list_attachments
# ---------------------------------------------------------------------------


def test_list_attachments_empty_when_no_parts(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={"1": _plain_msg_bytes()})
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="1", folder="INBOX")
        )
    )
    assert "No attachments found" in result
    # Must use PEEK so the message stays unread.
    assert fake.fetch_calls[0][1] == "(BODY.PEEK[])"
    assert fake.select_readonly is True
    assert fake.logged_out is True


def test_list_attachments_renders_table_for_multiple_parts(stub_account, monkeypatch):
    body = _multipart_with_attachments(
        [
            ("report.pdf", "pdf", b"PDF-payload-bytes"),
            ("data.csv", "csv", b"a,b,c\n1,2,3\n"),
        ]
    )
    fake = _FakeIMAP(fetch_bodies={"5": body})
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="5")
        )
    )
    assert "| Index | Filename | Content-Type | Size (bytes) |" in result
    assert "| 0 | report.pdf | application/pdf |" in result
    assert "| 1 | data.csv | application/csv |" in result
    assert fake.logged_out is True


def test_list_attachments_includes_inline_with_filename(stub_account, monkeypatch):
    """Outlook-style inline-with-filename parts (e.g. Schotten PDF) must show up."""
    body = _inline_with_filename("schotten.pdf", b"%PDF-1.4 inline")
    fake = _FakeIMAP(fetch_bodies={"7": body})
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="7")
        )
    )
    assert "schotten.pdf" in result
    assert "| 0 |" in result


def test_list_attachments_missing_uid_surfaces_error(stub_account, monkeypatch):
    fake = _FakeIMAP(fetch_bodies={})
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="999")
        )
    )
    assert "Error: Could not fetch message UID 999" in result
    assert fake.logged_out is True


# ---------------------------------------------------------------------------
# email_get_attachment — selector validation
# ---------------------------------------------------------------------------


def test_get_attachment_requires_exactly_one_selector(tmp_path):
    save = tmp_path / "out.bin"
    # Neither index nor filename → error.
    with pytest.raises(ValidationError):
        email_mcp.GetAttachmentInput(account_id=ACCT_ID, uid="1", save_path=str(save))
    # Both index and filename → error.
    with pytest.raises(ValidationError):
        email_mcp.GetAttachmentInput(
            account_id=ACCT_ID,
            uid="1",
            index=0,
            filename="report.pdf",
            save_path=str(save),
        )


# ---------------------------------------------------------------------------
# email_get_attachment — save_path validation
# ---------------------------------------------------------------------------


def test_get_attachment_rejects_relative_save_path():
    with pytest.raises(ValidationError):
        email_mcp.GetAttachmentInput(
            account_id=ACCT_ID,
            uid="1",
            index=0,
            save_path="relative/out.bin",
        )


@pytest.mark.parametrize(
    "forbidden",
    [
        "/etc/passwd",
        "/etc/something/else.bin",
        "/usr/local/bin/pwn.bin",
        "/bin/sh",
        "/sbin/init",
        "/System/Library/foo",
        "/Library/System/secret",
    ],
)
def test_get_attachment_rejects_system_directory_save_path(forbidden):
    with pytest.raises(ValidationError):
        email_mcp.GetAttachmentInput(
            account_id=ACCT_ID,
            uid="1",
            index=0,
            save_path=forbidden,
        )


def test_get_attachment_rejects_save_path_traversal_into_etc():
    """Normalised path landing under /etc must be rejected."""
    with pytest.raises(ValidationError):
        email_mcp.GetAttachmentInput(
            account_id=ACCT_ID,
            uid="1",
            index=0,
            save_path="/var/tmp/../../etc/sneaky",
        )


# ---------------------------------------------------------------------------
# email_get_attachment — happy paths
# ---------------------------------------------------------------------------


def test_get_attachment_by_index_writes_bytes(stub_account, monkeypatch, tmp_path):
    payload = b"%PDF-1.4 fake bytes"
    body = _multipart_with_attachments(
        [
            ("report.pdf", "pdf", payload),
            ("data.csv", "csv", b"a,b\n1,2\n"),
        ]
    )
    fake = _FakeIMAP(fetch_bodies={"3": body})
    _install_imap(monkeypatch, fake)
    target = tmp_path / "nested" / "report.pdf"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                index=0,
                save_path=str(target),
            )
        )
    )
    assert target.exists()
    assert target.read_bytes() == payload
    assert "**Filename**: report.pdf" in result
    assert "**Content-Type**: application/pdf" in result
    assert str(target) in result
    # Must use PEEK so the message stays unread.
    assert fake.fetch_calls[0][1] == "(BODY.PEEK[])"
    assert fake.select_readonly is True
    assert fake.logged_out is True


def test_get_attachment_by_filename_writes_bytes(stub_account, monkeypatch, tmp_path):
    payload = b"col1,col2\nval1,val2\n"
    body = _multipart_with_attachments(
        [
            ("report.pdf", "pdf", b"PDF"),
            ("data.csv", "csv", payload),
        ]
    )
    fake = _FakeIMAP(fetch_bodies={"3": body})
    _install_imap(monkeypatch, fake)
    target = tmp_path / "data.csv"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                filename="data.csv",
                save_path=str(target),
            )
        )
    )
    assert target.exists()
    assert target.read_bytes() == payload
    assert "**Filename**: data.csv" in result


def test_get_attachment_by_filename_missing_returns_error(
    stub_account, monkeypatch, tmp_path
):
    body = _multipart_with_attachments([("report.pdf", "pdf", b"PDF")])
    fake = _FakeIMAP(fetch_bodies={"3": body})
    _install_imap(monkeypatch, fake)
    target = tmp_path / "out.bin"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                filename="missing.pdf",
                save_path=str(target),
            )
        )
    )
    assert "Error: No attachment matching" in result
    assert "missing.pdf" in result
    assert not target.exists()


def test_get_attachment_by_index_out_of_range_returns_error(
    stub_account, monkeypatch, tmp_path
):
    body = _multipart_with_attachments([("report.pdf", "pdf", b"PDF")])
    fake = _FakeIMAP(fetch_bodies={"3": body})
    _install_imap(monkeypatch, fake)
    target = tmp_path / "out.bin"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                index=99,
                save_path=str(target),
            )
        )
    )
    assert "Error: No attachment matching" in result
    assert "index=99" in result
    assert not target.exists()


def test_get_attachment_uid_missing_returns_error(stub_account, monkeypatch, tmp_path):
    fake = _FakeIMAP(fetch_bodies={})
    _install_imap(monkeypatch, fake)
    target = tmp_path / "out.bin"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="999",
                index=0,
                save_path=str(target),
            )
        )
    )
    assert "Error: Could not fetch message UID 999" in result
    assert not target.exists()
    assert fake.logged_out is True


def test_get_attachment_outer_except_when_imap_connect_raises(
    stub_account, monkeypatch, tmp_path
):
    def boom(acct):
        raise RuntimeError("boom get attachment")

    monkeypatch.setattr(email_mcp, "_imap_connect", boom)
    target = tmp_path / "out.bin"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="1",
                index=0,
                save_path=str(target),
            )
        )
    )
    assert result.startswith("Error:")
    assert "boom get attachment" in result


# ---------------------------------------------------------------------------
# Coverage completeness — filename placeholder, logout swallow, outer except,
# non-decodable payload
# ---------------------------------------------------------------------------


def test_list_attachments_uses_placeholder_for_nameless_attachment(
    stub_account, monkeypatch
):
    """An attachment part with no filename renders as ``attachment-{idx}``."""
    fake = _FakeIMAP(fetch_bodies={"8": _attachment_without_filename(b"raw-bytes")})
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="8")
        )
    )
    assert "attachment-0" in result
    assert "| 0 | attachment-0 |" in result


def test_list_attachments_swallows_logout_error(stub_account, monkeypatch):
    """A raising ``logout()`` in the finally block must not mask the result."""
    fake = _FakeIMAP(
        fetch_bodies={"5": _multipart_with_attachments([("a.pdf", "pdf", b"X")])}
    )

    def _boom_logout():
        raise RuntimeError("logout boom")

    fake.logout = _boom_logout
    _install_imap(monkeypatch, fake)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="5")
        )
    )
    # Table still returned despite the logout failure.
    assert "| 0 | a.pdf |" in result


def test_list_attachments_outer_except_when_imap_connect_raises(
    stub_account, monkeypatch
):
    def boom(acct):
        raise RuntimeError("boom list attachments")

    monkeypatch.setattr(email_mcp, "_imap_connect", boom)

    result = run(
        email_mcp.email_list_attachments(
            email_mcp.ListAttachmentsInput(account_id=ACCT_ID, uid="1")
        )
    )
    assert result.startswith("Error:")
    assert "boom list attachments" in result


def test_get_attachment_non_decodable_payload_returns_error(
    stub_account, monkeypatch, tmp_path
):
    """A part whose ``get_payload(decode=True)`` is None surfaces an error."""

    class _NoPayloadPart:
        def get_filename(self):
            return "broken.bin"

        def get_payload(self, decode=False):
            return None

        def get_content_type(self):
            return "application/octet-stream"

    fake = _FakeIMAP(fetch_bodies={"3": _plain_msg_bytes()})
    _install_imap(monkeypatch, fake)
    monkeypatch.setattr(
        email_mcp, "_iter_attachment_parts", lambda msg: [_NoPayloadPart()]
    )
    target = tmp_path / "out.bin"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                index=0,
                save_path=str(target),
            )
        )
    )
    assert "no decodable payload" in result
    assert "broken.bin" in result
    assert not target.exists()


def test_get_attachment_swallows_logout_error(stub_account, monkeypatch, tmp_path):
    """A raising ``logout()`` after a successful write must not mask success."""
    payload = b"%PDF saved bytes"
    fake = _FakeIMAP(
        fetch_bodies={
            "3": _multipart_with_attachments([("report.pdf", "pdf", payload)])
        }
    )

    def _boom_logout():
        raise RuntimeError("logout boom")

    fake.logout = _boom_logout
    _install_imap(monkeypatch, fake)
    target = tmp_path / "report.pdf"

    result = run(
        email_mcp.email_get_attachment(
            email_mcp.GetAttachmentInput(
                account_id=ACCT_ID,
                uid="3",
                index=0,
                save_path=str(target),
            )
        )
    )
    assert "# Saved attachment" in result
    assert target.read_bytes() == payload
