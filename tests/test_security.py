"""Unit tests for servers/_security.py."""

from __future__ import annotations

import pytest

import _security as sec


class TestIsSafeHost:
    @pytest.mark.parametrize(
        "host,expected_ok",
        [
            ("127.0.0.1", False),
            ("localhost", False),
            ("169.254.169.254", False),
            ("metadata.google.internal", False),
            ("10.0.0.1", False),
            ("172.16.0.1", False),
            ("192.168.1.1", False),
            ("0.0.0.0", False),
            ("::1", False),
            ("fe80::1", False),
            ("fd00:ec2::254", False),
            ("8.8.8.8", True),
            ("1.1.1.1", True),
        ],
    )
    def test_literal_addresses(self, host, expected_ok):
        ok, reason = sec._is_safe_host(host)
        assert ok is expected_ok, f"{host}: {reason}"

    def test_empty_host_blocked(self):
        ok, _ = sec._is_safe_host("")
        assert ok is False

    def test_unresolvable_host_blocked(self):
        ok, reason = sec._is_safe_host("definitely-not-a-real-tld-xyzzy.invalid")
        assert ok is False
        assert "DNS resolution failed" in reason

    def test_escape_hatch(self, monkeypatch):
        monkeypatch.setattr(sec, "_ALLOW_PRIVATE", True)
        ok, _ = sec._is_safe_host("127.0.0.1")
        assert ok is True


class TestCheckUrlSafe:
    def test_blocks_loopback_url(self):
        with pytest.raises(__import__("httpx").RequestError):
            sec._check_url_safe("http://127.0.0.1/admin")

    def test_blocks_metadata_url(self):
        with pytest.raises(__import__("httpx").RequestError):
            sec._check_url_safe(
                "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
            )

    def test_allows_public_url(self):
        sec._check_url_safe("https://example.com/")


class TestSafeAsyncClientRedirectHook:
    """Verify the SSRF guard fires on redirect hops, not just initial requests."""

    @pytest.mark.asyncio
    async def test_redirect_to_loopback_blocked(self):
        """A 302 from a public host to 127.0.0.1 must be blocked by the hook."""
        import httpx

        # Use a mock transport so we don't depend on network.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "example.com":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})
            return httpx.Response(200, text="should not reach here")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            event_hooks={"request": [sec._enforce_ssrf_on_request]},
        ) as client:
            with pytest.raises(httpx.RequestError):
                await client.get("http://example.com/")


# ---------------------------------------------------------------------------
# _security.py mop-up (#8 iter-6)
#
# Pins:
#   - `_is_safe_host` ValueError fallthrough inside the resolved-IP loop
#     (lines 74-75) — fires when getaddrinfo returns a sockaddr whose
#     first element isn't a parseable IP literal.
#   - `safe_async_client` factory body (line 139) — every other test
#     monkeypatches this out; this one calls it directly.
# ---------------------------------------------------------------------------


class TestIsSafeHostSockaddrValueErrorSkip:
    """When getaddrinfo returns a sockaddr whose address part fails to parse
    as an IP literal, the loop must `continue` to the next entry, not crash.
    """

    def test_skips_unparseable_sockaddr_ip(self, monkeypatch):
        """Mock getaddrinfo to mix a non-IP sockaddr with a valid public IP."""
        import socket

        fake_infos = [
            # AF_INET-shaped tuple with garbage address.
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("not-an-ip", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.1.1.1", 0)),
        ]
        monkeypatch.setattr(sec.socket, "getaddrinfo", lambda *a, **kw: fake_infos)

        ok, reason = sec._is_safe_host("example.com")
        assert ok is True, reason


class TestSafeAsyncClientFactory:
    """Direct call into the factory — pin the AsyncClient construction (line 139)."""

    @pytest.mark.asyncio
    async def test_factory_returns_configured_client(self):
        import httpx

        client = sec.safe_async_client(timeout=10.0)
        try:
            assert isinstance(client, httpx.AsyncClient)
            # event hooks include the SSRF guard
            hooks = client.event_hooks.get("request", [])
            assert sec._enforce_ssrf_on_request in hooks
        finally:
            await client.aclose()
