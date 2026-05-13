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
