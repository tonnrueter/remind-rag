"""Tool-output checks: each test replays a failure from audit round 02 (eval/rounds/round-02.md) and asserts
that the MCP tools now return the information whose absence led to it. No LLM involved.

    uv run pytest tests            (uses the newest data/remind-bge*.db, or REMIND_RAG_DB)
"""

from __future__ import annotations

import re

import pytest

from remind_rag import server


def section(text: str, title: str) -> str:
    """The text of one '### title' section of get_symbol output."""
    m = re.search(rf"### {re.escape(title)}.*?(?=\n### |\n## |\Z)", text, re.S)
    return m.group(0) if m else ""


# ------------------------------------------------------------------ default awareness

def test_non_default_realization_is_marked():
    # Q1/Q12: v31_fuSlack only exists in timeDepGrades / MOFEX; default fossil realization is grades2poly
    out = server.search("v31_fuSlack early retirement slack", k=5)
    assert "realization timeDepGrades: NOT DEFAULT (default: grades2poly" in out \
        or "realization MOFEX: NOT DEFAULT (default: grades2poly" in out


def test_declaration_only_in_non_default_realizations():
    out = server.get_symbol("v31_fuSlack")
    assert "only in non-default realizations (default: grades2poly)" in out


def test_runtime_guard_inactive_by_default():
    # Q10: the q_co2eq.m overwrite in core/postsolve.gms only runs with cm_emiscen = 6 (default 9)
    out = server.get_symbol("pm_taxCO2eqSum", max_uses=60)
    assigned = section(out, "Assigned")
    assert "core/presolve.gms:10" in assigned
    postsolve = [line for line in assigned.split("\n- ") if line.startswith("core/postsolve.gms:51")]
    assert postsolve and "cm_emiscen eq 6) → inactive by default" in postsolve[0]


def test_roles_separate_declaring_and_assigning_modules():
    # Q10: 21_tax reads pm_taxCO2eqSum but never assigns it
    out = server.get_symbol("pm_taxCO2eqSum", max_uses=60)
    assert "21_tax" not in section(out, "Assigned").splitlines()[0]
    assert "declared in (interface owner): 46_carbonpriceRegi" in out


def test_ifthen_condition_inactive_by_default():
    # Q9/Q13-type: code under $ifthen of a switch that is off by default
    out = server.get_symbol("pm_taxCO2eqSum", max_uses=60)
    assert 'not "%cm_CO2PriceLimit%" == "off" → inactive by default' in out


def test_budget_equation_marked_inactive():
    # Q9: q_budgetCO2eqGlob only binds with cm_emiscen = 6
    out = server.get_symbol("q_budgetCO2eqGlob")
    assert "Generated for" in out and "cm_emiscen" in out
    assert re.search(r"cm_emiscen (eq|=) 6  \(default cm_emiscen = 9\) → false", out)


def test_climate_module_default_marked():
    # Q7/Q9/Q14: climate is off by default
    out = server.get_module("15")
    assert "realization off" in out and "[DEFAULT]" in out
    assert re.search(r"realization magicc7_ar6\s+\[not default\]", out)


def test_carbonprice_default_marked():
    # Q4: NPi2025 is the default carbonprice realization; functionalForm is not
    out = server.get_module("45")
    assert re.search(r"realization NPi2025\s+\[DEFAULT\]", out)
    assert re.search(r"realization functionalForm\s+\[not default\]", out)


# ------------------------------------------------------------------ equations

def test_multiline_equation_head_is_an_equation():
    # parser gap #25: q24_capTrade's $-condition spans several lines before `..`
    out = server.get_symbol("q24_capTrade")
    assert "## Equation definition" in out


def test_equation_domain_condition_shown():
    # Q18: q33_DAC_FEdemand is generated from fe2cdr, independent of cm_33DAC
    out = server.get_symbol("q33_DAC_FEdemand")
    head = re.search(r"Generated for \(domain and \$-condition\): (.*)", out)
    assert head and "fe2cdr" in head.group(1)


def test_long_equation_not_split():
    # qm_budget (01_macro) was longer than the chunk cap and came back in two halves
    rows = server.idx.db.execute("SELECT text FROM chunks WHERE kind = 'equation' AND name = 'qm_budget'").fetchall()
    assert rows and all(".." in r["text"] and r["text"].rstrip().endswith(";") for r in rows)


def test_index_has_no_parser_warnings():
    import json

    warnings = json.loads(server.idx.meta.get("check_warnings", "null") or "null")
    assert warnings == [], warnings


# ------------------------------------------------------------------ interface consumers (Q11)

@pytest.mark.parametrize("realization", ["NDCplus", "regiCarbonPrice", "heat"])
def test_capEarlyReti_consumers(realization):
    out = server.get_symbol("vm_capEarlyReti", max_uses=80)
    assert f"/{realization}/" in out or f"/{realization}]" in out
