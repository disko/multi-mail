"""Tests for ``_autodiscover`` — the orchestrator that fans out to
Mozilla autoconfig, Microsoft Autodiscover, DNS SRV, and ``.well-known`` DAV
discovery, then merges the results.

The four ``_try_*`` discovery sources are monkeypatched with async coroutines
that return canned dicts (or ``None`` / raise) so we can drive every merge,
priority, and template-expansion branch without going on-network.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)


def _stub_sources(monkeypatch, *, mozilla=None, microsoft=None, dns=None, dav=None):
    """Replace each _try_* with an async function returning the given value
    (or raising, if value is an Exception instance)."""

    def _factory(value):
        async def _fn(*args, **kwargs):
            if isinstance(value, Exception):
                raise value
            return value
        return _fn

    monkeypatch.setattr(email_mcp, "_try_mozilla_autoconfig", _factory(mozilla))
    monkeypatch.setattr(email_mcp, "_try_microsoft_autodiscover", _factory(microsoft))
    monkeypatch.setattr(email_mcp, "_try_dns_srv", _factory(dns))
    monkeypatch.setattr(email_mcp, "_try_wellknown_dav", _factory(dav))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Result merging
# ---------------------------------------------------------------------------

def test_mozilla_result_wins_over_microsoft_when_both_present(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.mozilla.example",
            "imap_port": 993,
            "imap_security": "ssl",
            "smtp_host": "smtp.mozilla.example",
            "smtp_port": 587,
            "smtp_security": "starttls",
        },
        microsoft={
            "source": "microsoft",
            "imap_host": "imap.microsoft.example",
            "imap_port": 993,
            "imap_security": "ssl",
        },
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert out["imap_host"] == "imap.mozilla.example"
    assert "mozilla" in out["sources"]
    assert "microsoft" in out["sources"]


def test_later_source_fills_gaps_left_by_earlier(monkeypatch):
    """Mozilla provides IMAP only; .well-known fills in DAV URLs."""
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        dav={
            "source": "well-known-dav",
            "caldav_url": "https://dav.example.com/caldav/",
            "carddav_url": "https://dav.example.com/carddav/",
        },
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert out["imap_host"] == "imap.example.com"
    assert out["caldav_url"] == "https://dav.example.com/caldav/"
    assert out["carddav_url"] == "https://dav.example.com/carddav/"


def test_dns_provides_smtp_when_mozilla_did_not(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        dns={
            "source": "dns-srv",
            "smtp_host": "submission.example.com",
            "smtp_port": 587,
            "smtp_security": "starttls",
        },
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert out["smtp_host"] == "submission.example.com"
    assert out["smtp_port"] == 587


def test_all_sources_none_returns_error_blob(monkeypatch):
    _stub_sources(monkeypatch)
    out = run(email_mcp._autodiscover("alice@unknown.example.com"))
    assert "error" in out
    assert "No autodiscovery results" in out["error"]
    assert "unknown.example.com" in out["error"]


def test_exception_from_one_source_is_swallowed(monkeypatch):
    """If one method raises (e.g. DNS server timeout), the orchestrator must
    still return results from the other methods rather than blowing up."""
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
        },
        microsoft=RuntimeError("boom"),
        dns=TimeoutError("dns slow"),
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert out["imap_host"] == "imap.example.com"
    assert "mozilla" in out["sources"]
    # Failed sources do NOT show up in sources[]
    assert "microsoft" not in out["sources"]


# ---------------------------------------------------------------------------
# username_template expansion
# ---------------------------------------------------------------------------

def test_username_template_emailaddress_placeholder(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
            "username_template": "%EMAILADDRESS%",
        },
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert out["suggested_username"] == "alice@example.com"
    assert "username_template" not in out  # consumed


def test_username_template_localpart_placeholder(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
            "username_template": "%EMAILLOCALPART%",
        },
    )
    out = run(email_mcp._autodiscover("alice+work@example.com"))
    assert out["suggested_username"] == "alice+work"


def test_username_template_domain_placeholder(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
            "username_template": "%EMAILLOCALPART%@%EMAILDOMAIN%",
        },
    )
    out = run(email_mcp._autodiscover("Alice@Example.com"))
    # _domain_from_email lower-cases; localpart preserved as-is
    assert out["suggested_username"] == "Alice@example.com"


def test_username_template_omitted_when_not_provided(monkeypatch):
    _stub_sources(monkeypatch,
        mozilla={
            "source": "mozilla",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_security": "ssl",
        },
    )
    out = run(email_mcp._autodiscover("alice@example.com"))
    assert "suggested_username" not in out


# ---------------------------------------------------------------------------
# email_autodiscover (the public MCP tool) — markdown formatter coverage
# ---------------------------------------------------------------------------

def _stub_autodiscover(monkeypatch, value):
    """Replace the orchestrator `_autodiscover` with an async function
    returning the given canned dict. The public `email_autodiscover` tool
    body wraps this seam in markdown — we drive only the formatter."""
    async def _fn(_email_addr):
        return value
    monkeypatch.setattr(email_mcp, "_autodiscover", _fn)


def test_autodiscover_tool_error_returns_error_string(monkeypatch):
    err = "No autodiscovery results for unknown.example.com. Try email_add_account directly."
    _stub_autodiscover(monkeypatch, {"error": err})
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="user@unknown.example.com")
    ))
    # Short-circuits before formatting — no markdown header, just the raw error.
    assert out == err
    assert "# Autodiscovery Results" not in out


def test_autodiscover_tool_renders_full_imap_smtp_caldav_carddav_markdown(monkeypatch):
    _stub_autodiscover(monkeypatch, {
        "sources": ["mozilla", "well-known-dav"],
        "provider_name": "Example Mail",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "caldav_url": "https://dav.example.com/caldav/",
        "carddav_url": "https://dav.example.com/carddav/",
        "suggested_username": "alice",
    })
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="alice@example.com")
    ))
    assert "# Autodiscovery Results for alice@example.com" in out
    assert "Domain: `example.com`" in out
    assert "mozilla, well-known-dav" in out
    assert "**Provider**: Example Mail" in out
    assert "## IMAP (Incoming)" in out
    assert "imap.example.com" in out
    assert "993" in out
    assert "ssl" in out
    assert "## SMTP (Outgoing)" in out
    assert "smtp.example.com" in out
    assert "587" in out
    assert "starttls" in out
    assert "## CalDAV (Calendar)" in out
    assert "https://dav.example.com/caldav/" in out
    assert "## CardDAV (Contacts)" in out
    assert "https://dav.example.com/carddav/" in out
    assert "**Suggested username**: `alice`" in out
    assert "(default)" not in out
    assert "email_add_account" in out


def test_autodiscover_tool_omits_provider_when_absent(monkeypatch):
    _stub_autodiscover(monkeypatch, {
        "sources": ["mozilla"],
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
    })
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="alice@example.com")
    ))
    assert "**Provider**" not in out
    assert "## IMAP (Incoming)" in out


def test_autodiscover_tool_default_ports_and_security_when_not_provided(monkeypatch):
    """When the orchestrator omits port/security, the formatter falls back
    to the IMAP=993/ssl and SMTP=587/starttls defaults."""
    _stub_autodiscover(monkeypatch, {
        "sources": ["mozilla"],
        "imap_host": "imap.example.com",
        "smtp_host": "smtp.example.com",
    })
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="alice@example.com")
    ))
    assert "993" in out
    assert "ssl" in out
    assert "587" in out
    assert "starttls" in out


def test_autodiscover_tool_uses_default_username_when_no_template_suggested(monkeypatch):
    _stub_autodiscover(monkeypatch, {
        "sources": ["mozilla"],
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
    })
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="bob@example.org")
    ))
    assert "**Suggested username**: `bob@example.org` (default)" in out


def test_autodiscover_tool_omits_dav_and_smtp_blocks_when_only_imap_discovered(monkeypatch):
    _stub_autodiscover(monkeypatch, {
        "sources": ["mozilla"],
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
    })
    out = run(email_mcp.email_autodiscover(
        email_mcp.AutodiscoverInput(email_address="alice@example.com")
    ))
    assert "## IMAP (Incoming)" in out
    assert "## SMTP" not in out
    assert "## CalDAV" not in out
    assert "## CardDAV" not in out
    assert "(default)" in out
