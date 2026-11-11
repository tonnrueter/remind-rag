"""Tests marked `remind` run only on an index built from the baseline commit (baseline.py); elsewhere, e.g. on a
deployment that indexes its own REMIND checkout, they are skipped and the rest of the suite still runs."""

from __future__ import annotations

import baseline
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "remind: asserts on an index built from the baseline REMIND commit (baseline.py)"
    )


def pytest_report_header(config):
    reason = baseline.mismatch()
    if reason:
        return f"REMIND content tests SKIPPED: {reason}"
    return f"REMIND content tests run against baseline {baseline.REMIND_COMMIT[:9]}"


def pytest_collection_modifyitems(config, items):
    reason = baseline.mismatch()
    if not reason:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("remind"):
            item.add_marker(skip)
