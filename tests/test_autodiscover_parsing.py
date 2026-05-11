"""Tests for Mozilla autoconfig + Microsoft autodiscover XML parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

import email_mcp

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_mozilla_autoconfig_extracts_imap_and_smtp():
    xml = (FIXTURES / "mozilla_autoconfig.xml").read_text()
    result = email_mcp._parse_mozilla_autoconfig(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    assert result["imap_port"] == 993
    assert result["imap_security"] == "ssl"
    assert result["smtp_host"] == "smtp.example.com"
    assert result["smtp_port"] == 587
    assert result["smtp_security"] == "starttls"
    assert result["provider_name"] == "Example Mail"
    assert result["username_template"] == "%EMAILADDRESS%"


def test_parse_microsoft_autodiscover_extracts_imap_and_smtp():
    xml = (FIXTURES / "microsoft_autodiscover.xml").read_text()
    result = email_mcp._parse_microsoft_autodiscover(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    assert result["imap_port"] == 993
    assert result["imap_security"] == "ssl"
    assert result["smtp_host"] == "smtp.example.com"
    assert result["smtp_port"] == 587
    assert result["smtp_security"] == "starttls"


def test_billion_laughs_rejected_by_defusedxml():
    """defusedxml.ElementTree.fromstring must refuse the entity-bomb DTD."""
    from defusedxml import EntitiesForbidden
    xml = (FIXTURES / "billion_laughs.xml").read_text()
    with pytest.raises(EntitiesForbidden):
        email_mcp._parse_mozilla_autoconfig(xml)


def test_malformed_xml_returns_none_not_exception():
    assert email_mcp._parse_mozilla_autoconfig("<<not valid xml") is None
    assert email_mcp._parse_microsoft_autodiscover("<<not valid xml") is None
