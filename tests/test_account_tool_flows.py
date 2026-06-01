"""Tests for the public account-management MCP tool wrappers:

- ``email_list_accounts`` — empty-config hint, populated rendering, and the
  ``display_name or id`` fallback (gotcha #1: do NOT use the
  ``dict.get(k, default)`` idiom — explicit ``or`` is required so that an
  on-disk ``"display_name": null`` falls back to the id).
- ``email_remove_account`` — happy-path removal, not-found early-return
  (must NOT call ``_save_accounts``), and the empty-config not-found path.

Sibling files:
- ``test_account_io.py`` — exercises ``_load_accounts`` / ``_save_accounts``
  / ``_get_account`` (the IO seam beneath these tools).
- ``test_imap_write_flows.py`` — exercises ``email_add_account`` (the
  remove tool's sibling write path).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "email_mcp", ROOT / "servers" / "email_mcp.py"
)
email_mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(email_mcp)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a per-test temp file (mirrors test_account_io.py)."""
    path = tmp_path / "subdir" / "accounts.json"
    monkeypatch.setattr(email_mcp, "CONFIG_PATH", str(path))
    return path


def run(coro):
    return asyncio.run(coro)


def _acct(**overrides):
    base = {
        "id": "acct1",
        "display_name": "Work",
        "email_address": "me@example.com",
        "username": "me@example.com",
        "password": "pw",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# email_list_accounts
# ---------------------------------------------------------------------------


def test_list_accounts_empty_returns_hint(config):
    """No config file → short-circuit hint message; rendering loop is not reached."""
    assert not config.exists()
    out = run(email_mcp.email_list_accounts())
    assert out.startswith("No email accounts configured.")
    assert "email_add_account" in out


def test_list_accounts_renders_each_entry_with_display_name(config):
    """Populated config renders each account with header tally, host:port,
    and the ``display_name or id`` fallback for each row."""
    email_mcp._save_accounts(
        [
            _acct(id="acct1", display_name="Work"),
            _acct(id="acct2", display_name=None, email_address="me@example.org"),
        ]
    )
    out = run(email_mcp.email_list_accounts())
    assert "# Configured Email Accounts (2)" in out
    # display_name branch:
    assert "## Work (`acct1`)" in out
    # id fallback branch (display_name is None → falls back to id):
    assert "## acct2 (`acct2`)" in out
    assert "- **Email**: me@example.com" in out
    assert "- **IMAP**: imap.example.com:993 (ssl)" in out
    assert "- **SMTP**: smtp.example.com:587 (starttls)" in out


def test_list_accounts_uses_id_when_display_name_is_null(config):
    """Regression pin for gotcha #1 (v0.3.2 fix): a serialized
    ``"display_name": null`` must fall back to the account id, not print
    as ``None``. ``acct.get("display_name") or a["id"]`` is correct;
    ``acct.get("display_name", a["id"])`` would be the bug."""
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "id": "x",
                        "display_name": None,
                        "email_address": "x@example.com",
                        "imap_host": "imap.example.com",
                        "smtp_host": "smtp.example.com",
                    },
                ]
            }
        )
    )
    out = run(email_mcp.email_list_accounts())
    assert "## x (`x`)" in out
    assert "None" not in out


# ---------------------------------------------------------------------------
# email_remove_account
# ---------------------------------------------------------------------------


def test_remove_account_removes_existing_and_persists(config):
    """Happy path: load → filter → save → confirmation. Disk reflects the
    removal — only the surviving account remains."""
    email_mcp._save_accounts(
        [
            _acct(id="work", display_name="Work"),
            _acct(
                id="personal", display_name="Personal", email_address="me@example.org"
            ),
        ]
    )
    out = run(
        email_mcp.email_remove_account(email_mcp.RemoveAccountInput(account_id="work"))
    )
    assert "Account 'work' removed." in out
    remaining = json.loads(config.read_text())["accounts"]
    assert len(remaining) == 1
    assert remaining[0]["id"] == "personal"


def test_remove_account_not_found_returns_error_and_leaves_disk_unchanged(config):
    """The not-found early-return must NOT call _save_accounts — disk
    contents and mtime stay identical to the pre-call state."""
    email_mcp._save_accounts([_acct(id="only")])
    pre_content = config.read_text()
    pre_mtime_ns = config.stat().st_mtime_ns

    out = run(
        email_mcp.email_remove_account(email_mcp.RemoveAccountInput(account_id="ghost"))
    )
    assert "Error: Account 'ghost' not found." in out
    # Disk content unchanged.
    assert config.read_text() == pre_content
    # mtime unchanged — proves _save_accounts was not invoked.
    assert config.stat().st_mtime_ns == pre_mtime_ns


def test_remove_account_with_empty_config_returns_not_found(config):
    """No config file at all → ``_load_accounts`` returns ``[]`` → the
    not-found branch fires without _save_accounts ever creating the file."""
    assert not config.exists()
    out = run(
        email_mcp.email_remove_account(
            email_mcp.RemoveAccountInput(account_id="anything")
        )
    )
    assert "Error: Account 'anything' not found." in out
    # No file was created — _save_accounts was not called.
    assert not config.exists()
