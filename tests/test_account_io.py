"""Tests for ``_load_accounts``, ``_save_accounts``, and ``_get_account``.

Covers:
- Empty / missing config file.
- Round-trip persistence.
- Parent-directory creation.
- POSIX file/dir permissions (0o600 / 0o700) — the file holds plaintext
  credentials and must not be world-readable.
- ``_get_account`` lookup and not-found path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
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
    """Point CONFIG_PATH at a per-test temp file."""
    path = tmp_path / "subdir" / "accounts.json"
    monkeypatch.setattr(email_mcp, "CONFIG_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# _load_accounts
# ---------------------------------------------------------------------------

def test_load_returns_empty_list_when_file_missing(config):
    assert not config.exists()
    assert email_mcp._load_accounts() == []


def test_load_returns_accounts_from_valid_json(config):
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"accounts": [{"id": "a", "email_address": "a@example.com"}]}))
    accounts = email_mcp._load_accounts()
    assert accounts == [{"id": "a", "email_address": "a@example.com"}]


def test_load_returns_empty_list_when_no_accounts_key(config):
    """A config file shaped {} should not crash — treat as no accounts."""
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{}")
    assert email_mcp._load_accounts() == []


# ---------------------------------------------------------------------------
# _save_accounts
# ---------------------------------------------------------------------------

def test_save_creates_parent_directory(config):
    assert not config.parent.exists()
    email_mcp._save_accounts([])
    assert config.parent.is_dir()


def test_save_writes_accounts_under_top_level_key(config):
    accts = [{"id": "x", "password": "secret"}]
    email_mcp._save_accounts(accts)
    payload = json.loads(config.read_text())
    assert payload == {"accounts": accts}


def test_save_round_trips_with_load(config):
    accts = [
        {"id": "work", "email_address": "me@example.com", "password": "pw1"},
        {"id": "personal", "email_address": "me@example.org", "password": "pw2"},
    ]
    email_mcp._save_accounts(accts)
    assert email_mcp._load_accounts() == accts


def test_save_overwrites_existing_file(config):
    email_mcp._save_accounts([{"id": "first"}])
    email_mcp._save_accounts([{"id": "second"}])
    assert email_mcp._load_accounts() == [{"id": "second"}]


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only chmod check")
def test_save_file_is_owner_only(config):
    """0600: read/write only for owner. World-readable mode would leak creds."""
    email_mcp._save_accounts([{"id": "a", "password": "secret"}])
    mode = stat.S_IMODE(config.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only chmod check")
def test_save_parent_dir_is_owner_only(config):
    email_mcp._save_accounts([])
    mode = stat.S_IMODE(config.parent.stat().st_mode)
    assert mode == 0o700, f"expected 0700 on parent, got {oct(mode)}"


# ---------------------------------------------------------------------------
# _get_account
# ---------------------------------------------------------------------------

def test_get_account_returns_matching_entry(config):
    email_mcp._save_accounts([
        {"id": "a", "email_address": "a@example.com"},
        {"id": "b", "email_address": "b@example.com"},
    ])
    assert email_mcp._get_account("b")["email_address"] == "b@example.com"


def test_get_account_raises_when_not_found(config):
    email_mcp._save_accounts([{"id": "only", "email_address": "only@example.com"}])
    with pytest.raises(ValueError, match="not found"):
        email_mcp._get_account("missing")


def test_get_account_raises_on_empty_config(config):
    with pytest.raises(ValueError, match="not found"):
        email_mcp._get_account("anything")
