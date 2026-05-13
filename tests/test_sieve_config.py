"""Tests for ManageSieve connection parameter resolution.

Regression coverage for the bug where ``sieve_host: null`` in the saved
account JSON made the plugin dial localhost instead of the IMAP host
(``socket.create_connection((None, 4190))`` → ``[Errno 61] Connection refused``).
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
_resolve_sieve_params = email_mcp._resolve_sieve_params


def _base_acct(**overrides):
    acct = {
        "id": "test",
        "imap_host": "imap.example.com",
        "imap_allow_insecure": False,
        "sieve_host": None,
        "sieve_port": 4190,
        "sieve_security": "starttls",
        "sieve_allow_insecure": False,
        "username": "user",
        "password": "pw",
    }
    acct.update(overrides)
    return acct


def test_sieve_host_falls_back_to_imap_when_null():
    """The original bug: dict.get('sieve_host', imap_host) returns None when
    the JSON contains "sieve_host": null. The fix uses truthy-OR fallback."""
    host, *_ = _resolve_sieve_params(_base_acct(sieve_host=None))
    assert host == "imap.example.com"


def test_sieve_host_falls_back_to_imap_when_missing():
    acct = _base_acct()
    del acct["sieve_host"]
    host, *_ = _resolve_sieve_params(acct)
    assert host == "imap.example.com"


def test_sieve_host_respects_explicit_value():
    host, *_ = _resolve_sieve_params(_base_acct(sieve_host="sieve.example.com"))
    assert host == "sieve.example.com"


def test_sieve_port_defaults_to_4190_when_null():
    _, port, *_ = _resolve_sieve_params(_base_acct(sieve_port=None))
    assert port == 4190


def test_sieve_security_defaults_to_starttls_when_null():
    _, _, security, _ = _resolve_sieve_params(_base_acct(sieve_security=None))
    assert security == "starttls"


def test_sieve_security_lowercased():
    _, _, security, _ = _resolve_sieve_params(_base_acct(sieve_security="STARTTLS"))
    assert security == "starttls"


def test_sieve_allow_insecure_inherits_from_imap_when_unset():
    *_, allow = _resolve_sieve_params(
        _base_acct(sieve_allow_insecure=None, imap_allow_insecure=True)
    )
    assert allow is True


def test_sieve_allow_insecure_respects_explicit_false():
    """Sieve must NOT inherit imap_allow_insecure=True if sieve_allow_insecure=False
    was set explicitly — that would silently widen the trust boundary."""
    *_, allow = _resolve_sieve_params(
        _base_acct(sieve_allow_insecure=False, imap_allow_insecure=True)
    )
    assert allow is False
