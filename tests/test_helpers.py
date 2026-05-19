"""Tests for small pure helpers in ``servers/email_mcp.py`` — RFC 2047
header decoding, MIME body extraction, message summary, autoconfig
socket-type mapping, and the email-domain splitter.

These are all unit-testable without touching the network."""
from __future__ import annotations

import email
import email.mime.multipart
import email.mime.text
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)

_decode_header = email_mcp._decode_header
_get_body = email_mcp._get_body
_summarise_msg = email_mcp._summarise_msg
_domain_from_email = email_mcp._domain_from_email
_map_socket_type = email_mcp._map_socket_type


# ---------------------------------------------------------------------------
# _decode_header — RFC 2047
# ---------------------------------------------------------------------------

def test_decode_header_plain_ascii():
    assert _decode_header("Hello") == "Hello"


def test_decode_header_none_returns_empty():
    assert _decode_header(None) == ""


def test_decode_header_empty_returns_empty():
    assert _decode_header("") == ""


def test_decode_header_rfc2047_utf8_b():
    # =?UTF-8?B?...?= → base64-encoded UTF-8
    encoded = "=?UTF-8?B?SOKAmWFsbG8gV29ybGQ=?="  # "H’allo World"
    assert _decode_header(encoded) == "H’allo World"


def test_decode_header_rfc2047_quoted_printable():
    encoded = "=?utf-8?Q?H=C3=A4llo_W=C3=B6rld?="
    assert _decode_header(encoded) == "Hällo Wörld"


def test_decode_header_mixed_segments():
    encoded = "=?utf-8?Q?H=C3=A4llo?= World"
    assert _decode_header(encoded) == "Hällo World"


def test_decode_header_unknown_charset_falls_back_to_utf8():
    encoded = "=?bogus-charset?Q?Hello?="
    # falls back to utf-8 with replacement
    assert "Hello" in _decode_header(encoded)


# ---------------------------------------------------------------------------
# _get_body — plain/HTML extraction from MIME
# ---------------------------------------------------------------------------

def _multipart(parts):
    msg = email.mime.multipart.MIMEMultipart("alternative")
    for p in parts:
        msg.attach(p)
    return msg


def test_get_body_plain_text_singlepart():
    msg = email.mime.text.MIMEText("hello plaintext", "plain", "utf-8")
    assert _get_body(msg) == "hello plaintext"


def test_get_body_prefers_plain_over_html():
    msg = _multipart([
        email.mime.text.MIMEText("<b>html version</b>", "html", "utf-8"),
        email.mime.text.MIMEText("plain version", "plain", "utf-8"),
    ])
    assert _get_body(msg) == "plain version"


def test_get_body_falls_back_to_html_when_no_plain():
    msg = _multipart([
        email.mime.text.MIMEText("<b>only html</b>", "html", "utf-8"),
    ])
    assert _get_body(msg) == "<b>only html</b>"


def test_get_body_handles_utf8_charset():
    msg = email.mime.text.MIMEText("Grüße", "plain", "utf-8")
    assert _get_body(msg) == "Grüße"


def test_get_body_empty_when_no_text_part():
    msg = email.mime.multipart.MIMEMultipart("mixed")
    # No text parts at all
    assert _get_body(msg) == ""


# ---------------------------------------------------------------------------
# _summarise_msg
# ---------------------------------------------------------------------------

def test_summarise_msg_extracts_standard_headers():
    msg = email.mime.text.MIMEText("body", "plain", "utf-8")
    msg["From"] = "alice@example.com"
    msg["To"] = "bob@example.com"
    msg["Subject"] = "=?utf-8?Q?H=C3=A4llo?="
    msg["Date"] = "Mon, 13 May 2026 12:00:00 +0000"
    msg["Message-ID"] = "<abc@example.com>"

    s = _summarise_msg(msg, "42")
    assert s == {
        "uid": "42",
        "from": "alice@example.com",
        "to": "bob@example.com",
        "subject": "Hällo",
        "date": "Mon, 13 May 2026 12:00:00 +0000",
        "message_id": "<abc@example.com>",
    }


def test_summarise_msg_missing_headers_blank():
    msg = email.mime.text.MIMEText("body", "plain", "utf-8")
    s = _summarise_msg(msg, "1")
    assert s["from"] == ""
    assert s["to"] == ""
    assert s["subject"] == ""
    assert s["message_id"] == ""


# ---------------------------------------------------------------------------
# _domain_from_email + _map_socket_type
# ---------------------------------------------------------------------------

def test_domain_from_email_simple():
    assert _domain_from_email("alice@example.com") == "example.com"


def test_domain_from_email_lowercases():
    assert _domain_from_email("Alice@EXAMPLE.COM") == "example.com"


def test_domain_from_email_strips_whitespace():
    assert _domain_from_email("alice@ example.com ") == "example.com"


def test_domain_from_email_with_plus_alias():
    assert _domain_from_email("alice+work@example.com") == "example.com"


def test_domain_from_email_handles_no_at_sign():
    # No @ → whole string is the "domain"
    assert _domain_from_email("alice") == "alice"


def test_map_socket_type_ssl():
    assert _map_socket_type("SSL") == "ssl"
    assert _map_socket_type("TLS") == "ssl"
    assert _map_socket_type("ssl") == "ssl"


def test_map_socket_type_starttls():
    assert _map_socket_type("STARTTLS") == "starttls"
    assert _map_socket_type("starttls") == "starttls"


def test_map_socket_type_plaintext_is_none():
    assert _map_socket_type("plain") == "none"
    assert _map_socket_type("") == "none"
    assert _map_socket_type("anything-else") == "none"


# ---------------------------------------------------------------------------
# _parse_imap_list_line — defensive branches for malformed inputs (#8 iter-6)
#
# RFC 3501 LIST responses are messy in the wild. The parser must not crash on
# malformed input — it returns None and the caller drops the entry. These
# tests pin every defensive return-None branch.
# ---------------------------------------------------------------------------

_parse_imap_list_line = email_mcp._parse_imap_list_line


def test_parse_imap_list_line_tuple_with_none_payload_returns_none():
    """imaplib literal-form tuples must have a bytes payload at index 1.
    None means we received header bytes but no literal body — bail."""
    assert _parse_imap_list_line((b'(\\HasNoChildren) "/" {5}', None)) is None


def test_parse_imap_list_line_tuple_with_non_decodable_payload_returns_none():
    """Tuple's index-1 element isn't bytes-like → AttributeError → return None.
    Pins lines 416-417."""
    # Pass a non-bytes object (int has no .decode) — caught by the
    # AttributeError handler.
    assert _parse_imap_list_line((b'(\\HasNoChildren) "/" {5}', 12345)) is None


def test_parse_imap_list_line_short_tuple_returns_none():
    """A single-element tuple is malformed — return None."""
    assert _parse_imap_list_line((b'(\\HasNoChildren) "/" {5}',)) is None


def test_parse_imap_list_line_unknown_type_returns_none():
    """Anything that's neither bytes/bytearray nor tuple → None."""
    assert _parse_imap_list_line(123) is None
    assert _parse_imap_list_line(None) is None
    assert _parse_imap_list_line("not bytes") is None


def test_parse_imap_list_line_empty_text_returns_none():
    """Whitespace-only bytes → None."""
    assert _parse_imap_list_line(b"   ") is None
    assert _parse_imap_list_line(b"") is None


def test_parse_imap_list_line_unbalanced_parens_returns_none():
    """Opening paren never closes → None."""
    assert _parse_imap_list_line(b'(\\HasNoChildren "/" "INBOX"') is None


def test_parse_imap_list_line_unterminated_delimiter_returns_none():
    """Quoted delimiter starts but never closes → None."""
    assert _parse_imap_list_line(b'(\\HasNoChildren) "/ INBOX') is None


def test_parse_imap_list_line_atom_delimiter_no_name_returns_none():
    """Atom-form delimiter (NIL) with no following mailbox token → None."""
    assert _parse_imap_list_line(b'(\\HasNoChildren) NIL') is None


def test_parse_imap_list_line_unterminated_quoted_name_returns_none():
    """Mailbox name opens with " but never closes → None."""
    assert _parse_imap_list_line(b'(\\HasNoChildren) "/" "INBOX') is None


def test_parse_imap_list_line_only_delimiter_returns_none():
    """Quoted delimiter consumes the input — nothing left for the name → None.

    Pins the post-delimiter empty-text return (line 456).
    """
    # After parsing `(\\HasNoChildren)` and `"/"`, the remainder is just
    # whitespace → empty text branch fires.
    assert _parse_imap_list_line(b'(\\HasNoChildren) "/"   ') is None


def test_parse_imap_list_line_atom_form_returns_last_token():
    """Atom-form mailbox name (no quotes) — pins the tokens[-1] path (467-469)."""
    assert _parse_imap_list_line(b'(\\HasNoChildren) "/" INBOX') == "INBOX"


def test_parse_imap_list_line_atom_form_nil_delimiter_returns_name():
    """Atom NIL delimiter + atom mailbox name → name extracted (no delimiter)."""
    assert _parse_imap_list_line(b'(\\HasNoChildren) NIL INBOX') == "INBOX"


# ---------------------------------------------------------------------------
# _get_body — fall-through paths (#8 iter-6)
# ---------------------------------------------------------------------------


def test_get_body_returns_empty_on_singlepart_without_payload():
    """A singlepart message with an empty payload returns "". Pins the
    `if payload:` false branch at line 380->383."""
    msg = email.mime.text.MIMEText("", "plain", "utf-8")
    # Force payload to be falsy.
    msg.set_payload(b"")
    # MIMEText defaults set a charset; with empty payload _get_body falls
    # through and returns "".
    assert _get_body(msg) == ""


# ---------------------------------------------------------------------------
# _get_body — multipart-walk continue-after-empty-payload (#8 iter-7)
#
# When a multipart message has a payload-bearing part AFTER an empty part of
# the same content-type, the parser must continue past the empty part rather
# than short-circuit. Pins partials 367->363 (text/plain loop) and 375->371
# (text/html fallback loop).
# ---------------------------------------------------------------------------


def test_get_body_multipart_continues_past_empty_text_plain():
    """Two text/plain parts: first empty, second has content. Pins 367->363."""
    msg = email.mime.multipart.MIMEMultipart()
    p1 = email.mime.text.MIMEText("", "plain", "utf-8")
    p1.set_payload(b"")
    p2 = email.mime.text.MIMEText("real content", "plain", "utf-8")
    msg.attach(p1)
    msg.attach(p2)
    assert _get_body(msg) == "real content"


def test_get_body_multipart_html_fallback_continues_past_empty_part():
    """No text/plain, two text/html parts: first empty, second has content.
    Pins 375->371."""
    msg = email.mime.multipart.MIMEMultipart()
    p1 = email.mime.text.MIMEText("", "html", "utf-8")
    p1.set_payload(b"")
    p2 = email.mime.text.MIMEText("<b>real</b>", "html", "utf-8")
    msg.attach(p1)
    msg.attach(p2)
    assert _get_body(msg) == "<b>real</b>"


# ---------------------------------------------------------------------------
# _parse_imap_list_line — paren-handling partials (#8 iter-7)
# ---------------------------------------------------------------------------


def test_parse_imap_list_line_no_leading_paren_uses_atom_delim_path():
    """Input doesn't start with '(' → the flag-group strip is skipped and
    the first token is taken as the delimiter atom. Pins partial 428->443."""
    # No flag group; the first token IS the delimiter atom (NIL).
    # Then the rest is the mailbox name.
    assert _parse_imap_list_line(b'NIL Inbox') == "Inbox"


def test_parse_imap_list_line_nested_parens_in_flag_group():
    """Nested parens in the flag group keep depth > 0 until the outer
    closer. Pins partial 435->430 (loop continues past inner close)."""
    assert _parse_imap_list_line(b'(()()) "/" "INBOX"') == "INBOX"


# ---------------------------------------------------------------------------
# _resolve_trash_folder — list-status NO + \Trash-parse-fail (#8 iter-7)
# ---------------------------------------------------------------------------


def test_resolve_trash_folder_list_status_not_ok_falls_through():
    """``conn.list()`` returns ("NO", []) → the `if status == "OK"` arm is
    skipped; function falls through to the hardcoded "Trash" default.
    Pins 501->516."""
    class _NOIMAP:
        def list(self):
            return ("NO", [])
    assert email_mcp._resolve_trash_folder(_NOIMAP(), {"id": "x"}) == "Trash"


def test_resolve_trash_folder_trash_flag_with_unparseable_name_skips():
    """LIST entry contains the ``\\Trash`` flag (so substring scan hits) but
    ``_parse_imap_list_line`` returns None for the line → continue. With no
    other Trash candidate found, fall back to the hardcoded default.
    Pins partial 511->502."""
    class _BadIMAP:
        def list(self):
            # Unterminated quoted name → parser returns None at line 462.
            return ("OK", [b'(\\HasNoChildren \\Trash) "/" "unterminated'])
    assert email_mcp._resolve_trash_folder(_BadIMAP(), {"id": "x"}) == "Trash"
