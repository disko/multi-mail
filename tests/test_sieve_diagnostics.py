"""Tests for the diagnostic error messages in ``_sieve_connect``.

When a ManageSieve connection fails — bad credentials, missing STARTTLS,
no SASL mechanisms advertised — the error must include enough context that
the user can fix their account config without reading the library source.
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


ACCT_STARTTLS = {
    "id": "work",
    "imap_host": "imap.example.com",
    "sieve_host": "sieve.example.com",
    "sieve_port": 4190,
    "sieve_security": "starttls",
    "sieve_allow_insecure": False,
    "username": "alice@example.com",
    "password": "secret",
}


class _FakeMS:
    """Stands in for ``managesieve.MANAGESIEVE`` construction.

    Captures constructor kwargs, exposes ``loginmechs``, and lets the test
    decide what ``login()`` returns or whether it raises.
    """

    instances = []

    class error(Exception):
        pass

    class abort(error):
        pass

    def __init__(self, host, port, use_tls, tls_verify, timeout):
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.tls_verify = tls_verify
        self.timeout = timeout
        self.loginmechs = _FakeMS._next_loginmechs
        self._login_result = _FakeMS._next_login_result
        self._login_raises = _FakeMS._next_login_raises
        _FakeMS.instances.append(self)

    def login(self, mech, user, password):
        self.login_args = (mech, user, password)
        if self._login_raises is not None:
            raise self._login_raises
        return self._login_result


def _patch_ms(
    monkeypatch, *, loginmechs=("PLAIN",), login_result="OK", login_raises=None
):
    """Monkeypatch email_mcp.ms.MANAGESIEVE (and its .error class) for one test."""
    _FakeMS.instances = []
    _FakeMS._next_loginmechs = list(loginmechs)
    _FakeMS._next_login_result = login_result
    _FakeMS._next_login_raises = login_raises
    # Replace just the MANAGESIEVE class on the imported managesieve module
    monkeypatch.setattr(email_mcp.ms, "MANAGESIEVE", _FakeMS)


def _stub_account(monkeypatch, **acct_overrides):
    acct = dict(ACCT_STARTTLS, **acct_overrides)
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: acct)
    return acct


# ---------------------------------------------------------------------------
# loginmechs empty → ConnectionError with context
# ---------------------------------------------------------------------------


def test_empty_loginmechs_raises_with_full_context(monkeypatch):
    """Empty mechanism list must surface security mode + host:port + a hint."""
    acct = _stub_account(monkeypatch)
    _patch_ms(monkeypatch, loginmechs=[])

    with pytest.raises(ConnectionError) as exc:
        email_mcp._sieve_connect(acct)

    msg = str(exc.value)
    assert "no SASL mechanisms" in msg
    assert "sieve.example.com:4190" in msg
    assert "security='starttls'" in msg


def test_empty_loginmechs_hints_at_starttls_when_security_is_not_starttls(monkeypatch):
    """If sieve_security != 'starttls', the error must tell the user that
    most servers require STARTTLS before advertising SASL."""
    acct = _stub_account(monkeypatch, sieve_security="none")
    _patch_ms(monkeypatch, loginmechs=[])

    with pytest.raises(ConnectionError) as exc:
        email_mcp._sieve_connect(acct)

    msg = str(exc.value)
    assert "sieve_security='none'" in msg
    assert "STARTTLS" in msg
    assert "accounts.json" in msg


def test_empty_loginmechs_no_starttls_hint_when_already_starttls(monkeypatch):
    """If user is already on STARTTLS, the hint shouldn't suggest setting it."""
    acct = _stub_account(monkeypatch)  # already starttls
    _patch_ms(monkeypatch, loginmechs=[])

    with pytest.raises(ConnectionError) as exc:
        email_mcp._sieve_connect(acct)

    msg = str(exc.value)
    # The "try setting sieve_security to starttls" sentence must NOT fire here
    assert "Try setting" not in msg


# ---------------------------------------------------------------------------
# login returns NO → ConnectionError with context + credentials hint
# ---------------------------------------------------------------------------


def test_login_returning_no_surfaces_credentials_hint(monkeypatch):
    acct = _stub_account(monkeypatch)
    _patch_ms(monkeypatch, loginmechs=["PLAIN"], login_result="NO")

    with pytest.raises(ConnectionError) as exc:
        email_mcp._sieve_connect(acct)

    msg = str(exc.value)
    assert "'NO'" in msg
    assert "username/password" in msg
    assert "server_mechanisms=['PLAIN']" in msg
    assert "sieve.example.com:4190" in msg


# ---------------------------------------------------------------------------
# login raises managesieve.error → wrapped with context
# ---------------------------------------------------------------------------


def test_login_raising_managesieve_error_is_wrapped(monkeypatch):
    acct = _stub_account(monkeypatch)
    boom = _FakeMS.abort("'No matching authentication mechanism found.'")
    _patch_ms(monkeypatch, loginmechs=["CRAM-MD5"], login_raises=boom)

    with pytest.raises(ConnectionError) as exc:
        email_mcp._sieve_connect(acct)

    msg = str(exc.value)
    assert "authentication error" in msg
    assert "No matching authentication mechanism" in msg
    assert "server_mechanisms=['CRAM-MD5']" in msg
    # __cause__ preserved for tracebacks
    assert exc.value.__cause__ is boom


# ---------------------------------------------------------------------------
# Happy path still returns the connection unchanged
# ---------------------------------------------------------------------------


def test_successful_login_returns_connection(monkeypatch):
    acct = _stub_account(monkeypatch)
    _patch_ms(monkeypatch, loginmechs=["PLAIN"], login_result="OK")

    conn = email_mcp._sieve_connect(acct)
    assert isinstance(conn, _FakeMS)
    assert conn.login_args == ("PLAIN", "alice@example.com", "secret")
    assert conn.use_tls is True  # security="starttls" → use_tls=True
