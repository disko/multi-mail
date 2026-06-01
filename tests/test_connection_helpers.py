"""Tests for the connection-construction helpers: ``_imap_connect``,
``_smtp_connect``, ``_caldav_client``, and ``_carddav_headers``.

Every other test file in this repo monkeypatches these helpers at the
function seam — so their bodies (variant matrix across security modes,
allow_insecure toggles, no-URL error branches) are never exercised
through the existing suite. This file fills that gap by replacing the
stdlib-shaped construction classes (``imaplib.IMAP4_SSL`` /
``imaplib.IMAP4`` / ``smtplib.SMTP_SSL`` / ``smtplib.SMTP`` /
``caldav.DAVClient``) with constructor-capture fakes — mirroring the
``_FakeMS`` pattern in ``test_sieve_diagnostics.py``.
"""

from __future__ import annotations

import importlib.util
import ssl
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)


# ---------------------------------------------------------------------------
# Constructor-capture fakes (mirror ``_FakeMS`` in test_sieve_diagnostics.py)
# ---------------------------------------------------------------------------


class _FakeIMAP4SSL:
    """Stands in for ``imaplib.IMAP4_SSL``."""

    instances: list = []

    def __init__(self, host, port, ssl_context=None):
        self.host = host
        self.port = port
        self.ssl_context = ssl_context
        self.login_args = None
        _FakeIMAP4SSL.instances.append(self)

    def login(self, user, password):
        self.login_args = (user, password)
        return ("OK", [b""])


class _FakeIMAP4:
    """Stands in for ``imaplib.IMAP4`` (plaintext or STARTTLS base)."""

    instances: list = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.starttls_ctx = None
        self.starttls_called = False
        self.login_args = None
        _FakeIMAP4.instances.append(self)

    def starttls(self, ssl_context=None):
        self.starttls_called = True
        self.starttls_ctx = ssl_context

    def login(self, user, password):
        self.login_args = (user, password)
        return ("OK", [b""])


class _FakeSMTPSSL:
    """Stands in for ``smtplib.SMTP_SSL``."""

    instances: list = []

    def __init__(self, host, port, context=None):
        self.host = host
        self.port = port
        self.context = context
        self.login_args = None
        _FakeSMTPSSL.instances.append(self)

    def login(self, user, password):
        self.login_args = (user, password)


class _FakeSMTP:
    """Stands in for ``smtplib.SMTP`` (plaintext or STARTTLS base)."""

    instances: list = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.ehlo_count = 0
        self.starttls_ctx = None
        self.starttls_called = False
        self.login_args = None
        _FakeSMTP.instances.append(self)

    def ehlo(self):
        self.ehlo_count += 1

    def starttls(self, context=None):
        self.starttls_called = True
        self.starttls_ctx = context

    def login(self, user, password):
        self.login_args = (user, password)


class _FakeDAVClient:
    """Stands in for ``caldav.DAVClient``."""

    instances: list = []

    def __init__(self, url, username, password, ssl_verify_cert):
        self.url = url
        self.username = username
        self.password = password
        self.ssl_verify_cert = ssl_verify_cert
        _FakeDAVClient.instances.append(self)


def _reset_instances() -> None:
    _FakeIMAP4SSL.instances = []
    _FakeIMAP4.instances = []
    _FakeSMTPSSL.instances = []
    _FakeSMTP.instances = []
    _FakeDAVClient.instances = []


def _install_imap_fakes(monkeypatch) -> None:
    _reset_instances()
    monkeypatch.setattr(email_mcp.imaplib, "IMAP4_SSL", _FakeIMAP4SSL)
    monkeypatch.setattr(email_mcp.imaplib, "IMAP4", _FakeIMAP4)


def _install_smtp_fakes(monkeypatch) -> None:
    _reset_instances()
    monkeypatch.setattr(email_mcp.smtplib, "SMTP_SSL", _FakeSMTPSSL)
    monkeypatch.setattr(email_mcp.smtplib, "SMTP", _FakeSMTP)


def _install_caldav_fake(monkeypatch) -> None:
    _reset_instances()
    monkeypatch.setattr(email_mcp.caldav, "DAVClient", _FakeDAVClient)


ACCT_BASE = {
    "id": "work",
    "imap_host": "imap.example.com",
    "smtp_host": "smtp.example.com",
    "username": "alice@example.com",
    "password": "secret",
}


# ---------------------------------------------------------------------------
# Group A — _imap_connect (5 variants)
# ---------------------------------------------------------------------------


def test_imap_connect_ssl_default_uses_strict_ssl_context(monkeypatch):
    """Default security=ssl with no override → IMAP4_SSL on port 993 with strict ctx."""
    _install_imap_fakes(monkeypatch)
    acct = dict(ACCT_BASE)

    email_mcp._imap_connect(acct)

    assert len(_FakeIMAP4SSL.instances) == 1
    conn = _FakeIMAP4SSL.instances[0]
    assert conn.host == "imap.example.com"
    assert conn.port == 993
    assert conn.ssl_context.check_hostname is True
    assert conn.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert conn.login_args == ("alice@example.com", "secret")
    assert len(_FakeIMAP4.instances) == 0


def test_imap_connect_ssl_with_allow_insecure_relaxes_context(monkeypatch):
    """security=ssl + imap_allow_insecure=True → context check_hostname/verify relaxed."""
    _install_imap_fakes(monkeypatch)
    acct = dict(
        ACCT_BASE,
        imap_security="ssl",
        imap_allow_insecure=True,
        imap_port=9993,
    )

    email_mcp._imap_connect(acct)

    assert len(_FakeIMAP4SSL.instances) == 1
    conn = _FakeIMAP4SSL.instances[0]
    assert conn.port == 9993
    assert conn.ssl_context.check_hostname is False
    assert conn.ssl_context.verify_mode == ssl.CERT_NONE


def test_imap_connect_starttls_default_constructs_imap4_and_calls_starttls(monkeypatch):
    """security=starttls → IMAP4 plaintext base + starttls() with strict ctx."""
    _install_imap_fakes(monkeypatch)
    acct = dict(ACCT_BASE, imap_security="starttls")

    email_mcp._imap_connect(acct)

    assert len(_FakeIMAP4.instances) == 1
    conn = _FakeIMAP4.instances[0]
    assert conn.host == "imap.example.com"
    assert conn.port == 993  # acct.get("imap_port", 993) default
    assert conn.starttls_called is True
    assert conn.starttls_ctx is not None
    assert conn.starttls_ctx.check_hostname is True
    assert conn.starttls_ctx.verify_mode == ssl.CERT_REQUIRED
    assert conn.login_args == ("alice@example.com", "secret")
    assert len(_FakeIMAP4SSL.instances) == 0


def test_imap_connect_starttls_with_allow_insecure_relaxes_context(monkeypatch):
    """security=starttls + imap_allow_insecure=True → starttls ctx relaxed."""
    _install_imap_fakes(monkeypatch)
    acct = dict(
        ACCT_BASE,
        imap_security="starttls",
        imap_allow_insecure=True,
        imap_port=1143,
    )

    email_mcp._imap_connect(acct)

    assert len(_FakeIMAP4.instances) == 1
    conn = _FakeIMAP4.instances[0]
    assert conn.port == 1143
    assert conn.starttls_called is True
    assert conn.starttls_ctx.check_hostname is False
    assert conn.starttls_ctx.verify_mode == ssl.CERT_NONE


def test_imap_connect_plaintext_constructs_imap4_only(monkeypatch):
    """security='none' → IMAP4 plaintext, no starttls()."""
    _install_imap_fakes(monkeypatch)
    acct = dict(ACCT_BASE, imap_security="none", imap_port=143)

    email_mcp._imap_connect(acct)

    assert len(_FakeIMAP4.instances) == 1
    conn = _FakeIMAP4.instances[0]
    assert conn.host == "imap.example.com"
    assert conn.port == 143
    assert conn.starttls_called is False
    assert conn.login_args == ("alice@example.com", "secret")
    assert len(_FakeIMAP4SSL.instances) == 0


# ---------------------------------------------------------------------------
# Group B — _smtp_connect (4 variants)
# ---------------------------------------------------------------------------


def test_smtp_connect_ssl_uses_smtp_ssl_with_strict_context(monkeypatch):
    """security=ssl → SMTP_SSL with strict context; no plaintext class touched."""
    _install_smtp_fakes(monkeypatch)
    acct = dict(ACCT_BASE, smtp_security="ssl", smtp_port=465)

    email_mcp._smtp_connect(acct)

    assert len(_FakeSMTPSSL.instances) == 1
    conn = _FakeSMTPSSL.instances[0]
    assert conn.host == "smtp.example.com"
    assert conn.port == 465
    assert conn.context.check_hostname is True
    assert conn.context.verify_mode == ssl.CERT_REQUIRED
    assert conn.login_args == ("alice@example.com", "secret")
    assert len(_FakeSMTP.instances) == 0


def test_smtp_connect_ssl_with_allow_insecure_relaxes_context(monkeypatch):
    """security=ssl + smtp_allow_insecure → SMTP_SSL context relaxed."""
    _install_smtp_fakes(monkeypatch)
    acct = dict(ACCT_BASE, smtp_security="ssl", smtp_allow_insecure=True)

    email_mcp._smtp_connect(acct)

    assert len(_FakeSMTPSSL.instances) == 1
    conn = _FakeSMTPSSL.instances[0]
    assert conn.context.check_hostname is False
    assert conn.context.verify_mode == ssl.CERT_NONE


def test_smtp_connect_starttls_default_calls_ehlo_starttls_ehlo(monkeypatch):
    """Default (no smtp_security key) → starttls path: EHLO, STARTTLS, EHLO."""
    _install_smtp_fakes(monkeypatch)
    acct = dict(ACCT_BASE)  # default smtp_security == 'starttls'

    email_mcp._smtp_connect(acct)

    assert len(_FakeSMTP.instances) == 1
    conn = _FakeSMTP.instances[0]
    assert conn.host == "smtp.example.com"
    assert conn.port == 587
    assert conn.ehlo_count == 2  # EHLO before AND after STARTTLS
    assert conn.starttls_called is True
    assert conn.starttls_ctx is not None
    assert conn.starttls_ctx.check_hostname is True
    assert conn.starttls_ctx.verify_mode == ssl.CERT_REQUIRED
    assert conn.login_args == ("alice@example.com", "secret")
    assert len(_FakeSMTPSSL.instances) == 0


def test_smtp_connect_plaintext_calls_ehlo_once_skips_starttls(monkeypatch):
    """security='none' → SMTP plaintext: one EHLO, no STARTTLS."""
    _install_smtp_fakes(monkeypatch)
    acct = dict(ACCT_BASE, smtp_security="none", smtp_port=25)

    email_mcp._smtp_connect(acct)

    assert len(_FakeSMTP.instances) == 1
    conn = _FakeSMTP.instances[0]
    assert conn.port == 25
    assert conn.ehlo_count == 1
    assert conn.starttls_called is False
    assert conn.login_args == ("alice@example.com", "secret")


# ---------------------------------------------------------------------------
# Group C — _caldav_client (3 variants)
# ---------------------------------------------------------------------------


def test_caldav_client_happy_path_constructs_dav_client_with_strict_verify(monkeypatch):
    """Happy path → DAVClient constructed with ssl_verify_cert=True."""
    _install_caldav_fake(monkeypatch)
    acct = dict(ACCT_BASE, caldav_url="https://dav.example.com/caldav/")

    email_mcp._caldav_client(acct)

    assert len(_FakeDAVClient.instances) == 1
    client = _FakeDAVClient.instances[0]
    assert client.url == "https://dav.example.com/caldav/"
    assert client.username == "alice@example.com"
    assert client.password == "secret"
    assert client.ssl_verify_cert is True


def test_caldav_client_with_dav_allow_insecure_disables_verify(monkeypatch):
    """dav_allow_insecure=True → ssl_verify_cert=False."""
    _install_caldav_fake(monkeypatch)
    acct = dict(
        ACCT_BASE,
        caldav_url="https://dav.example.com/caldav/",
        dav_allow_insecure=True,
    )

    email_mcp._caldav_client(acct)

    assert len(_FakeDAVClient.instances) == 1
    client = _FakeDAVClient.instances[0]
    assert client.ssl_verify_cert is False


def test_caldav_client_missing_url_raises_value_error_naming_account(monkeypatch):
    """No caldav_url → ValueError naming the account id, no DAVClient constructed."""
    _install_caldav_fake(monkeypatch)
    acct = dict(ACCT_BASE)  # no caldav_url key

    with pytest.raises(ValueError, match=r"No CalDAV URL configured") as exc:
        email_mcp._caldav_client(acct)

    msg = str(exc.value)
    assert msg.startswith("No CalDAV URL configured for account 'work'.")
    assert "Set caldav_url" in msg
    assert len(_FakeDAVClient.instances) == 0


# ---------------------------------------------------------------------------
# Group D — _carddav_headers (1 variant — no-URL error branch)
# ---------------------------------------------------------------------------


def test_carddav_headers_missing_url_raises_value_error_naming_account():
    """No carddav_url → ValueError naming the account id; no httpx.BasicAuth reached."""
    acct = dict(ACCT_BASE)  # no carddav_url key

    with pytest.raises(ValueError, match=r"No CardDAV URL configured") as exc:
        email_mcp._carddav_headers(acct)

    msg = str(exc.value)
    assert msg.startswith("No CardDAV URL configured for account 'work'.")
    assert "Set carddav_url" in msg
