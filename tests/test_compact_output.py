"""Compact tool output (#35): the default call returns a summary that Claude can drill into.

Written before the change. Part A pins what must stay in the SHORT default output (what Claude actually sees),
part B the size budgets, part C the drill-down calls that return the details the summary leaves out.
Every round-02 / smoke-test failure in test_tool_output.py must keep passing unchanged.

Size budgets in characters; ~2 chars/token for GAMS-heavy output, so 4,000 chars ≈ 2k tokens at most.
"""

from __future__ import annotations

import re

import pytest

from remind_rag import server

SYMBOL_BUDGET = 4_000
MODULE_BUDGET = 6_000
SEARCH_BUDGET = 5_000


# ------------------------------------------------------------------ A: information that must survive

def test_symbol_summary_keeps_owner_and_computing_modules():
    out = server.get_symbol("pm_taxCO2eqSum")
    assert "declared in (interface owner): 46_carbonpriceRegi" in out
    # where the value is computed in a default run: the one ✓ assignment stays visible as a line
    assert "core/presolve.gms:10" in out


def test_symbol_summary_lists_every_non_default_use_individually():
    # Q10: the overwrite in core/postsolve.gms:51 only runs with cm_emiscen = 6; a count alone would hide that
    out = server.get_symbol("pm_taxCO2eqSum")
    line = next((x for x in out.splitlines() if "core/postsolve.gms:51" in x), "")
    assert "⚠" in line or "cm_emiscen eq 6) → inactive by default" in out


def test_symbol_summary_counts_uses_per_role_and_file():
    # vm_deltaCap: 108 assignments, 40 of them in core/bounds.gms; the summary says so instead of listing them
    out = server.get_symbol("vm_deltaCap")
    assert re.search(r"[Aa]ssigned.*\b108\b", out)
    assert re.search(r"core/bounds\.gms\D{0,20}\b4\d\b", out)  # per-file count
    assert "✓" in out  # the positive default mark survives in the summary (smoke test 2026-09-26)


def test_symbol_summary_names_how_to_get_details():
    out = server.get_symbol("vm_deltaCap")
    assert 'role="assigned"' in out  # the drill-down call is spelled out for Claude


def test_module_summary_keeps_default_and_alternatives():
    out = server.get_module("45")
    assert re.search(r"NPi2025\s+\[DEFAULT\]", out)
    for alt in ("functionalForm", "temperatureNotToExceed", "NDC", "exogenous"):
        assert re.search(rf"{alt}\s+\[not default\]", out), alt


def test_module_summary_keeps_alarming_preconditions():
    # smoke test answer 3 missed that temperatureNotToExceed can't compile; that verdict stays in the summary
    out45 = server.get_module("45")
    line = next((x for x in out45.splitlines() if "temperatureNotToExceed" in x and "CAN NEVER BE MET" in x), "")
    assert line
    out46 = server.get_module("46")
    assert re.search(r"netZero.*aborts under the defaults", out46)


def test_search_keeps_status_and_location():
    out = server.search("v31_fuSlack early retirement slack", k=5)
    assert "NOT DEFAULT (default: grades2poly" in out  # Q1/Q12, as in test_tool_output
    assert re.search(r"\.gms:\d+", out)


def test_search_points_to_the_rest_of_a_cut_chunk():
    out = server.search("how is the carbon price turned into tax revenue")
    assert re.search(r"more lines?.*\.gms:\d+[–-]\d+", out)  # "… (N more lines, path:L1–L2)"


# ------------------------------------------------------------------ B: size budgets for the default call

@pytest.mark.parametrize("name", ["vm_deltaCap", "vm_cap", "pm_taxCO2eqSum", "vm_co2eq"])
def test_symbol_budget(name):
    assert len(server.get_symbol(name)) <= SYMBOL_BUDGET


@pytest.mark.parametrize("module", ["45_carbonprice", "33_carbonRemoval", "15_climate", "47_regipol"])
def test_module_budget(module):
    assert len(server.get_module(module)) <= MODULE_BUDGET


@pytest.mark.parametrize("query", ["how is the carbon price turned into tax revenue",
                                   "early retirement of capacities", "why does the model not converge"])
def test_search_budget(query):
    assert len(server.search(query)) <= SEARCH_BUDGET


# ------------------------------------------------------------------ C: drill-down returns what the summary omits

def test_symbol_drill_down_by_role_and_module():
    out = server.get_symbol("vm_deltaCap", role="assigned", module="core")
    assert "core/bounds.gms:44" in out and "core/bounds.gms:97" in out
    assert "05_initialCap" not in out  # filtered to core


def test_module_drill_down_by_realization():
    out = server.get_module("45", realization="functionalForm")
    assert re.search(r"branches on \(compile time.*cm_taxCO2_functionalForm \(linear\)", out)
    assert "other abort checks (12 met by the defaults)" in out


# ------------------------------------------------------------------ answer comparison 2026-10-01 (rounds 90/91)

def test_per_file_counts_carry_default_status():
    # Q11 on the compact server: "modules/70_water/heat/output.gms 1" had no mark, so the answer left open whether
    # 70_water/heat (the default) is affected; "core/bounds.gms 6" next to one ⚠ line read as "all core bounds gated"
    out = server.get_symbol("vm_capEarlyReti")
    assert re.search(r"core/bounds\.gms 6 \(5 ✓, 1 ⚠\)", out)
    assert re.search(r"modules/70_water/heat/output\.gms:\d+ .*✓ default realization", out)  # 1-line role in full
