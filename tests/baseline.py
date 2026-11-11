"""The REMIND state the content tests were written against.

Tests marked `remind` (tool output, output budgets, tests/reference/) assert on real REMIND lines, counts and
scenarios, so they only mean something on an index built from this commit; on any other index they are skipped
(conftest.py), and the header of the test run says why. test_sweeps.py reads REMIND at this commit from git.

Moving the baseline is a deliberate step of its own, never part of a code change: build the index from the new
commit, set REMIND_COMMIT, run `uv run python tests/test_reference_output.py --update`, read the diffs, then fix
the hand-written tests whose REMIND lines moved or are gone (FINDINGS 20 did this for 8d9052ef8 -> 1e2dbd972).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from remind_rag import store

REMIND_COMMIT = "1e2dbd9724d9d225e35cda1fcbad9f8f3ea9139a"  # develop, 3.7.1.dev149, 2026-11-11 (index v13)
# the REMIND repository next to rag/, as in the workspace; only test_sweeps.py reads it
REMIND_REPO = Path(__file__).resolve().parents[2] / "remind"


def index_commit() -> str | None:
    """The REMIND commit the index under test was built from (store.default_db(): REMIND_RAG_DB or the pinned one)."""
    with sqlite3.connect(store.default_db()) as db:
        row = db.execute("SELECT value FROM meta WHERE key = 'remind_commit'").fetchone()
    return row[0] if row else None


def mismatch() -> str:
    """Why the content tests can't run on this index, or '' when they can."""
    built = index_commit()
    if built == REMIND_COMMIT:
        return ""
    return (
        f"index {store.default_db().name} is built from REMIND {(built or 'unknown')[:9]}, "
        f"the content tests expect {REMIND_COMMIT[:9]} (tests/baseline.py)"
    )
