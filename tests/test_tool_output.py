"""Tool-output checks: each test replays a failure from audit round 02 (eval/rounds/round-02.md) and asserts
that the MCP tools now return the information whose absence led to it. No LLM involved.

    uv run pytest tests            (uses the newest data/remind-bge*.db, or REMIND_RAG_DB)
"""

from __future__ import annotations

import re

import pytest

from remind_rag import server, usage


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


def test_module_off_by_default_is_named():
    # 46_carbonpriceRegi defaults to "none": say the module is off, not "non-default realization (default: none)"
    out = server.get_symbol("pm_taxCO2eqSum", max_uses=60)
    assert "module switched off by default (none)" in out
    assert "(default: none)" not in out


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


# ------------------------------------------------------------------ get_module: which switches steer a realization

def steering(module: str, realization: str) -> str:
    out = server.get_module(module)
    m = re.search(rf"### {re.escape(realization)}  \[.*?(?=\n### |\n## |\Z)", out, re.S)
    return m.group(0) if m else ""


def test_precondition_aborting_by_default_is_shown():
    # netZero aborts unless cm_multigasscen = 2 (default 3); all scenario configs that select it set 2
    block = steering("46", "netZero")
    assert "requires: aborts if not (cm_multigasscen = 2) → aborts under the defaults" in block
    assert "cm_multigasscen = 2 (20)" in block


def test_impossible_precondition_is_shown():
    # smoke test answer 3 missed this: temperatureNotToExceed requires a climate realization that doesn't exist
    block = steering("45", "temperatureNotToExceed")
    assert "CAN NEVER BE MET" in block and "no realization 'magicc'" in block


def test_switch_use_kinds():
    block = steering("45", "functionalForm")
    assert re.search(r"branches on \(compile time.*cm_taxCO2_functionalForm \(linear\)", block)
    assert re.search(r"tests \(run time.*cm_iterative_target_adj \(0\)", block)
    assert re.search(r"reads: .*cm_taxCO2_expGrowth \(1\.045\)", block)
    assert "other abort checks (12 met by the defaults)" in block  # nested if/elseif chain folded into one line


def test_switches_shared_by_all_realizations_listed_once():
    out = server.get_module("51")
    assert "used by all 12 realizations that use switches: cm_damages_SccHorizon (100)" in out
    assert "cm_damages_SccHorizon" not in steering("51", "COACCHitr")


def test_get_module_stays_readable():
    assert len(server.get_module("45")) < 30_000


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


# ------------------------------------------------------------------ index v3: where-used fixes

def test_bang_comments_are_not_uses():
    # core/postsolve.gms:55 names "carbonprice" and "climate" only in a !! end-of-line comment
    rows = server.idx.db.execute("SELECT name FROM symbol_uses WHERE path = 'core/postsolve.gms' AND line = 55"
                                 ).fetchall()
    assert {"carbonprice", "climate"}.isdisjoint({r["name"] for r in rows})


def test_assignment_with_equals_on_next_line():
    # p70_cap_vintages(...)$(...) on one line, "=" on the next
    row = server.idx.db.execute("SELECT role FROM symbol_uses WHERE name = 'p70_cap_vintages' AND "
                                "path = 'modules/70_water/heat/output.gms' AND line = 14").fetchone()
    assert row and row["role"] == "assigned"


def test_if_condition_is_not_an_assignment():
    # "if (cm_startyear gt 2005," followed by an Execute_Loadpoint: the if line only reads cm_startyear
    row = server.idx.db.execute("SELECT role FROM symbol_uses WHERE name = 'cm_startyear' AND "
                                "path = 'core/preloop.gms' AND snippet LIKE 'if (cm_startyear gt 2005,%'").fetchone()
    assert row and row["role"] == "read"


# ------------------------------------------------------------------ index v4: role fixes (real REMIND lines)

@pytest.mark.parametrize("line, name, expected", [
    # `=` inside if( / elseif( / $( / break$( is a comparison
    ("if(cm_nucscen = 5,", "cm_nucscen", "read"),
    ("elseif(c_co2captureEnergy = 3), !! no bio carbon capture:", "c_co2captureEnergy", "read"),
    ("if ( (pm_ffPolyCumEx(regi,enty,\"max\") = 0),", "pm_ffPolyCumEx", "read"),
    ('loop(teNoLearn(te) $ (pm_data(regi,"tech_stat",te) = 2),', "pm_data", "read"),
    ("break$(p47_implicitQttyTargetActive_iter(iteration2,ext_regi) = 1);", "p47_implicitQttyTargetActive_iter",
     "read"),
    # conditional assignments with a space after `$` are assignments
    ('vm_cap.lo(t,regi,te,"1") $ (t.val >= 2030) = 1e-7;', "vm_cap", "assigned"),
    ('p_capCum("2015",regi,te) $ (p_capCum("2015",regi,te) = 0) = fm_dataglob("ccap0",te) / card(regi);',
     "p_capCum", "assigned"),
    ('pm_cesdata_sigma(ttot,in)$ (pm_ttot_val(ttot) le 2025  AND sameAs(in, "inco")) = 0.1;', "pm_cesdata_sigma",
     "assigned"),
    # the loop variable of a for loop is assigned
    ("for (sm_tmp = sm_tmp downto 0,", "sm_tmp", "assigned"),
    ("if(cond, pm_x(regi) = 1);", "pm_x", "assigned"),
])
def test_role(line, name, expected):
    assert usage.role(line, name) == expected


def test_bounds_with_spaced_condition_are_assignments():
    row = server.idx.db.execute("SELECT role FROM symbol_uses WHERE name = 'vm_cap' AND snippet LIKE "
                                "'vm_cap.lo(t,regi,te,\"1\") $ (t.val >= 2030) = 1e-7;%'").fetchone()
    assert row and row["role"] == "assigned"


def test_switch_comparison_in_if_is_read():
    row = server.idx.db.execute("SELECT role FROM symbol_uses WHERE name = 'cm_multigasscen' AND "
                                "path = 'modules/46_carbonpriceRegi/netZero/datainput.gms' AND line = 9").fetchone()
    assert row and row["role"] == "read"


# ------------------------------------------------------------------ get_links

def links_section(text: str, title: str) -> str:
    """The text of one '## title' section of get_links output."""
    m = re.search(rf"## {re.escape(title)}.*?(?=\n## |\Z)", text, re.S)
    return m.group(0) if m else ""


def test_links_computed_from_inputs_with_modules():
    out = server.get_links("pm_taxCO2eqSum")
    computed = links_section(out, "Computed from")
    line = next(x for x in computed.splitlines() if x.startswith("- core/presolve.gms:10"))
    assert "pm_taxCO2eq" in line and "pm_taxCO2eqRegi [46_carbonpriceRegi]" in line and "⚠" not in line


def test_links_guard_conditions_are_not_inputs():
    out = server.get_links("pm_taxCO2eqSum")
    line = next(x for x in out.splitlines() if x.startswith("- core/postsolve.gms:55"))
    assert "cm_emiscen" not in line and "from pm_taxCO2eq" in line


def test_links_inactive_links_sorted_last():
    computed = links_section(server.get_links("pm_taxCO2eqSum", max_links=50), "Computed from")
    entries = computed.split("\n- ")[1:]
    flags = ["⚠" in e for e in entries]
    assert flags == sorted(flags) and not flags[0] and flags[-1]


def test_links_feeds_into_other_modules():
    feeds = links_section(server.get_links("pm_taxCO2eqSum", max_links=50), "Feeds into")
    assert "modules/21_tax/on/postsolve.gms" in feeds and "→ pm_taxrevGHG0" in feeds


def test_links_equations_with_variables():
    out = server.get_links("pm_taxCO2eqSum")
    eqs = links_section(out, "In equations")
    assert "q21_taxrevGHG" in eqs and "vm_co2eq [core]" in eqs


def test_links_of_an_equation_show_domain_condition():
    out = server.get_links("q_budgetCO2eqGlob")
    assert "generated only if (cm_emiscen=6) → inactive by default" in out
    assert "v_co2eqCum" in out and out.count("## ") == 1  # its members, not itself again


def test_links_multiline_assignment_feeds():
    # Q11: vm_capEarlyReti feeds 70_water/heat through a statement whose "=" is on the next line
    feeds = links_section(server.get_links("vm_capEarlyReti"), "Feeds into")
    assert "modules/70_water/heat/output.gms:14" in feeds and "p70_cap_vintages" in feeds


# ------------------------------------------------------------------ default status on the entry's first line

def test_links_default_realization_marked_positively():
    # smoke test 2026-09-26: q33_carbonRemovalspending (33/portfolio, the default) was called a non-default
    # alternative; the entry carried no mark, only a missing ⚠
    eqs = links_section(server.get_links("pm_taxCO2eqSum"), "In equations")
    line = next(x for x in eqs.splitlines() if x.startswith("- q33_carbonRemovalspending"))
    assert line.endswith("✓ default realization")


def test_links_non_default_marked_on_first_line():
    eqs = links_section(server.get_links("pm_taxCO2eq"), "In equations")
    line = next(x for x in eqs.splitlines() if x.startswith("- q02_taxrev_Add"))
    assert line.endswith("⚠ not in a default run")
    assert "1 run by default, 1 not in a default run" in eqs.splitlines()[0]


def test_symbol_use_lines_carry_status():
    assigned = section(server.get_symbol("pm_taxCO2eqSum", max_uses=60), "Assigned")
    assert "core/presolve.gms:10 [core/presolve] ✓ runs by default" in assigned
    assert "core/postsolve.gms:51 [core/postsolve] ⚠ not in a default run" in assigned
