"""Tests for ``resolve_dav_url`` — host pinning for CardDAV/CalDAV requests.

Prevents a compromised (or MITM'd) DAV server from returning attacker-controlled
``<href>`` elements that would cause the client to send authenticated requests
to a different host. The SSRF guard does not catch this because the attacker
host is a normal public IP.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "_security", ROOT / "servers" / "_security.py"
)
_security = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(_security)
resolve_dav_url = _security.resolve_dav_url


BASE = "https://dav.example.com/remote.php/dav/addressbooks/users/u/"


def test_relative_href_resolves_against_base():
    assert resolve_dav_url(BASE, "book/contact.vcf") == BASE + "book/contact.vcf"


def test_absolute_path_resolves_against_base_host():
    assert (
        resolve_dav_url(BASE, "/remote.php/dav/x.vcf")
        == "https://dav.example.com/remote.php/dav/x.vcf"
    )


def test_same_host_absolute_url_passes():
    target = "https://dav.example.com/anywhere.vcf"
    assert resolve_dav_url(BASE, target) == target


def test_cross_host_absolute_url_rejected():
    with pytest.raises(httpx.RequestError, match="different host"):
        resolve_dav_url(BASE, "https://evil.example.org/steal.vcf")


def test_scheme_relative_cross_host_rejected():
    """A scheme-relative href // attacker takes over the scheme but stays a URL."""
    with pytest.raises(httpx.RequestError, match="different host"):
        resolve_dav_url(BASE, "//evil.example.org/x.vcf")


def test_http_to_other_host_rejected():
    with pytest.raises(httpx.RequestError, match="different host"):
        resolve_dav_url(BASE, "http://evil.example.org/x")


def test_host_match_is_case_insensitive():
    target = "https://DAV.example.com/x.vcf"
    assert resolve_dav_url(BASE, target).startswith("https://DAV.example.com")


def test_empty_base_raises():
    with pytest.raises(ValueError):
        resolve_dav_url("", "/x.vcf")


def test_unparseable_base_raises():
    with pytest.raises(ValueError):
        resolve_dav_url("not-a-url", "/x.vcf")
