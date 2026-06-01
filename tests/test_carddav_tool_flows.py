"""Tests for the CardDAV-backed tool flows: ``card_list_addressbooks``,
``card_list_contacts``, ``card_search_contacts``, ``card_get_contact``,
``card_create_contact``, ``card_update_contact``, ``card_delete_contact``.

The DAV helpers ``_carddav_propfind``, ``_carddav_list_vcards``, and the
``safe_async_client`` factory are monkeypatched with in-memory stand-ins so we
exercise the tool-level orchestration (sorting, limiting, filtering, output
shape, status-code handling) without touching the network.
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


ACCT_ID = "work"
ACCT = {
    "id": ACCT_ID,
    "display_name": "Work",
    "email_address": "me@example.com",
    "username": "me@example.com",
    "password": "pw",
    "imap_host": "imap.example.com",
    "carddav_url": "https://dav.example.com/dav/",
}


# --- vCard fixtures --------------------------------------------------------


def _vcard(uid, fn, email_=None, tel=None):
    parts = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"FN:{fn}"]
    parts.append(f"N:{fn.split()[-1] if ' ' in fn else fn};{fn.split()[0]};;;")
    if email_:
        parts.append(f"EMAIL;TYPE=INTERNET:{email_}")
    if tel:
        parts.append(f"TEL;TYPE=CELL:{tel}")
    parts.append("END:VCARD")
    return "\r\n".join(parts) + "\r\n"


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def stub_account(monkeypatch):
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)


@pytest.fixture
def stub_books(monkeypatch):
    """Default: two address books, "Personal" (first) and "Work"."""
    books = [
        {"name": "Personal", "href": "https://dav.example.com/dav/abooks/personal/"},
        {"name": "Work", "href": "https://dav.example.com/dav/abooks/work/"},
    ]

    async def fake_propfind(acct):
        return books

    monkeypatch.setattr(email_mcp, "_carddav_propfind", fake_propfind)
    return books


def _install_vcards(monkeypatch, vcards):
    """Make _carddav_list_vcards return the given list of (href, data) tuples."""

    async def fake_list(acct, book_href):
        return vcards

    monkeypatch.setattr(email_mcp, "_carddav_list_vcards", fake_list)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# card_list_addressbooks
# ---------------------------------------------------------------------------


def test_list_addressbooks_renders_markdown(stub_account, stub_books):
    result = run(
        email_mcp.card_list_addressbooks(
            email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
        )
    )
    assert "# Address Books" in result
    assert "- **Personal**" in result
    assert "- **Work**" in result


def test_list_addressbooks_empty_returns_friendly_message(stub_account, monkeypatch):
    async def empty(acct):
        return []

    monkeypatch.setattr(email_mcp, "_carddav_propfind", empty)
    result = run(
        email_mcp.card_list_addressbooks(
            email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
        )
    )
    assert "No address books found" in result


# ---------------------------------------------------------------------------
# card_list_contacts
# ---------------------------------------------------------------------------


def test_list_contacts_sorted_alphabetically_by_fn(
    stub_account, stub_books, monkeypatch
):
    _install_vcards(
        monkeypatch,
        [
            ("/c1.vcf", _vcard("c1", "Charlie Brown", "charlie@example.com")),
            ("/c2.vcf", _vcard("c2", "alice example", "alice@example.com")),
            ("/c3.vcf", _vcard("c3", "Bob Sample", "bob@example.com")),
        ],
    )
    result = run(
        email_mcp.card_list_contacts(
            email_mcp.CardListContactsInput(account_id=ACCT_ID)
        )
    )
    # Sorting is case-insensitive on fn
    alice_idx = result.index("alice example")
    bob_idx = result.index("Bob Sample")
    charlie_idx = result.index("Charlie Brown")
    assert alice_idx < bob_idx < charlie_idx


def test_list_contacts_limits_results_and_shows_total(
    stub_account, stub_books, monkeypatch
):
    vcards = [
        (f"/c{i}.vcf", _vcard(f"u{i}", f"User {i:02d}", f"u{i}@example.com"))
        for i in range(10)
    ]
    _install_vcards(monkeypatch, vcards)
    result = run(
        email_mcp.card_list_contacts(
            email_mcp.CardListContactsInput(account_id=ACCT_ID, limit=3)
        )
    )
    assert "(3 of 10)" in result


def test_list_contacts_empty_book_message(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [])
    result = run(
        email_mcp.card_list_contacts(
            email_mcp.CardListContactsInput(account_id=ACCT_ID)
        )
    )
    assert "No contacts found" in result


# ---------------------------------------------------------------------------
# card_search_contacts
# ---------------------------------------------------------------------------


def test_search_filters_case_insensitively(stub_account, stub_books, monkeypatch):
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("a", "Alice Example", "alice@example.com")),
            ("/b.vcf", _vcard("b", "Bob Sample", "bob@elsewhere.com")),
            ("/c.vcf", _vcard("c", "Carol", "carol@example.com")),
        ],
    )
    result = run(
        email_mcp.card_search_contacts(
            email_mcp.CardSearchContactsInput(account_id=ACCT_ID, query="EXAMPLE.COM"),
        )
    )
    assert "Alice Example" in result
    assert "Carol" in result
    assert "Bob Sample" not in result


def test_search_no_matches_friendly_message(stub_account, stub_books, monkeypatch):
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("a", "Alice", "alice@example.com")),
        ],
    )
    result = run(
        email_mcp.card_search_contacts(
            email_mcp.CardSearchContactsInput(
                account_id=ACCT_ID, query="nothing-matches"
            ),
        )
    )
    assert "No contacts matching 'nothing-matches'" in result


def test_search_respects_limit(stub_account, stub_books, monkeypatch):
    vcards = [
        (f"/c{i}.vcf", _vcard(f"u{i}", "Common Name", f"u{i}@example.com"))
        for i in range(8)
    ]
    _install_vcards(monkeypatch, vcards)
    result = run(
        email_mcp.card_search_contacts(
            email_mcp.CardSearchContactsInput(
                account_id=ACCT_ID, query="Common", limit=3
            ),
        )
    )
    assert "(3 matches)" in result


# ---------------------------------------------------------------------------
# card_get_contact
# ---------------------------------------------------------------------------


def test_get_contact_returns_full_record(stub_account, stub_books, monkeypatch):
    _install_vcards(
        monkeypatch,
        [
            (
                "/a.vcf",
                _vcard("contact-1", "Alice Example", "alice@example.com", "+15550001"),
            ),
            ("/b.vcf", _vcard("contact-2", "Bob Sample", "bob@example.com")),
        ],
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="contact-1"),
        )
    )
    assert "# Contact: Alice Example" in result
    assert "**UID**: contact-1" in result
    assert "**Email**: alice@example.com" in result
    assert "**Phone**: +15550001" in result


def test_get_contact_uid_not_found(stub_account, stub_books, monkeypatch):
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("contact-1", "Alice", "alice@example.com")),
        ],
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="missing"),
        )
    )
    assert "Contact with UID 'missing' not found" in result


# ---------------------------------------------------------------------------
# card_create_contact / update / delete — mock the HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=201, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used by safe_async_client."""

    def __init__(self, response):
        self._response = response
        self.put_calls = []
        self.delete_calls = []
        self.request_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def put(self, url, content=None, headers=None):
        self.put_calls.append({"url": url, "content": content, "headers": headers})
        return self._response

    async def delete(self, url, headers=None):
        self.delete_calls.append({"url": url, "headers": headers})
        return self._response

    async def request(self, method, url, content=None, headers=None):
        self.request_calls.append(
            {"method": method, "url": url, "content": content, "headers": headers}
        )
        return self._response


def _install_client(monkeypatch, response):
    client = _FakeAsyncClient(response)

    def factory(**kwargs):
        return client

    monkeypatch.setattr(email_mcp, "safe_async_client", factory)
    return client


def test_create_contact_puts_to_pinned_url(stub_account, stub_books, monkeypatch):
    client = _install_client(monkeypatch, _FakeResponse(status_code=201))

    result = run(
        email_mcp.card_create_contact(
            email_mcp.CardCreateContactInput(
                account_id=ACCT_ID,
                fn="Alice Example",
                email="alice@example.com",
                addressbook_name="Personal",
            )
        )
    )

    assert len(client.put_calls) == 1
    url = client.put_calls[0]["url"]
    # Must stay on the configured carddav host
    assert url.startswith("https://dav.example.com/")
    # Body is the serialized vCard
    body = client.put_calls[0]["content"]
    assert "BEGIN:VCARD" in body
    assert "FN:Alice Example" in body
    assert "EMAIL:alice@example.com" in body
    assert "created" in result.lower()


def test_create_contact_server_error_reports_status(
    stub_account, stub_books, monkeypatch
):
    _install_client(monkeypatch, _FakeResponse(status_code=507, text="quota"))
    result = run(
        email_mcp.card_create_contact(
            email_mcp.CardCreateContactInput(
                account_id=ACCT_ID,
                fn="Bob",
                email="bob@example.com",
                addressbook_name="Personal",
            )
        )
    )
    assert "507" in result


def test_delete_contact_walks_listing_then_deletes(
    stub_account, stub_books, monkeypatch
):
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("alice-1", "Alice", "alice@example.com")),
            ("/b.vcf", _vcard("bob-2", "Bob", "bob@example.com")),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_delete_contact(
            email_mcp.CardDeleteContactInput(
                account_id=ACCT_ID,
                uid="bob-2",
                addressbook_name="Personal",
            )
        )
    )
    assert len(client.delete_calls) == 1
    # Stayed on configured host (host pin invariant)
    assert client.delete_calls[0]["url"].startswith("https://dav.example.com/")
    assert "deleted" in result.lower()


def test_delete_contact_uid_not_found_returns_message(
    stub_account, stub_books, monkeypatch
):
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("alice-1", "Alice", "alice@example.com")),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_delete_contact(
            email_mcp.CardDeleteContactInput(
                account_id=ACCT_ID,
                uid="ghost",
                addressbook_name="Personal",
            )
        )
    )
    assert "Contact with UID 'ghost' not found" in result
    assert client.delete_calls == []  # no DELETE issued for a missing target


# ---------------------------------------------------------------------------
# card_update_contact — coverage iteration 1 (issue #8)
# ---------------------------------------------------------------------------


def test_update_contact_changes_fn_only(stub_account, stub_books, monkeypatch):
    """Updating only fn preserves existing email; PUT lands on pinned host."""
    _install_vcards(
        monkeypatch,
        [
            (
                "/dav/abooks/personal/c1.vcf",
                _vcard("c1", "Alice Old", "alice@example.com"),
            ),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                fn="Alice New",
            )
        )
    )

    assert len(client.put_calls) == 1
    assert client.put_calls[0]["url"].startswith("https://dav.example.com/")
    body = client.put_calls[0]["content"]
    assert "FN:Alice New" in body
    # existing email preserved because params.email is None
    assert "alice@example.com" in body
    assert "updated" in result.lower()


def test_update_contact_replaces_email_and_tel_lists(
    stub_account, stub_books, monkeypatch
):
    """Comma-separated email/tel replace existing values rather than appending."""
    _install_vcards(
        monkeypatch,
        [
            (
                "/dav/abooks/personal/c1.vcf",
                _vcard("c1", "Bob", "bob.old@example.com", "+15550001"),
            ),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                email="b1@example.com, b2@example.com",
                tel="+15550002, +15550003",
            )
        )
    )

    assert len(client.put_calls) == 1
    body = client.put_calls[0]["content"]
    assert "b1@example.com" in body
    assert "b2@example.com" in body
    assert "bob.old@example.com" not in body
    assert "+15550002" in body
    assert "+15550003" in body
    assert "+15550001" not in body
    assert "updated" in result.lower()


def test_update_contact_adds_org_and_title_when_absent(
    stub_account, stub_books, monkeypatch
):
    """vCard with no ORG/TITLE → fall through to vc.add() else-branch. Status 200."""
    _install_vcards(
        monkeypatch,
        [
            ("/dav/abooks/personal/c1.vcf", _vcard("c1", "Carol", "carol@example.com")),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=200))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                org="Acme Inc",
                title="Engineer",
            )
        )
    )

    assert len(client.put_calls) == 1
    body = client.put_calls[0]["content"]
    assert "Acme Inc" in body
    assert "Engineer" in body
    assert "updated" in result.lower()


def test_update_contact_overwrites_existing_org_and_title(
    stub_account, stub_books, monkeypatch
):
    """vCard with existing ORG/TITLE → hasattr(vc, ...) True branch overwrites."""
    vcard_with_org = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "UID:c1\r\n"
        "FN:Carol\r\n"
        "N:Carol;Carol;;;\r\n"
        "ORG:Old Co\r\n"
        "TITLE:Old Title\r\n"
        "END:VCARD\r\n"
    )
    _install_vcards(
        monkeypatch,
        [
            ("/dav/abooks/personal/c1.vcf", vcard_with_org),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                org="New Co",
                title="New Title",
            )
        )
    )

    assert len(client.put_calls) == 1
    body = client.put_calls[0]["content"]
    assert "New Co" in body
    assert "New Title" in body
    assert "Old Co" not in body
    assert "Old Title" not in body
    assert "updated" in result.lower()


def test_update_contact_uid_not_found_returns_message(
    stub_account, stub_books, monkeypatch
):
    """When uid is not present in listing, the function bails before PUT."""
    _install_vcards(
        monkeypatch,
        [
            (
                "/dav/abooks/personal/c1.vcf",
                _vcard("alice-1", "Alice", "alice@example.com"),
            ),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="ghost",
                addressbook_name="Personal",
                fn="anything",
            )
        )
    )
    assert "Contact with UID 'ghost' not found" in result
    assert client.put_calls == []


def test_update_contact_server_error_reports_status(
    stub_account, stub_books, monkeypatch
):
    """Non-2xx response from PUT surfaces status code in the error string."""
    _install_vcards(
        monkeypatch,
        [
            ("/dav/abooks/personal/c1.vcf", _vcard("c1", "Alice", "alice@example.com")),
        ],
    )
    _install_client(monkeypatch, _FakeResponse(status_code=507, text="quota"))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                fn="Alice New",
            )
        )
    )
    assert "507" in result
    assert result.startswith("Error:")


def test_update_contact_skips_unparseable_vcard_and_continues(
    stub_account, stub_books, monkeypatch
):
    """Malformed vCard mid-listing is skipped; loop continues to the next entry."""
    _install_vcards(
        monkeypatch,
        [
            ("/dav/abooks/personal/garbage.vcf", "NOT A VCARD\n"),
            (
                "/dav/abooks/personal/c2.vcf",
                _vcard("c2", "Real Person", "real@example.com"),
            ),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c2",
                addressbook_name="Personal",
                fn="New Name",
            )
        )
    )
    assert len(client.put_calls) == 1
    assert "updated" in result.lower()


def test_update_contact_outer_exception_returns_error_string(stub_account, monkeypatch):
    """Helper raising mid-flow is caught by outer except → error message."""

    async def boom(acct):
        raise RuntimeError("boom")

    monkeypatch.setattr(email_mcp, "_carddav_propfind", boom)

    result = run(
        email_mcp.card_update_contact(
            email_mcp.CardUpdateContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
                fn="x",
            )
        )
    )
    assert result.startswith("Error updating contact:")
    assert "boom" in result


# ---------------------------------------------------------------------------
# _carddav_propfind / _carddav_list_vcards — XML parsing coverage
# ---------------------------------------------------------------------------

_PROPFIND_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response>
    <d:href>https://dav.example.com/dav/abooks/personal/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Personal</d:displayname>
        <d:resourcetype>
          <d:collection/>
          <card:addressbook/>
        </d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>https://dav.example.com/dav/abooks/unnamed/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname/>
        <d:resourcetype>
          <d:collection/>
          <card:addressbook/>
        </d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>https://dav.example.com/dav/abooks/</d:href>
    <d:propstat>
      <d:prop>
        <d:displayname>Root</d:displayname>
        <d:resourcetype>
          <d:collection/>
        </d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_carddav_propfind_parses_addressbook_xml(stub_account, monkeypatch):
    """PROPFIND XML round-trip: two addressbooks parsed, non-AB collection skipped."""
    client = _install_client(
        monkeypatch, _FakeResponse(status_code=200, text=_PROPFIND_XML)
    )

    result = run(email_mcp._carddav_propfind(ACCT))

    assert len(result) == 2
    assert result[0]["name"] == "Personal"
    assert result[0]["href"].endswith("/personal/")
    # displayname element exists but is empty → name_el.text is None →
    # `name or "(unnamed)"` fallback fires
    assert result[1]["name"] == "(unnamed)"
    # Confirm the request was a PROPFIND with Depth: 1
    assert len(client.request_calls) == 1
    call = client.request_calls[0]
    assert call["method"] == "PROPFIND"
    assert call["headers"]["Depth"] == "1"
    assert call["url"] == ACCT["carddav_url"]


def test_carddav_propfind_parse_error_returns_empty(stub_account, monkeypatch):
    """Malformed XML triggers ET.ParseError swallow → empty list."""
    _install_client(monkeypatch, _FakeResponse(status_code=200, text="<<<not xml>>>"))
    result = run(email_mcp._carddav_propfind(ACCT))
    assert result == []


_REPORT_XML = """<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">
  <d:response>
    <d:href>/dav/abooks/personal/c1.vcf</d:href>
    <d:propstat>
      <d:prop>
        <d:getetag>"abc"</d:getetag>
        <card:address-data>BEGIN:VCARD&#13;
VERSION:3.0&#13;
UID:c1&#13;
FN:Alice&#13;
END:VCARD&#13;
</card:address-data>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/abooks/personal/c2.vcf</d:href>
    <d:propstat>
      <d:prop>
        <d:getetag>"def"</d:getetag>
        <card:address-data>BEGIN:VCARD&#13;
VERSION:3.0&#13;
UID:c2&#13;
FN:Bob&#13;
END:VCARD&#13;
</card:address-data>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/dav/abooks/personal/empty.vcf</d:href>
    <d:propstat>
      <d:prop>
        <d:getetag>"ghi"</d:getetag>
        <card:address-data/>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>
"""


def test_carddav_list_vcards_parses_report_xml(stub_account, monkeypatch):
    """REPORT XML round-trip: two vcards parsed, empty address-data entry skipped."""
    client = _install_client(
        monkeypatch, _FakeResponse(status_code=200, text=_REPORT_XML)
    )

    result = run(email_mcp._carddav_list_vcards(ACCT, "/dav/abooks/personal/"))

    assert len(result) == 2
    for href, data in result:
        assert "BEGIN:VCARD" in data
    # Confirm REPORT method + pinned host
    assert len(client.request_calls) == 1
    call = client.request_calls[0]
    assert call["method"] == "REPORT"
    assert call["url"].startswith("https://dav.example.com/")


def test_carddav_list_vcards_parse_error_returns_empty(stub_account, monkeypatch):
    """Malformed XML triggers ET.ParseError swallow → empty list."""
    _install_client(monkeypatch, _FakeResponse(status_code=200, text="<<<not xml>>>"))
    result = run(email_mcp._carddav_list_vcards(ACCT, "/dav/abooks/personal/"))
    assert result == []


# ---------------------------------------------------------------------------
# Outer-except tail sweep + _get_addressbook_href ValueError paths
# (coverage iter-4, issue #8)
#
# Pins:
#   - card_list_addressbooks outer except (2982-2983)
#   - _get_addressbook_href no-books raise (2941-2942) + name-not-found raise (2947-2950)
#   - card_search_contacts outer except (3081-3082)
#   - card_get_contact outer except (3127-3128)
#
# Note: _carddav_propfind, _get_addressbook_href, _carddav_list_vcards are
# async coroutines — the raising override must also be async, otherwise the
# await chain raises TypeError instead of the injected exception.
# ---------------------------------------------------------------------------


def _async_raise_factory(exc):
    """Return an async function that raises ``exc`` when awaited."""

    async def boom(*args, **kwargs):
        raise exc

    return boom


def test_list_addressbooks_outer_except_returns_error(stub_account, monkeypatch):
    monkeypatch.setattr(
        email_mcp,
        "_carddav_propfind",
        _async_raise_factory(RuntimeError("propfind boom")),
    )
    result = run(
        email_mcp.card_list_addressbooks(
            email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
        )
    )
    assert result.startswith("Error:")
    assert "propfind boom" in result


def test_get_addressbook_href_raises_when_no_books(stub_account, monkeypatch):
    """No address books at all → _get_addressbook_href raises ValueError;
    card_list_contacts's outer except swallows and surfaces it."""

    async def empty(acct):
        return []

    monkeypatch.setattr(email_mcp, "_carddav_propfind", empty)
    result = run(
        email_mcp.card_list_contacts(
            email_mcp.CardListContactsInput(account_id=ACCT_ID)
        )
    )
    assert result.startswith("Error:")
    assert "No address books found" in result
    assert ACCT_ID in result


def test_get_addressbook_href_raises_when_name_not_in_books(
    stub_account, stub_books, monkeypatch
):
    """addressbook_name not in propfind list → ValueError listing the
    available names; card_list_contacts's outer except surfaces it."""
    result = run(
        email_mcp.card_list_contacts(
            email_mcp.CardListContactsInput(
                account_id=ACCT_ID,
                addressbook_name="DoesNotExist",
            )
        )
    )
    assert result.startswith("Error:")
    assert "Address book 'DoesNotExist' not found" in result
    assert "Personal" in result
    assert "Work" in result


def test_card_search_contacts_outer_except_returns_error(stub_account, monkeypatch):
    monkeypatch.setattr(
        email_mcp,
        "_get_addressbook_href",
        _async_raise_factory(RuntimeError("search boom")),
    )
    result = run(
        email_mcp.card_search_contacts(
            email_mcp.CardSearchContactsInput(account_id=ACCT_ID, query="any")
        )
    )
    assert result.startswith("Error:")
    assert "search boom" in result


def test_card_get_contact_outer_except_returns_error(stub_account, monkeypatch):
    monkeypatch.setattr(
        email_mcp,
        "_get_addressbook_href",
        _async_raise_factory(RuntimeError("get boom")),
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="x")
        )
    )
    assert result.startswith("Error:")
    assert "get boom" in result


# ---------------------------------------------------------------------------
# Body branches — card_get_contact / card_create_contact / card_delete_contact
# (issue #8 iter-5)
# ---------------------------------------------------------------------------


def test_get_contact_renders_org_and_title_when_present(
    stub_account, stub_books, monkeypatch
):
    """vCard with ORG and TITLE → those rendering branches fire (lines 3119-3122)."""
    vcard_with_org = (
        "BEGIN:VCARD\r\n"
        "VERSION:3.0\r\n"
        "UID:c1\r\n"
        "FN:Carol Org\r\n"
        "N:Org;Carol;;;\r\n"
        "EMAIL;TYPE=INTERNET:carol@example.com\r\n"
        "TEL;TYPE=CELL:+15550100\r\n"
        "ORG:Acme Inc\r\n"
        "TITLE:Engineer\r\n"
        "END:VCARD\r\n"
    )
    _install_vcards(
        monkeypatch,
        [
            ("/dav/abooks/personal/c1.vcf", vcard_with_org),
        ],
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="c1")
        )
    )
    assert "# Contact: Carol Org" in result
    assert "**Email**: carol@example.com" in result
    assert "**Phone**: +15550100" in result
    assert "**Organization**: Acme Inc" in result
    assert "**Title**: Engineer" in result


def test_get_contact_skips_vcards_that_fail_to_parse(
    stub_account, stub_books, monkeypatch
):
    """Malformed vCard → vobject.readOne raises → inner `continue` arm (3124-3125)."""
    valid = _vcard("c1", "Alice Valid", "alice@example.com")
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", "garbage not a vcard"),  # vobject.readOne raises here
            ("/b.vcf", valid),
        ],
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="c1")
        )
    )
    # The malformed entry was skipped; the valid one rendered.
    assert "# Contact: Alice Valid" in result
    assert "**Email**: alice@example.com" in result


def test_create_contact_handles_tel_org_title_branches(
    stub_account, stub_books, monkeypatch
):
    """tel/org/title set → the conditional add branches all fire (3168-3177)."""
    client = _install_client(monkeypatch, _FakeResponse(status_code=201))

    result = run(
        email_mcp.card_create_contact(
            email_mcp.CardCreateContactInput(
                account_id=ACCT_ID,
                fn="Dave Multi",
                email="dave@example.com",
                tel="+15550111, +15550222",
                org="Acme",
                title="Manager",
                addressbook_name="Personal",
            )
        )
    )

    assert len(client.put_calls) == 1
    body = client.put_calls[0]["content"]
    # All four optional sections present in the serialized vCard
    assert "EMAIL:dave@example.com" in body
    assert "TEL:+15550111" in body
    assert "TEL:+15550222" in body
    assert "Acme" in body
    assert "Manager" in body
    assert "created" in result.lower()


def test_delete_contact_returns_error_on_non_2xx_response(
    stub_account, stub_books, monkeypatch
):
    """Server returns 403 → ``Error: Server returned 403`` (line 3338)."""
    _install_vcards(
        monkeypatch,
        [
            ("/a.vcf", _vcard("c1", "Alice", "alice@example.com")),
        ],
    )
    _install_client(monkeypatch, _FakeResponse(status_code=403, text="forbidden"))

    result = run(
        email_mcp.card_delete_contact(
            email_mcp.CardDeleteContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
            )
        )
    )
    assert "Error: Server returned 403" in result


def test_delete_contact_skips_vcards_that_fail_to_parse(
    stub_account, stub_books, monkeypatch
):
    """Malformed vCard in the walk → inner `continue` arm (3324-3325)."""
    _install_vcards(
        monkeypatch,
        [
            ("/bad.vcf", "garbage not a vcard"),
            ("/good.vcf", _vcard("c1", "Alice", "alice@example.com")),
        ],
    )
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(
        email_mcp.card_delete_contact(
            email_mcp.CardDeleteContactInput(
                account_id=ACCT_ID,
                uid="c1",
                addressbook_name="Personal",
            )
        )
    )
    # The malformed entry was skipped; the valid one matched and was deleted.
    assert "deleted" in result.lower()
    assert len(client.delete_calls) == 1


# ---------------------------------------------------------------------------
# card_search_contacts + card_create_contact + card_delete_contact mop-up
# (#8 iter-6) — empty-result branch, no-email create branch, outer-excepts
# ---------------------------------------------------------------------------


def test_search_contacts_returns_friendly_message_when_empty(
    stub_account,
    stub_books,
    monkeypatch,
):
    """Empty address book → 'No contacts found.' (line 3057)."""
    _install_vcards(monkeypatch, [])
    result = run(
        email_mcp.card_search_contacts(
            email_mcp.CardSearchContactsInput(
                account_id=ACCT_ID,
                query="alice",
                addressbook_name="Personal",
            )
        )
    )
    assert result == "No contacts found."


def test_create_contact_without_email_omits_email_field(
    stub_account,
    stub_books,
    monkeypatch,
):
    """``email=None`` → the `if params.email:` branch is false. Pins partial
    3168->3171."""
    client = _install_client(monkeypatch, _FakeResponse(status_code=201))
    result = run(
        email_mcp.card_create_contact(
            email_mcp.CardCreateContactInput(
                account_id=ACCT_ID,
                fn="Bob NoEmail",
                tel="+15550133",
                addressbook_name="Personal",
            )
        )
    )
    assert "created" in result.lower()
    assert len(client.put_calls) == 1
    body = client.put_calls[0]["content"]
    assert "FN:Bob NoEmail" in body
    assert "TEL:+15550133" in body
    assert "EMAIL" not in body


def test_create_contact_outer_except_when_get_account_raises(monkeypatch):
    """``_get_account`` raises → 'Error creating contact: …' (lines 3199-3200)."""

    def _boom(aid):
        raise RuntimeError("acct boom")

    monkeypatch.setattr(email_mcp, "_get_account", _boom)
    result = run(
        email_mcp.card_create_contact(
            email_mcp.CardCreateContactInput(
                account_id=ACCT_ID,
                fn="x",
                email="x@example.com",
            )
        )
    )
    assert result.startswith("Error creating contact")
    assert "acct boom" in result


def test_delete_contact_outer_except_when_get_account_raises(monkeypatch):
    """``_get_account`` raises → 'Error deleting contact: …' (lines 3341-3342)."""

    def _boom(aid):
        raise RuntimeError("del-acct boom")

    monkeypatch.setattr(email_mcp, "_get_account", _boom)
    result = run(
        email_mcp.card_delete_contact(
            email_mcp.CardDeleteContactInput(
                account_id=ACCT_ID,
                uid="c1",
            )
        )
    )
    assert result.startswith("Error deleting contact")
    assert "del-acct boom" in result


# ---------------------------------------------------------------------------
# card_get_contact — no-email rendering branch (#8 iter-7)
# ---------------------------------------------------------------------------


def test_get_contact_omits_email_line_when_vcard_has_no_email(
    stub_account,
    stub_books,
    monkeypatch,
):
    """vCard with no EMAIL field → the ``if c.get("email"):`` arm is False
    and the ``**Email**`` line is omitted from the rendered output. Pins
    partial 3115->3117 (skip the email-line append, fall through to the
    tel check)."""
    _install_vcards(
        monkeypatch,
        [
            (
                "/dav/abooks/personal/solo.vcf",
                _vcard("solo-1", "Solo Contact", email_=None, tel="+15550100"),
            ),
        ],
    )
    result = run(
        email_mcp.card_get_contact(
            email_mcp.CardGetContactInput(
                account_id=ACCT_ID,
                uid="solo-1",
                addressbook_name="Personal",
            )
        )
    )
    assert "# Contact: Solo Contact" in result
    assert "**UID**: solo-1" in result
    assert "**Email**" not in result
    assert "**Phone**: +15550100" in result
