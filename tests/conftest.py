"""Shared pytest config — make `servers/` importable for tests."""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "servers"))

os.environ["EMAIL_ACCOUNTS_CONFIG"] = str(ROOT / "tests" / ".test-accounts.json")
