"""Security helpers for multi-mail.

Provides:
- _is_safe_host(): blocks loopback, private, link-local, multicast, reserved,
  and metadata-service IPs to prevent SSRF via attacker-influenceable hostnames
  in autodiscovery and DAV URLs.
- safe_async_client(): factory for an httpx.AsyncClient that verifies TLS by
  default and re-runs the SSRF check on every redirect hop.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "metadata.google.internal",
        "metadata.aws.com",
        "fd00:ec2::254",
    }
)

_ALLOW_PRIVATE = os.environ.get("MULTI_MAIL_ALLOW_PRIVATE_AUTODISCOVER") == "1"


def _is_unsafe_ip(ip) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _is_safe_host(host: str) -> Tuple[bool, str]:
    """Resolve *host* and return (ok, reason).

    Honors the MULTI_MAIL_ALLOW_PRIVATE_AUTODISCOVER=1 escape hatch (lab use only).
    """
    if not host:
        return False, "empty host"

    host_l = host.lower().strip(".")
    if host_l in _METADATA_HOSTS:
        return False, f"blocked metadata host: {host_l}"

    if _ALLOW_PRIVATE:
        return True, "MULTI_MAIL_ALLOW_PRIVATE_AUTODISCOVER=1"

    try:
        ip = ipaddress.ip_address(host_l)
        if _is_unsafe_ip(ip):
            return False, f"blocked address range: {ip}"
        return True, "literal-ip ok"
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host_l, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        return False, f"DNS resolution failed: {e}"

    for _family, _, _, _, sockaddr in infos:
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_unsafe_ip(ip):
            return False, f"resolves to blocked address: {addr}"

    return True, "ok"


def _check_url_safe(url: str) -> None:
    """Raise httpx.RequestError if the URL host fails the SSRF check."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    ok, reason = _is_safe_host(host)
    if not ok:
        raise httpx.RequestError(f"refused by SSRF guard ({reason}): {url}")


def resolve_dav_url(account_base: str, target: str) -> str:
    """Resolve *target* relative to *account_base* and pin it to the same host.

    DAV servers return ``<href>`` elements in PROPFIND/REPORT responses that the
    client uses to build follow-up authenticated requests. A compromised (or
    MITM'd) server could return a cross-origin href and trick the client into
    sending HTTP Basic credentials to an attacker. The SSRF guard does not stop
    this because the attacker host is a normal public IP.

    Pin the resolved URL to the host of the configured account base. Reject
    cross-origin redirects.
    """
    if not account_base:
        raise ValueError("DAV account URL is not configured")
    base_host = urlparse(account_base).hostname
    if not base_host:
        raise ValueError(f"DAV account URL has no host: {account_base!r}")

    from urllib.parse import urljoin

    full = (
        target
        if target.startswith(("http://", "https://"))
        else urljoin(account_base, target)
    )
    final_host = urlparse(full).hostname
    if not final_host or final_host.lower() != base_host.lower():
        raise httpx.RequestError(
            f"refused: DAV server returned href on a different host "
            f"({final_host!r} != configured {base_host!r}): {full}"
        )
    return full


async def _enforce_ssrf_on_request(request: httpx.Request) -> None:
    """httpx event hook — runs on every request, including each redirect hop."""
    _check_url_safe(str(request.url))


def safe_async_client(
    *,
    timeout: float = 30.0,
    follow_redirects: bool = True,
    auth: Optional[httpx.Auth] = None,
    verify: bool = True,
) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient with TLS verification on and SSRF enforcement.

    The SSRF guard runs on the initial URL and on every redirect hop via the
    request event hook, so a 302 pointing at 127.0.0.1 is blocked before the
    body is sent.
    """
    return httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        auth=auth,
        verify=verify,
        event_hooks={"request": [_enforce_ssrf_on_request]},
    )
