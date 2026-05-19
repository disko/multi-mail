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


# ---------------------------------------------------------------------------
# _save_accounts error-path branches (issue #8 iter-6)
#
# Pins the OSError-swallow on chmod(parent dir) / chmod(file) and the
# json.dump-fails-then-close-fd-and-reraise path. These never fire under
# normal CI (the writes succeed, chmod succeeds) but they exist for a
# reason — a partial filesystem or a tmpfs without permission semantics
# should not crash the tool.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only chmod path")
def test_save_accounts_swallows_chmod_oserror_on_parent_dir(config, monkeypatch):
    """First chmod call (parent dir, mode 0o700) raises OSError → swallow."""
    real_chmod = os.chmod
    calls = []

    def patched_chmod(path, mode):
        calls.append((str(path), mode))
        if mode == 0o700:
            raise OSError("simulated parent-dir chmod failure")
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", patched_chmod)

    # Must not raise.
    email_mcp._save_accounts([{"id": "x"}])
    # Parent-dir chmod was attempted.
    assert any(mode == 0o700 for _, mode in calls)
    # File still written.
    assert json.loads(config.read_text()) == {"accounts": [{"id": "x"}]}


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only chmod path")
def test_save_accounts_swallows_chmod_oserror_on_file(config, monkeypatch):
    """Second chmod call (file, mode 0o600) raises OSError → swallow."""
    real_chmod = os.chmod
    file_chmod_attempts = []

    def patched_chmod(path, mode):
        if mode == 0o600:
            file_chmod_attempts.append(str(path))
            raise OSError("simulated file chmod failure")
        return real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", patched_chmod)

    email_mcp._save_accounts([{"id": "y"}])
    # Attempt was made on the file path.
    assert any(p.endswith("accounts.json") for p in file_chmod_attempts)
    # File still written despite chmod failure.
    assert json.loads(config.read_text()) == {"accounts": [{"id": "y"}]}


def test_save_accounts_closes_fd_and_reraises_on_dump_failure(config, monkeypatch):
    """``json.dump`` raises → the fd is closed in the except branch and the
    original exception propagates. Pins lines 99-104.
    """
    import json as _json

    def boom(*args, **kwargs):
        raise IOError("disk full")

    monkeypatch.setattr(_json, "dump", boom)
    # The module imports json at top level; patching the global is enough.
    monkeypatch.setattr(email_mcp, "json", _json)

    with pytest.raises(IOError, match="disk full"):
        email_mcp._save_accounts([{"id": "z"}])


# ---------------------------------------------------------------------------
# _save_accounts non-POSIX (#8 iter-7) — both chmod blocks are guarded by
# ``if os.name == "posix"``. On a Windows host both blocks are skipped and
# the function just opens/writes/closes the fd. The CI matrix runs on POSIX
# only, so we monkeypatch ``os.name`` to ``"nt"`` to exercise the skip path.
# Pins partial branches 90->95 and 105->exit.
# ---------------------------------------------------------------------------


def test_save_accounts_skips_chmod_when_os_name_is_not_posix(config, monkeypatch):
    """On a non-POSIX host (``os.name != "posix"``) both chmod blocks short-
    circuit. The file is still written; ACLs are the caller's responsibility
    on Windows. Pins partials 90->95 and 105->exit.

    Strategy: instead of monkeypatching ``os.name`` directly (which would
    break ``pathlib`` because Path internally reads ``os.name`` to pick its
    flavor), we replace ``email_mcp.os`` with a thin shim whose ``name``
    attribute is ``"nt"`` and which delegates everything else. The
    production code uses ``email_mcp.os.name`` for its two checks, so the
    shim intercepts both — but the pathlib import was already resolved at
    module load with the real os.name (``"posix"``) so Path() still works.
    """
    import os as _real_os
    chmod_calls = []

    class _OsShim:
        name = "nt"  # the only value we override

        def __getattr__(self, attr):
            return getattr(_real_os, attr)

    shim = _OsShim()

    # Override chmod via the shim too, so we can assert it's never called.
    def _track_chmod(path, mode):
        chmod_calls.append((str(path), mode))
        return _real_os.chmod(path, mode)
    shim.chmod = _track_chmod  # type: ignore[attr-defined]

    monkeypatch.setattr(email_mcp, "os", shim)

    email_mcp._save_accounts([{"id": "w"}])

    # Both chmod blocks were guarded by `if os.name == "posix":` → skipped.
    assert chmod_calls == []
    # Payload still landed on disk.
    assert json.loads(config.read_text()) == {"accounts": [{"id": "w"}]}
