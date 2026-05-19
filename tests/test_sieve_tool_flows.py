"""Tests for the ManageSieve tool flows: ``email_sieve_list``,
``email_sieve_get``, ``email_sieve_put``, ``email_sieve_activate``,
``email_sieve_delete``, and ``email_sieve_rename``.

A tiny ``_FakeSieve`` records every method the tool layer calls (``listscripts``,
``getscript``, ``putscript``, ``setactive``, ``deletescript``, ``logout``) and
lets each test pre-seed scripts or canned error returns.
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
}


class _FakeSieve:
    """Minimal in-memory ManageSieve client."""

    def __init__(self):
        # name -> content
        self.scripts = {}
        self.active = None
        self.logged_out = False
        # When set, the next call to the named method returns this status
        # instead of "OK". Mostly used to simulate server errors.
        self.next_error = {}

    def _pop_error(self, method):
        if method in self.next_error:
            return self.next_error.pop(method)
        return None

    def listscripts(self):
        err = self._pop_error("listscripts")
        if err:
            return (err, None)
        return ("OK", [(name, name == self.active) for name in self.scripts])

    def getscript(self, name):
        err = self._pop_error("getscript")
        if err:
            return (err, None)
        if name not in self.scripts:
            return ("NO", None)
        return ("OK", self.scripts[name])

    def putscript(self, name, content):
        err = self._pop_error("putscript")
        if err:
            return err
        self.scripts[name] = content
        return "OK"

    def setactive(self, name):
        err = self._pop_error("setactive")
        if err:
            return err
        if name == "":
            self.active = None
        else:
            if name not in self.scripts:
                return "NO"
            self.active = name
        return "OK"

    def deletescript(self, name):
        err = self._pop_error("deletescript")
        if err:
            return err
        if name == self.active:
            return "NO"  # match real ManageSieve: can't delete active
        if name in self.scripts:
            del self.scripts[name]
            return "OK"
        return "NO"

    def logout(self):
        self.logged_out = True


@pytest.fixture
def fake(monkeypatch):
    """Install a fresh _FakeSieve and stub _get_account / _sieve_connect."""
    sieve = _FakeSieve()
    monkeypatch.setattr(email_mcp, "_get_account", lambda aid: ACCT)
    monkeypatch.setattr(email_mcp, "_sieve_connect", lambda acct: sieve)
    return sieve


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# email_sieve_list
# ---------------------------------------------------------------------------

def test_list_renders_scripts_with_active_marker(fake):
    fake.scripts = {"vacation": "...", "spam-rules": "..."}
    fake.active = "spam-rules"

    result = run(email_mcp.email_sieve_list(email_mcp.SieveListInput(account_id=ACCT_ID)))
    assert "# Sieve Scripts" in result
    assert "`vacation`" in result
    assert "`spam-rules` **(active)**" in result
    assert fake.logged_out is True


def test_list_empty_friendly_message(fake):
    result = run(email_mcp.email_sieve_list(email_mcp.SieveListInput(account_id=ACCT_ID)))
    assert f"No Sieve scripts on {ACCT_ID}" in result


def test_list_surfaces_server_error(fake):
    fake.next_error["listscripts"] = "NO"
    result = run(email_mcp.email_sieve_list(email_mcp.SieveListInput(account_id=ACCT_ID)))
    assert "Error listing scripts" in result


# ---------------------------------------------------------------------------
# email_sieve_get
# ---------------------------------------------------------------------------

def test_get_returns_script_in_code_fence(fake):
    fake.scripts["vacation"] = 'require "vacation";\nvacation "OOO";'
    result = run(email_mcp.email_sieve_get(email_mcp.SieveGetInput(
        account_id=ACCT_ID, script_name="vacation",
    )))
    assert "# Sieve Script: `vacation`" in result
    assert "```sieve" in result
    assert "vacation \"OOO\";" in result


def test_get_missing_script_returns_error(fake):
    result = run(email_mcp.email_sieve_get(email_mcp.SieveGetInput(
        account_id=ACCT_ID, script_name="does-not-exist",
    )))
    assert "Error: Could not retrieve script 'does-not-exist'" in result


# ---------------------------------------------------------------------------
# email_sieve_put
# ---------------------------------------------------------------------------

def test_put_uploads_script(fake):
    result = run(email_mcp.email_sieve_put(email_mcp.SievePutInput(
        account_id=ACCT_ID, script_name="rules",
        script_content='require "fileinto";',
        activate=False,
    )))
    assert fake.scripts["rules"] == 'require "fileinto";'
    assert fake.active is None
    assert "uploaded" in result.lower()


def test_put_with_activate_sets_active(fake):
    result = run(email_mcp.email_sieve_put(email_mcp.SievePutInput(
        account_id=ACCT_ID, script_name="rules",
        script_content="# noop",
        activate=True,
    )))
    assert fake.active == "rules"
    assert "**active**" in result


def test_put_server_validation_failure_surfaces_error(fake):
    fake.next_error["putscript"] = "NO syntax error at line 3"
    result = run(email_mcp.email_sieve_put(email_mcp.SievePutInput(
        account_id=ACCT_ID, script_name="bad", script_content="garbage",
        activate=False,
    )))
    assert "Error uploading script" in result
    assert "bad" not in fake.scripts  # never persisted


def test_put_with_activate_warns_on_partial_failure(fake):
    """If putscript succeeds but setactive fails, the message must call that out."""
    # First putscript succeeds; setactive fails
    fake.next_error["setactive"] = "NO server busy"
    result = run(email_mcp.email_sieve_put(email_mcp.SievePutInput(
        account_id=ACCT_ID, script_name="rules", script_content="# ok",
        activate=True,
    )))
    assert "uploaded" in result.lower()
    assert "Warning" in result
    assert "activation failed" in result


# ---------------------------------------------------------------------------
# email_sieve_activate
# ---------------------------------------------------------------------------

def test_activate_sets_named_script_active(fake):
    fake.scripts = {"a": "...", "b": "..."}
    result = run(email_mcp.email_sieve_activate(email_mcp.SieveActivateInput(
        account_id=ACCT_ID, script_name="a",
    )))
    assert fake.active == "a"
    assert "now the active filter" in result


def test_activate_empty_name_deactivates_all(fake):
    fake.scripts = {"a": "..."}
    fake.active = "a"
    result = run(email_mcp.email_sieve_activate(email_mcp.SieveActivateInput(
        account_id=ACCT_ID, script_name="",
    )))
    assert fake.active is None
    assert "deactivated" in result.lower()


# ---------------------------------------------------------------------------
# email_sieve_delete
# ---------------------------------------------------------------------------

def test_delete_removes_inactive_script(fake):
    fake.scripts = {"old": "..."}
    result = run(email_mcp.email_sieve_delete(email_mcp.SieveDeleteInput(
        account_id=ACCT_ID, script_name="old",
    )))
    assert "old" not in fake.scripts
    assert "deleted" in result.lower()


def test_delete_active_script_returns_error_with_hint(fake):
    fake.scripts = {"active": "..."}
    fake.active = "active"
    result = run(email_mcp.email_sieve_delete(email_mcp.SieveDeleteInput(
        account_id=ACCT_ID, script_name="active",
    )))
    assert "Error deleting script" in result
    assert "deactivate it first" in result
    assert "active" in fake.scripts  # not deleted


# ---------------------------------------------------------------------------
# email_sieve_rename
# ---------------------------------------------------------------------------

def test_rename_inactive_script(fake):
    fake.scripts = {"old": "content here"}
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="old", new_name="new",
    )))
    assert "renamed" in result.lower()
    assert "new" in fake.scripts
    assert "old" not in fake.scripts
    assert fake.scripts["new"] == "content here"


def test_rename_active_script_preserves_active_flag(fake):
    """The new script must end up active when the old one was."""
    fake.scripts = {"old": "content"}
    fake.active = "old"
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="old", new_name="new",
    )))
    assert fake.active == "new"
    assert "active" in result.lower()
    assert "old" not in fake.scripts


def test_rename_missing_source_returns_error(fake):
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="ghost", new_name="new",
    )))
    assert "Could not retrieve script 'ghost'" in result


# ---------------------------------------------------------------------------
# Outer-except tail sweep (coverage iter-4, issue #8)
#
# Every sieve tool wraps its body in ``try: ... except Exception as e:
# return f"Error: {e}"``. The happy-path tests above exercise the inner
# try; these tests inject a raising ``_sieve_connect`` so the outer except
# fires.
# ---------------------------------------------------------------------------


def _raising_sieve(monkeypatch, exc):
    """Override _sieve_connect to raise ``exc`` on the next call."""
    def boom(acct):
        raise exc
    monkeypatch.setattr(email_mcp, "_sieve_connect", boom)


def test_sieve_list_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("sieve unreachable"))
    result = run(email_mcp.email_sieve_list(email_mcp.SieveListInput(account_id=ACCT_ID)))
    assert result.startswith("Error:")
    assert "sieve unreachable" in result


def test_sieve_get_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("sieve get boom"))
    result = run(email_mcp.email_sieve_get(email_mcp.SieveGetInput(
        account_id=ACCT_ID, script_name="anything",
    )))
    assert result.startswith("Error:")
    assert "sieve get boom" in result


def test_sieve_put_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("put boom"))
    result = run(email_mcp.email_sieve_put(email_mcp.SievePutInput(
        account_id=ACCT_ID, script_name="x", script_content="# noop", activate=False,
    )))
    assert result.startswith("Error:")
    assert "put boom" in result


def test_sieve_activate_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("activate boom"))
    result = run(email_mcp.email_sieve_activate(email_mcp.SieveActivateInput(
        account_id=ACCT_ID, script_name="x",
    )))
    assert result.startswith("Error:")
    assert "activate boom" in result


def test_sieve_activate_inner_setactive_failure_surfaces(fake):
    """Inner arm: setactive returns non-OK → ``Error activating script: <code>``."""
    fake.scripts = {"a": "..."}
    fake.next_error["setactive"] = "NO server busy"
    result = run(email_mcp.email_sieve_activate(email_mcp.SieveActivateInput(
        account_id=ACCT_ID, script_name="a",
    )))
    assert "Error activating script" in result
    assert "NO server busy" in result


def test_sieve_delete_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("delete boom"))
    result = run(email_mcp.email_sieve_delete(email_mcp.SieveDeleteInput(
        account_id=ACCT_ID, script_name="x",
    )))
    assert result.startswith("Error:")
    assert "delete boom" in result


def test_sieve_rename_outer_except_returns_error(fake, monkeypatch):
    _raising_sieve(monkeypatch, RuntimeError("rename boom"))
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="old", new_name="new",
    )))
    assert result.startswith("Error:")
    assert "rename boom" in result


def test_sieve_rename_putscript_failure_short_circuits(fake):
    """Inner arm: putscript fails → ``Error uploading script with new name: …``."""
    fake.scripts = {"old": "content"}
    fake.next_error["putscript"] = "NO syntax"
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="old", new_name="new",
    )))
    assert "Error uploading script with new name" in result
    assert "NO syntax" in result
    assert "new" not in fake.scripts


def test_sieve_rename_deletescript_failure_surfaces_partial(fake):
    """Inner arm: putscript ok but deletescript fails → partial-failure message."""
    fake.scripts = {"old": "content"}
    # Old is NOT active, so deletescript runs without the active-script gate.
    fake.next_error["deletescript"] = "NO cant delete"
    result = run(email_mcp.email_sieve_rename(email_mcp.SieveRenameInput(
        account_id=ACCT_ID, old_name="old", new_name="new",
    )))
    assert "could not " in result.lower()
    assert "old" in result  # old name surfaces in the partial-failure string
    assert "new" in fake.scripts  # new script WAS uploaded
    assert "NO cant delete" in result


# ---------------------------------------------------------------------------
# Inner ``except Exception: pass`` swallow on ``conn.logout()`` — iter-5
#
# Iter-4 hit the outer ``except Exception as e:`` by raising in
# ``_sieve_connect``. That bypasses the inner ``finally`` so the logout
# swallow stays unhit. These tests configure ``logout()`` itself to raise
# on a successful happy-path call; the swallow catches it and the user
# still sees the success string.
# ---------------------------------------------------------------------------


def _make_sieve_logout_raiser(fake, exc):
    def _raise():
        raise exc
    fake.logout = _raise


def test_sieve_list_swallows_logout_exception(fake):
    fake.scripts = {"a": "..."}
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_list(
        email_mcp.SieveListInput(account_id=ACCT_ID)
    ))
    assert "# Sieve Scripts" in result
    assert not result.startswith("Error:")


def test_sieve_get_swallows_logout_exception(fake):
    fake.scripts = {"a": "# noop"}
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_get(
        email_mcp.SieveGetInput(account_id=ACCT_ID, script_name="a")
    ))
    assert "```sieve" in result
    assert not result.startswith("Error:")


def test_sieve_put_swallows_logout_exception(fake):
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_put(
        email_mcp.SievePutInput(
            account_id=ACCT_ID, script_name="x", script_content="# noop", activate=False,
        )
    ))
    assert "uploaded" in result.lower()
    assert not result.startswith("Error:")


def test_sieve_activate_swallows_logout_exception(fake):
    fake.scripts = {"a": "..."}
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_activate(
        email_mcp.SieveActivateInput(account_id=ACCT_ID, script_name="a")
    ))
    assert "active filter" in result
    assert not result.startswith("Error:")


def test_sieve_delete_swallows_logout_exception(fake):
    fake.scripts = {"a": "..."}
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_delete(
        email_mcp.SieveDeleteInput(account_id=ACCT_ID, script_name="a")
    ))
    assert "deleted" in result.lower()
    assert not result.startswith("Error:")


def test_sieve_rename_swallows_logout_exception(fake):
    fake.scripts = {"old": "# content"}
    _make_sieve_logout_raiser(fake, OSError("logout fail"))
    result = run(email_mcp.email_sieve_rename(
        email_mcp.SieveRenameInput(
            account_id=ACCT_ID, old_name="old", new_name="new",
        )
    ))
    assert "renamed" in result.lower()
    assert not result.startswith("Error:")
