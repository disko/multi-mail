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
    result = run(email_mcp.card_list_addressbooks(
        email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
    ))
    assert "# Address Books" in result
    assert "- **Personal**" in result
    assert "- **Work**" in result


def test_list_addressbooks_empty_returns_friendly_message(stub_account, monkeypatch):
    async def empty(acct):
        return []
    monkeypatch.setattr(email_mcp, "_carddav_propfind", empty)
    result = run(email_mcp.card_list_addressbooks(
        email_mcp.CardListAddressBooksInput(account_id=ACCT_ID)
    ))
    assert "No address books found" in result


# ---------------------------------------------------------------------------
# card_list_contacts
# ---------------------------------------------------------------------------

def test_list_contacts_sorted_alphabetically_by_fn(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/c1.vcf", _vcard("c1", "Charlie Brown", "charlie@example.com")),
        ("/c2.vcf", _vcard("c2", "alice example", "alice@example.com")),
        ("/c3.vcf", _vcard("c3", "Bob Sample", "bob@example.com")),
    ])
    result = run(email_mcp.card_list_contacts(
        email_mcp.CardListContactsInput(account_id=ACCT_ID)
    ))
    # Sorting is case-insensitive on fn
    alice_idx = result.index("alice example")
    bob_idx = result.index("Bob Sample")
    charlie_idx = result.index("Charlie Brown")
    assert alice_idx < bob_idx < charlie_idx


def test_list_contacts_limits_results_and_shows_total(stub_account, stub_books, monkeypatch):
    vcards = [(f"/c{i}.vcf", _vcard(f"u{i}", f"User {i:02d}", f"u{i}@example.com")) for i in range(10)]
    _install_vcards(monkeypatch, vcards)
    result = run(email_mcp.card_list_contacts(
        email_mcp.CardListContactsInput(account_id=ACCT_ID, limit=3)
    ))
    assert "(3 of 10)" in result


def test_list_contacts_empty_book_message(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [])
    result = run(email_mcp.card_list_contacts(
        email_mcp.CardListContactsInput(account_id=ACCT_ID)
    ))
    assert "No contacts found" in result


# ---------------------------------------------------------------------------
# card_search_contacts
# ---------------------------------------------------------------------------

def test_search_filters_case_insensitively(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("a", "Alice Example", "alice@example.com")),
        ("/b.vcf", _vcard("b", "Bob Sample", "bob@elsewhere.com")),
        ("/c.vcf", _vcard("c", "Carol", "carol@example.com")),
    ])
    result = run(email_mcp.card_search_contacts(
        email_mcp.CardSearchContactsInput(account_id=ACCT_ID, query="EXAMPLE.COM"),
    ))
    assert "Alice Example" in result
    assert "Carol" in result
    assert "Bob Sample" not in result


def test_search_no_matches_friendly_message(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("a", "Alice", "alice@example.com")),
    ])
    result = run(email_mcp.card_search_contacts(
        email_mcp.CardSearchContactsInput(account_id=ACCT_ID, query="nothing-matches"),
    ))
    assert "No contacts matching 'nothing-matches'" in result


def test_search_respects_limit(stub_account, stub_books, monkeypatch):
    vcards = [(f"/c{i}.vcf", _vcard(f"u{i}", "Common Name", f"u{i}@example.com")) for i in range(8)]
    _install_vcards(monkeypatch, vcards)
    result = run(email_mcp.card_search_contacts(
        email_mcp.CardSearchContactsInput(account_id=ACCT_ID, query="Common", limit=3),
    ))
    assert "(3 matches)" in result


# ---------------------------------------------------------------------------
# card_get_contact
# ---------------------------------------------------------------------------

def test_get_contact_returns_full_record(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("contact-1", "Alice Example", "alice@example.com", "+15550001")),
        ("/b.vcf", _vcard("contact-2", "Bob Sample", "bob@example.com")),
    ])
    result = run(email_mcp.card_get_contact(
        email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="contact-1"),
    ))
    assert "# Contact: Alice Example" in result
    assert "**UID**: contact-1" in result
    assert "**Email**: alice@example.com" in result
    assert "**Phone**: +15550001" in result


def test_get_contact_uid_not_found(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("contact-1", "Alice", "alice@example.com")),
    ])
    result = run(email_mcp.card_get_contact(
        email_mcp.CardGetContactInput(account_id=ACCT_ID, uid="missing"),
    ))
    assert "Contact with UID 'missing' not found" in result


# ---------------------------------------------------------------------------
# card_create_contact / update / delete — mock the HTTP layer
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=201, text=""):
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used by safe_async_client."""
    def __init__(self, response):
        self._response = response
        self.put_calls = []
        self.delete_calls = []

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


def _install_client(monkeypatch, response):
    client = _FakeAsyncClient(response)

    def factory(**kwargs):
        return client

    monkeypatch.setattr(email_mcp, "safe_async_client", factory)
    return client


def test_create_contact_puts_to_pinned_url(stub_account, stub_books, monkeypatch):
    client = _install_client(monkeypatch, _FakeResponse(status_code=201))

    result = run(email_mcp.card_create_contact(email_mcp.CardCreateContactInput(
        account_id=ACCT_ID, fn="Alice Example", email="alice@example.com",
        addressbook_name="Personal",
    )))

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


def test_create_contact_server_error_reports_status(stub_account, stub_books, monkeypatch):
    _install_client(monkeypatch, _FakeResponse(status_code=507, text="quota"))
    result = run(email_mcp.card_create_contact(email_mcp.CardCreateContactInput(
        account_id=ACCT_ID, fn="Bob", email="bob@example.com",
        addressbook_name="Personal",
    )))
    assert "507" in result


def test_delete_contact_walks_listing_then_deletes(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("alice-1", "Alice", "alice@example.com")),
        ("/b.vcf", _vcard("bob-2", "Bob", "bob@example.com")),
    ])
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(email_mcp.card_delete_contact(email_mcp.CardDeleteContactInput(
        account_id=ACCT_ID, uid="bob-2", addressbook_name="Personal",
    )))
    assert len(client.delete_calls) == 1
    # Stayed on configured host (host pin invariant)
    assert client.delete_calls[0]["url"].startswith("https://dav.example.com/")
    assert "deleted" in result.lower()


def test_delete_contact_uid_not_found_returns_message(stub_account, stub_books, monkeypatch):
    _install_vcards(monkeypatch, [
        ("/a.vcf", _vcard("alice-1", "Alice", "alice@example.com")),
    ])
    client = _install_client(monkeypatch, _FakeResponse(status_code=204))

    result = run(email_mcp.card_delete_contact(email_mcp.CardDeleteContactInput(
        account_id=ACCT_ID, uid="ghost", addressbook_name="Personal",
    )))
    assert "Contact with UID 'ghost' not found" in result
    assert client.delete_calls == []  # no DELETE issued for a missing target
