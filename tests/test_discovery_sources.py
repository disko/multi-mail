"""Tests for the four autodiscovery sources:

- ``_try_mozilla_autoconfig``
- ``_try_microsoft_autodiscover``
- ``_try_wellknown_dav``
- ``_try_dns_srv``

The three HTTP-based sources are exercised through a ``_FakeAsyncClient`` that
substitutes for ``safe_async_client`` and serves canned responses keyed by URL.
``_try_dns_srv`` shells out to ``dig``; we monkeypatch
``asyncio.create_subprocess_exec`` with a fake process. (That asyncio API is
Python's execFile-equivalent — no shell, no injection risk.)
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)


class _FakeResp:
    def __init__(self, status_code=200, text="", url=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.url = url
        self.headers = headers or {}


class _FakeAsyncClient:
    """Substitute for ``safe_async_client``. Looks up canned responses by URL."""

    def __init__(self, *, get=None, post=None, request=None, raise_on=None):
        self._get = get or {}
        self._post = post or {}
        self._request = request or {}
        self._raise_on = raise_on or set()
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        if url in self._raise_on:
            raise RuntimeError("network down")
        if url not in self._get:
            return _FakeResp(status_code=404, url=url)
        return self._get[url]

    async def post(self, url, content=None, headers=None, **kwargs):
        self.calls.append(("POST", url))
        if url in self._raise_on:
            raise RuntimeError("network down")
        if url not in self._post:
            return _FakeResp(status_code=404, url=url)
        return self._post[url]

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url in self._raise_on:
            raise RuntimeError("network down")
        key = (method, url)
        if key not in self._request:
            return _FakeResp(status_code=404, url=url)
        return self._request[key]


def _install_client(monkeypatch, client):
    def factory(**kwargs):
        return client

    monkeypatch.setattr(email_mcp, "safe_async_client", factory)


def run(coro):
    return asyncio.run(coro)


MOZILLA_XML = """<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="example.com">
    <incomingServer type="imap">
      <hostname>imap.example.com</hostname>
      <port>993</port>
      <socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username>
    </incomingServer>
    <outgoingServer type="smtp">
      <hostname>smtp.example.com</hostname>
      <port>587</port>
      <socketType>STARTTLS</socketType>
    </outgoingServer>
  </emailProvider>
</clientConfig>
"""


def test_mozilla_autoconfig_primary_url(monkeypatch):
    """Primary URL (autoconfig.{domain}/mail/config-v1.1.xml) hits 200 first."""
    client = _FakeAsyncClient(
        get={
            "https://autoconfig.example.com/mail/config-v1.1.xml": _FakeResp(
                status_code=200, text=MOZILLA_XML
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_mozilla_autoconfig("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert out["smtp_host"] == "smtp.example.com"
    assert out["source"] == "mozilla-autoconfig"
    assert len(client.calls) == 1


def test_mozilla_autoconfig_falls_back_to_well_known(monkeypatch):
    client = _FakeAsyncClient(
        get={
            "https://example.com/.well-known/autoconfig/mail/config-v1.1.xml": _FakeResp(
                status_code=200, text=MOZILLA_XML
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_mozilla_autoconfig("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert len(client.calls) == 2


def test_mozilla_autoconfig_both_404_returns_none(monkeypatch):
    _install_client(monkeypatch, _FakeAsyncClient(get={}))
    assert run(email_mcp._try_mozilla_autoconfig("nowhere.example")) is None


def test_mozilla_autoconfig_swallows_network_errors(monkeypatch):
    """A network exception on one URL should not crash; try the next."""
    client = _FakeAsyncClient(
        get={
            "https://example.com/.well-known/autoconfig/mail/config-v1.1.xml": _FakeResp(
                status_code=200, text=MOZILLA_XML
            ),
        },
        raise_on={"https://autoconfig.example.com/mail/config-v1.1.xml"},
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_mozilla_autoconfig("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"


MICROSOFT_XML = """<?xml version="1.0"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
  <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
    <Account>
      <Protocol>
        <Type>IMAP</Type>
        <Server>imap.example.com</Server>
        <Port>993</Port>
        <SSL>on</SSL>
      </Protocol>
      <Protocol>
        <Type>SMTP</Type>
        <Server>smtp.example.com</Server>
        <Port>587</Port>
        <Encryption>STARTTLS</Encryption>
      </Protocol>
    </Account>
  </Response>
</Autodiscover>
"""


def test_microsoft_autodiscover_primary_subdomain(monkeypatch):
    client = _FakeAsyncClient(
        post={
            "https://autodiscover.example.com/autodiscover/autodiscover.xml": _FakeResp(
                status_code=200, text=MICROSOFT_XML
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_microsoft_autodiscover("example.com", "alice@example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert out["smtp_host"] == "smtp.example.com"
    assert out["source"] == "microsoft-autodiscover"


def test_microsoft_autodiscover_falls_back_to_root_domain(monkeypatch):
    client = _FakeAsyncClient(
        post={
            "https://example.com/autodiscover/autodiscover.xml": _FakeResp(
                status_code=200, text=MICROSOFT_XML
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_microsoft_autodiscover("example.com", "alice@example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert len(client.calls) == 2


def test_microsoft_autodiscover_returns_none_when_nothing_responds(monkeypatch):
    _install_client(monkeypatch, _FakeAsyncClient(post={}))
    assert (
        run(
            email_mcp._try_microsoft_autodiscover(
                "nowhere.example", "x@nowhere.example"
            )
        )
        is None
    )


def test_wellknown_dav_207_response_captures_urls(monkeypatch):
    """A 207 (Multi-Status) response means a real DAV endpoint responded."""
    caldav_url = "https://example.com/.well-known/caldav"
    carddav_url = "https://example.com/.well-known/carddav"
    client = _FakeAsyncClient(
        request={
            ("PROPFIND", caldav_url): _FakeResp(status_code=207, url=caldav_url),
            ("PROPFIND", carddav_url): _FakeResp(status_code=207, url=carddav_url),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["caldav_url"] == caldav_url
    assert out["carddav_url"] == carddav_url
    assert out["source"] == "well-known-dav"


def test_wellknown_dav_no_endpoint_returns_none(monkeypatch):
    _install_client(monkeypatch, _FakeAsyncClient(request={}))
    assert run(email_mcp._try_wellknown_dav("example.com")) is None


def test_wellknown_dav_partial_discovery_caldav_only(monkeypatch):
    """If only caldav is available, the result has just caldav_url."""
    caldav_url = "https://example.com/.well-known/caldav"
    client = _FakeAsyncClient(
        request={
            ("PROPFIND", caldav_url): _FakeResp(status_code=207, url=caldav_url),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["caldav_url"] == caldav_url
    assert "carddav_url" not in out


class _FakeProc:
    def __init__(self, stdout_text):
        self._stdout = stdout_text.encode()

    async def communicate(self):
        return (self._stdout, b"")


def _install_dig(monkeypatch, srv_table):
    """Patch asyncio.create_subprocess_exec to look up SRV responses by name."""

    async def _fake_run(*args, stdout=None, stderr=None):
        srv_name = args[-1]
        return _FakeProc(srv_table.get(srv_name, ""))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_run)
    import socket as _socket

    def _no_addr(*args, **kwargs):
        raise _socket.gaierror("no")

    monkeypatch.setattr(_socket, "getaddrinfo", _no_addr)


def test_dns_srv_prefers_imaps_over_imap(monkeypatch):
    """When both _imaps and _imap return SRV records, prefer _imaps (SSL)."""
    _install_dig(
        monkeypatch,
        {
            "_imaps._tcp.example.com": "0 1 993 imap.example.com.",
            "_imap._tcp.example.com": "0 1 143 imap.example.com.",
            "_submissions._tcp.example.com": "0 1 465 smtp.example.com.",
            "_submission._tcp.example.com": "0 1 587 smtp.example.com.",
        },
    )
    out = run(email_mcp._try_dns_srv("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert out["imap_port"] == 993
    assert out["imap_security"] == "ssl"
    assert out["smtp_port"] == 465
    assert out["smtp_security"] == "ssl"


def test_dns_srv_falls_back_to_starttls_when_ssl_record_absent(monkeypatch):
    _install_dig(
        monkeypatch,
        {
            "_imap._tcp.example.com": "0 1 143 imap.example.com.",
            "_submission._tcp.example.com": "0 1 587 smtp.example.com.",
        },
    )
    out = run(email_mcp._try_dns_srv("example.com"))
    assert out is not None
    assert out["imap_port"] == 143
    assert out["imap_security"] == "starttls"
    assert out["smtp_port"] == 587
    assert out["smtp_security"] == "starttls"


def test_dns_srv_returns_none_when_no_records(monkeypatch):
    _install_dig(monkeypatch, {})
    assert run(email_mcp._try_dns_srv("nowhere.example")) is None


def test_dns_srv_skips_record_pointing_at_root_dot(monkeypatch):
    """A SRV record with target '.' means 'service not available' (RFC 2782)."""
    _install_dig(
        monkeypatch,
        {
            "_imaps._tcp.example.com": "0 0 0 .",
            "_imap._tcp.example.com": "0 1 143 imap.example.com.",
        },
    )
    out = run(email_mcp._try_dns_srv("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    assert out["imap_port"] == 143


# ---------------------------------------------------------------------------
# Additional discovery branches — issue #8 iter-6 mop-up
# ---------------------------------------------------------------------------


def test_microsoft_autodiscover_post_exception_continues_to_next_url(monkeypatch):
    """A POST exception on the first URL must not stop the loop — the second
    URL still gets tried. Pins lines 1054-1055."""
    primary = "https://autodiscover.example.com/autodiscover/autodiscover.xml"
    fallback = "https://example.com/autodiscover/autodiscover.xml"
    client = _FakeAsyncClient(
        post={fallback: _FakeResp(status_code=200, text=MICROSOFT_XML)},
        raise_on={primary},
    )
    _install_client(monkeypatch, client)
    out = run(
        email_mcp._try_microsoft_autodiscover(
            "example.com",
            "alice@example.com",
        )
    )
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    # Both URLs were attempted.
    assert ("POST", primary) in client.calls
    assert ("POST", fallback) in client.calls


def test_microsoft_autodiscover_skips_unparseable_response(monkeypatch):
    """POST returns 200 with junk XML → parser returns None → continue to next URL.

    Pins partial branch 1052->1046 (the `if result:` false arm).
    """
    primary = "https://autodiscover.example.com/autodiscover/autodiscover.xml"
    fallback = "https://example.com/autodiscover/autodiscover.xml"
    client = _FakeAsyncClient(
        post={
            primary: _FakeResp(status_code=200, text="<<not xml"),
            fallback: _FakeResp(status_code=200, text=MICROSOFT_XML),
        }
    )
    _install_client(monkeypatch, client)
    out = run(
        email_mcp._try_microsoft_autodiscover(
            "example.com",
            "alice@example.com",
        )
    )
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    # Continued past the unparseable primary.
    assert ("POST", primary) in client.calls
    assert ("POST", fallback) in client.calls


def test_wellknown_dav_get_fallback_after_propfind_404(monkeypatch):
    """PROPFIND returns 404 → GET fallback runs. If GET returns 200 on a
    different URL, that URL is captured as the dav root. Pins the GET
    fallback branch in `_try_wellknown_dav`."""
    caldav_path = "https://example.com/.well-known/caldav"
    real_dav = "https://example.com/dav/caldav/"
    client = _FakeAsyncClient(
        request={
            # PROPFIND for caldav returns 404; carddav not configured.
        },
        get={caldav_path: _FakeResp(status_code=200, url=real_dav)},
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["caldav_url"] == real_dav


def test_wellknown_dav_propfind_redirect_uses_location_header(monkeypatch):
    """A 301/302 from PROPFIND with a Location header captures that URL.
    Pins lines 1188-1191."""
    caldav_path = "https://example.com/.well-known/caldav"
    redirected = "https://dav.example.com/cal/"
    client = _FakeAsyncClient(
        request={
            ("PROPFIND", caldav_path): _FakeResp(
                status_code=301,
                url=caldav_path,
                headers={"location": redirected},
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["caldav_url"] == redirected


# ---------------------------------------------------------------------------
# Final coverage gaps — DNS SRV malformed lines, subprocess failure,
# well-known DAV PROPFIND/GET exception swallows, and redirect-skip
# (#8 iter-7).
# ---------------------------------------------------------------------------


def test_dns_srv_skips_dig_output_with_fewer_than_four_fields(monkeypatch):
    """dig returns a line with only 3 fields → ``len(parts) >= 4`` is False,
    loop continues without populating. The fallback variant (port 143)
    still wins. Pins partial 1156->1134."""
    _install_dig(
        monkeypatch,
        {
            "_imaps._tcp.example.com": "0 1 993",  # missing target field
            "_imap._tcp.example.com": "0 1 143 imap.example.com.",
        },
    )
    out = run(email_mcp._try_dns_srv("example.com"))
    assert out is not None
    assert out["imap_host"] == "imap.example.com"
    # The malformed _imaps line was discarded; we fell through to _imap.
    assert out["imap_port"] == 143
    assert out["imap_security"] == "starttls"


def test_dns_srv_swallows_subprocess_exception(monkeypatch):
    """``asyncio.create_subprocess_exec`` itself raises → except: continue.
    Pins lines 1163-1164."""

    async def _boom(*args, **kwargs):
        raise OSError("dig binary missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    import socket as _socket

    def _gaierror(*args, **kwargs):
        raise _socket.gaierror("nx")

    monkeypatch.setattr(_socket, "getaddrinfo", _gaierror)

    # All four SRV queries fail → no host populated → returns None.
    assert run(email_mcp._try_dns_srv("nowhere.example")) is None


def test_wellknown_dav_propfind_exception_skips_get_fallback(monkeypatch):
    """PROPFIND for the caldav URL raises → except: continue skips both the
    success arms AND the GET fallback for that URL. The carddav URL still
    succeeds. Pins lines 1192-1193."""
    caldav_url = "https://example.com/.well-known/caldav"
    carddav_url = "https://example.com/.well-known/carddav"
    client = _FakeAsyncClient(
        request={
            ("PROPFIND", carddav_url): _FakeResp(status_code=207, url=carddav_url),
        },
        raise_on={caldav_url},
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["carddav_url"] == carddav_url
    assert "caldav_url" not in out
    # The GET fallback for caldav was NOT attempted (the except skipped it).
    assert ("GET", caldav_url) not in client.calls


def test_wellknown_dav_get_fallback_exception_continues(monkeypatch):
    """PROPFIND returns 404 (no key populated) → falls into the GET
    fallback try; the GET call raises → except: continue. No key set on
    either URL, function returns None. Pins lines 1201-1202."""
    caldav_url = "https://example.com/.well-known/caldav"
    carddav_url = "https://example.com/.well-known/carddav"

    # We need PROPFIND to return 404 (NOT raise) and GET to raise. The
    # shared ``raise_on`` set keys by URL only and is checked in both
    # methods, so a subclass narrows the raise to GET only.
    class _GetOnlyRaisingClient(_FakeAsyncClient):
        async def get(self, url, **kwargs):
            self.calls.append(("GET", url))
            raise RuntimeError("get-side network down")

    client = _GetOnlyRaisingClient(request={})  # PROPFIND → 404 default
    _install_client(monkeypatch, client)

    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is None
    # GET fallback was attempted for both URLs (PROPFIND returned 404 → no
    # key set → into the GET branch → GET raised).
    assert ("GET", caldav_url) in client.calls
    assert ("GET", carddav_url) in client.calls


def test_wellknown_dav_redirect_with_location_skips_get_fallback(monkeypatch):
    """A 302 response with a ``Location`` header populates the URL key →
    the post-except `if key not in result:` arm is False → GET fallback is
    skipped for that URL."""
    caldav_url = "https://example.com/.well-known/caldav"
    redirected = "https://dav.example.com/cal/"
    client = _FakeAsyncClient(
        request={
            ("PROPFIND", caldav_url): _FakeResp(
                status_code=302,
                url=caldav_url,
                headers={"location": redirected},
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    assert out is not None
    assert out["caldav_url"] == redirected
    # GET was NOT called for caldav (PROPFIND already populated the key).
    assert ("GET", caldav_url) not in client.calls


def test_wellknown_dav_redirect_without_location_falls_to_get(monkeypatch):
    """A 301/302 PROPFIND response WITHOUT a ``Location`` header → the
    `if loc:` arm is False → key stays unset → GET fallback fires. Pins
    partial 1190->1194 (the `if loc:` false branch jumping past the
    populate-result line through the except into the GET-fallback check).
    """
    caldav_url = "https://example.com/.well-known/caldav"
    client = _FakeAsyncClient(
        request={
            # 301 with no `location` header in the response.
            ("PROPFIND", caldav_url): _FakeResp(
                status_code=301,
                url=caldav_url,
                headers={},
            ),
        }
    )
    _install_client(monkeypatch, client)
    out = run(email_mcp._try_wellknown_dav("example.com"))
    # PROPFIND fell through (no location), GET fallback got 404 default.
    assert out is None
    # GET was attempted for caldav (key not in result triggered the
    # fallback).
    assert ("GET", caldav_url) in client.calls
