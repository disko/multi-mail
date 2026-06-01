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


# ---------------------------------------------------------------------------
# Mozilla autoconfig — skip-arms and no-host return-None branch (#8 iter-6)
# ---------------------------------------------------------------------------


def test_parse_mozilla_skips_non_imap_incoming_servers():
    """An <incomingServer type="pop3"> must be skipped (line 981)."""
    xml = (
        '<?xml version="1.0"?>'
        '<clientConfig version="1.1">'
        '<emailProvider id="example.com">'
        '<incomingServer type="pop3">'
        "<hostname>pop.example.com</hostname>"
        "<port>995</port>"
        "<socketType>SSL</socketType>"
        "</incomingServer>"
        '<outgoingServer type="smtp">'
        "<hostname>smtp.example.com</hostname>"
        "<port>587</port>"
        "<socketType>STARTTLS</socketType>"
        "</outgoingServer>"
        "</emailProvider>"
        "</clientConfig>"
    )
    result = email_mcp._parse_mozilla_autoconfig(xml)
    assert result is not None
    assert "imap_host" not in result  # the pop3 entry was skipped
    assert result["smtp_host"] == "smtp.example.com"


def test_parse_mozilla_skips_non_smtp_outgoing_servers():
    """An <outgoingServer type="pop3"> must be skipped (line 999)."""
    xml = (
        '<?xml version="1.0"?>'
        '<clientConfig version="1.1">'
        '<emailProvider id="example.com">'
        '<incomingServer type="imap">'
        "<hostname>imap.example.com</hostname>"
        "<port>993</port>"
        "<socketType>SSL</socketType>"
        "</incomingServer>"
        '<outgoingServer type="pop3">'
        "<hostname>weird.example.com</hostname>"
        "</outgoingServer>"
        "</emailProvider>"
        "</clientConfig>"
    )
    result = email_mcp._parse_mozilla_autoconfig(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    assert "smtp_host" not in result


def test_parse_mozilla_returns_none_when_no_hosts_found():
    """A clientConfig with no incomingServer / outgoingServer → None (line 1019)."""
    xml = (
        '<?xml version="1.0"?>'
        '<clientConfig version="1.1">'
        '<emailProvider id="example.com">'
        "<displayName>Example</displayName>"
        "</emailProvider>"
        "</clientConfig>"
    )
    assert email_mcp._parse_mozilla_autoconfig(xml) is None


# ---------------------------------------------------------------------------
# Microsoft autodiscover — security-branches + no-port + no-protocol (#8 iter-6)
# ---------------------------------------------------------------------------


def test_parse_microsoft_encryption_ssl_maps_to_ssl():
    """An <Encryption>SSL</Encryption> tag maps to ``"ssl"`` (line 1090)."""
    xml = (
        '<?xml version="1.0"?>'
        "<Autodiscover>"
        "<Response><Account>"
        "<Protocol>"
        "<Type>IMAP</Type>"
        "<Server>imap.example.com</Server>"
        "<Port>993</Port>"
        "<Encryption>SSL</Encryption>"
        "</Protocol>"
        "</Account></Response>"
        "</Autodiscover>"
    )
    result = email_mcp._parse_microsoft_autodiscover(xml)
    assert result is not None
    assert result["imap_security"] == "ssl"


def test_parse_microsoft_no_encryption_defaults_to_starttls():
    """No <SSL> and no <Encryption> → fall-through default 'starttls' (line 1096)."""
    xml = (
        '<?xml version="1.0"?>'
        "<Autodiscover>"
        "<Response><Account>"
        "<Protocol>"
        "<Type>SMTP</Type>"
        "<Server>smtp.example.com</Server>"
        "<Port>587</Port>"
        "</Protocol>"
        "</Account></Response>"
        "</Autodiscover>"
    )
    result = email_mcp._parse_microsoft_autodiscover(xml)
    assert result is not None
    assert result["smtp_security"] == "starttls"


def test_parse_microsoft_omits_port_when_missing():
    """No <Port> → port key is not set (partials 1100->1102, 1105->1107)."""
    xml = (
        '<?xml version="1.0"?>'
        "<Autodiscover>"
        "<Response><Account>"
        "<Protocol>"
        "<Type>IMAP</Type>"
        "<Server>imap.example.com</Server>"
        "<Encryption>SSL</Encryption>"
        "</Protocol>"
        "<Protocol>"
        "<Type>SMTP</Type>"
        "<Server>smtp.example.com</Server>"
        "<Encryption>STARTTLS</Encryption>"
        "</Protocol>"
        "</Account></Response>"
        "</Autodiscover>"
    )
    result = email_mcp._parse_microsoft_autodiscover(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    assert "imap_port" not in result
    assert result["smtp_host"] == "smtp.example.com"
    assert "smtp_port" not in result


def test_parse_microsoft_returns_none_when_no_protocols():
    """An Autodiscover envelope with no <Protocol> elements → None (line 1110)."""
    xml = (
        '<?xml version="1.0"?>'
        "<Autodiscover>"
        "<Response><Account>"
        "</Account></Response>"
        "</Autodiscover>"
    )
    assert email_mcp._parse_microsoft_autodiscover(xml) is None


# ---------------------------------------------------------------------------
# Mozilla autoconfig — empty-element + missing-emailProvider (#8 iter-7)
#
# Each ``if x is not None and x.text:`` arm short-circuits when ``x.text`` is
# None or empty. An XML element like ``<hostname/>`` parses to a node with
# ``.text = None``; six partials (986->988, 988->990, 990->992, 1003->1005,
# 1005->1007, 1007->1009) all reduce to "the element exists but the text
# guard is false". The full doc with empty incoming AND outgoing elements
# exercises every one in a single parse.
# ---------------------------------------------------------------------------


def test_parse_mozilla_skips_elements_with_empty_text():
    """All six element-text guards trip false: hostname/port/socketType/
    username in incoming, hostname/port/socketType in outgoing. No host
    keys are set so the parser returns None (line 1018-1019).
    Pins partials 986->988, 988->990, 990->992, 1003->1005, 1005->1007,
    1007->1009."""
    xml = (
        '<?xml version="1.0"?>'
        '<clientConfig version="1.1">'
        '<emailProvider id="example.com">'
        '<incomingServer type="imap">'
        "<hostname></hostname><port></port>"
        "<socketType></socketType><username></username>"
        "</incomingServer>"
        '<outgoingServer type="smtp">'
        "<hostname></hostname><port></port><socketType></socketType>"
        "</outgoingServer>"
        "</emailProvider>"
        "</clientConfig>"
    )
    # Neither imap_host nor smtp_host was populated → return None.
    assert email_mcp._parse_mozilla_autoconfig(xml) is None


def test_parse_mozilla_skips_provider_block_when_absent():
    """No <emailProvider> in the doc → the displayName lookup branch is
    skipped. Hosts still populate from root-level incoming/outgoing servers.
    Pins partial 1013->1018."""
    xml = (
        '<?xml version="1.0"?>'
        '<clientConfig version="1.1">'
        '<incomingServer type="imap">'
        "<hostname>imap.example.com</hostname>"
        "<port>993</port>"
        "<socketType>SSL</socketType>"
        "</incomingServer>"
        "</clientConfig>"
    )
    result = email_mcp._parse_mozilla_autoconfig(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    assert "provider_name" not in result


# ---------------------------------------------------------------------------
# Microsoft autodiscover — SMTP Protocol with no Server (#8 iter-7)
# ---------------------------------------------------------------------------


def test_parse_microsoft_smtp_protocol_without_server_skips_assignment():
    """A <Protocol> block with Type=SMTP but no <Server> → the ``elif`` arm
    short-circuits (server is falsy), the loop continues to the next
    <Protocol>. Pins partial 1103->1081."""
    xml = (
        '<?xml version="1.0"?>'
        "<Autodiscover>"
        "<Response><Account>"
        "<Protocol>"
        "<Type>SMTP</Type>"
        "<Port>587</Port>"
        "<Encryption>STARTTLS</Encryption>"
        "</Protocol>"
        "<Protocol>"
        "<Type>IMAP</Type>"
        "<Server>imap.example.com</Server>"
        "<Port>993</Port>"
        "<Encryption>SSL</Encryption>"
        "</Protocol>"
        "</Account></Response>"
        "</Autodiscover>"
    )
    result = email_mcp._parse_microsoft_autodiscover(xml)
    assert result is not None
    assert result["imap_host"] == "imap.example.com"
    # SMTP block had no Server element → it was skipped.
    assert "smtp_host" not in result
