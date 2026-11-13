"""Tool-output checks: each test replays a failure from audit round 02 (eval/rounds/round-02.md) and asserts
that the MCP tools now return the information whose absence led to it. No LLM involved.

    uv run pytest tests            (uses data/remind-bge.db, or REMIND_RAG_DB)
"""

from __future__ import annotations

import json
import re

import pytest

from remind_rag import server

pytestmark = pytest.mark.remind  # real REMIND lines and counts: only on an index of the baseline commit


def section(text: str, title: str) -> str:
    """The text of one '### title' section of get_symbol output."""
    m = re.search(rf"### {re.escape(title)}.*?(?=\n### |\n## |\Z)", text, re.S)
    return m.group(0) if m else ""


# ------------------------------------------------------------------ default awareness


def test_non_default_realization_is_marked():
    # Q1/Q12: v31_fuSlack only exists in timeDepGrades / MOFEX; default fossil realization is grades2poly
    out = server.search("v31_fuSlack early retirement slack", k=5)
    assert (
        "realization timeDepGrades: NOT DEFAULT (default: grades2poly" in out
        or "realization MOFEX: NOT DEFAULT (default: grades2poly" in out
    )


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


def test_elseif_head_of_the_default_branch_runs():
    # R8: all three c_geoStorPotScen branch heads were ⚠, the default one (eq 3) included
    out = server.get_switch("c_geoStorPotScen")
    assert "core/datainput.gms:1210 ✓ runs by default  elseif (c_geoStorPotScen eq 3)," in out
    assert "core/datainput.gms:1207 ⚠" in out
    # a head after the default branch (cm_so2tax_scen = 1) holds none of that branch's code
    out = server.get_switch("cm_so2tax_scen")
    assert "modules/21_tax/on/datainput.gms:88 ✓" in out and "modules/21_tax/on/datainput.gms:90 ⚠" in out


def test_read_role_names_the_reads_in_equations():
    # a review took role="read" for every read of vm_cap; most of them sit in equations
    assert 'reads in equations, role="equation")' in server.get_symbol("vm_cap", role="read").splitlines()[0]


def test_scenario_shows_the_remind_row_and_only_names_the_magpie_side():
    # a review got only the MAgPIE row of M-SSP2-NPi-KLW (scenario_config_magpie_RIKEN.csv)
    out = server.get_scenario("M-SSP2-NPi-KLW")
    assert out.startswith("# M-SSP2-NPi-KLW  [config/scenario_config_RIKEN.csv:")
    assert out.splitlines()[-1].startswith(
        "MAgPIE side of a coupled run (MAgPIE settings): config/scenario_config_magpie"
    )


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
    out = server.get_module(module, realization=realization)  # #35: the full block is the drill-down
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


def test_config_counts_leave_out_the_magpie_side():
    # the 155 rows of scenario_config_magpie*.csv hold MAgPIE settings only; counted, each "kept the default"
    # carbonprice (NPi2025: 187 instead of 32 of 387)
    rows = server.idx.db.execute("SELECT path FROM scenarios").fetchall()
    remind = sum("scenario_config_magpie" not in r[0] for r in rows)
    assert remind < len(rows)
    out = server.get_module("45")
    assert f"in practice: 32 of {remind} scenario configs select it" in out
    assert f"296 of {remind} configs" in out


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
    rows = server.idx.db.execute(
        "SELECT name FROM symbol_uses WHERE path = 'core/postsolve.gms' AND line = 55"
    ).fetchall()
    assert {"carbonprice", "climate"}.isdisjoint({r["name"] for r in rows})


def test_assignment_with_equals_on_next_line():
    # p70_cap_vintages(...)$(...) on one line, "=" on the next
    row = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'p70_cap_vintages' AND "
        "path = 'modules/70_water/heat/output.gms' AND line = 14"
    ).fetchone()
    assert row and row["role"] == "assigned"


def test_if_condition_is_not_an_assignment():
    # "if (cm_startyear gt 2005," followed by an Execute_Loadpoint: the if line only reads cm_startyear
    row = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'cm_startyear' AND "
        "path = 'core/preloop.gms' AND snippet LIKE 'if (cm_startyear gt 2005,%'"
    ).fetchone()
    assert row and row["role"] == "read"


# ------------------------------------------------------------------ index v4: role fixes (real REMIND lines)
# (the line-by-line cases of usage.role are in test_gams.py)


def test_bounds_with_spaced_condition_are_assignments():
    row = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'vm_cap' AND snippet LIKE "
        "'vm_cap.lo(t,regi,te,\"1\") $ (t.val >= 2030) = 1e-7;%'"
    ).fetchone()
    assert row and row["role"] == "assigned"


def test_switch_comparison_in_if_is_read():
    row = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'cm_multigasscen' AND "
        "path = 'modules/46_carbonpriceRegi/netZero/datainput.gms' AND line = 9"
    ).fetchone()
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


def test_get_module_takes_name_and_module():
    # Claude called get_module(name="30_biomass") and got a validation error (ERRORS.md, #43)
    out = server.get_module(name="45")
    assert out == server.get_module(module="45") == server.get_module("45")
    assert out.startswith("# 45_carbonprice")
    assert "name=" in server.get_module()


def test_scenario_search_keeps_code_compiled_in():
    # 80_optimization/nash/bounds.gms:11 sits under `$ifthen %emicapregi% == 'none'`, true in every config; the old
    # scenario evaluator kept the quotes ('none' != none) and ranked such code as compiled out (#42)
    q = "nash bounds emission permits trade"
    chunk = ("modules/80_optimization/nash/bounds.gms", 11)
    plain = [(r["path"], r["line_start"]) for r in server.idx.search(q, k=10)]
    scen = [(r["path"], r["line_start"]) for r in server.idx.search(q, k=10, scenario="SSP2-PkBudg1000")]
    assert chunk in plain
    assert chunk in scen


# ------------------------------------------------------------------ #51: switch declarations in main.gms


def test_switch_declaration_in_main_gms_is_declared():
    # main.gms declares its switches inside switch chunks: `parameter cm_nash_mode "…" ;` then `cm_nash_mode = 2;`
    rows = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'cm_nash_mode' AND path = 'main.gms' ORDER BY line"
    ).fetchall()
    assert [r["role"] for r in rows] == ["declared", "assigned"]
    assert "main.gms" in section(server.get_symbol("cm_nash_mode"), "Declared").splitlines()[0]


def test_switch_description_mentions_are_not_uses():
    # cm_taxCO2_peakBudgYr's description names cm_peakBudgYr; that is not a read of cm_peakBudgYr
    row = server.idx.db.execute(
        "SELECT count(*) FROM symbol_uses WHERE name = 'cm_peakBudgYr' AND path = 'main.gms' "
        "AND snippet LIKE 'cm_taxCO2_peakBudgYr%'"
    ).fetchone()
    assert row[0] == 0


@pytest.mark.parametrize(
    ("name", "neighbour", "doc"),
    [
        # R8: a blank line between the assignment and its doc gave c_export_tax_scen this text
        ("c_geoStorPotScen", "c_export_tax_scen", "upper bound of the total geological storage potential"),
        # the doc repeats the switch's own name, which read as the start of the next switch's doc
        ("cm_flex_tax", "cm_H2targets", "inflexible technologies get a tax"),
        ("cm_FlexTaxFeedback", "cm_VRE_supply_assumptions", "has to be matched by electrictiy price increase"),
    ],
)
def test_doc_after_the_definition_stays_with_its_switch(name, neighbour, doc):
    assert doc in server.get_switch(name)
    assert doc not in server.get_switch(neighbour)


def test_search_shows_no_switch_test_from_a_comment():
    # q_limitCapEarlyReti's `!!` comment mentions "cm_startyear = 2005"; its code tests no switch
    out = server.search("q_limitCapEarlyReti", k=1)
    assert "switch tests in this code" not in out


# ------------------------------------------------------------------ indented `*` and $onText (FINDINGS 18)


def test_indented_star_line_is_a_use():
    # `        * pm_damage(tall2,regi2) * ...` continues a formula; only `*` in column 1 is a GAMS comment
    out = server.get_symbol("pm_damage", module="51")
    assert "modules/51_internalizeDamages/KWlikeItr/postsolve.gms:24" in out


def test_equation_member_on_indented_star_line():
    # q_costOM multiplies by vm_prodSe on a line starting with `*` (core/equations.gms)
    out = server.get_symbol("vm_prodSe", max_uses=80)
    assert "q_costOM" in section(out, "Used in equations")


def test_switched_off_code_is_no_use():
    # se_trade/presolve.gms assigns p24_XportsRegi only between $onText and $offText
    out = server.get_symbol("p24_XportsRegi")
    assert "### Assigned" not in out and "presolve.gms" not in out


# ------------------------------------------------------------------ assignments the roles missed (FINDINGS 19)


def assigned_at(name: str) -> set[tuple[str, int]]:
    rows = server.idx.db.execute(
        "SELECT path, line FROM symbol_uses WHERE name = ? COLLATE NOCASE AND role = 'assigned'", (name,)
    )
    return {(r["path"], r["line"]) for r in rows}


def test_condition_without_brackets_on_the_left():
    # `p33_FE_limit(...)$p33_shfetot_up(...) = ...`: the $name's own index used to hide the `=`
    assert {
        ("modules/33_carbonRemoval/portfolio/preloop.gms", 10),
        ("modules/33_carbonRemoval/portfolio/postsolve.gms", 13),
    } <= assigned_at("p33_FE_limit")
    assert ("core/bounds.gms", 75) in assigned_at("vm_cap")  # vm_cap.lo(...) $ pm_histCap(...) = ...


def test_tab_before_equals():
    assert ("main.gms", 1161) in assigned_at("cm_sccConvergence")  # `cm_sccConvergence<TAB>= 0.05;`


def test_one_line_if_assigns_under_its_condition():
    # `$if %cm_rcp_scen% == "rcp45" s15_rcpCluster = 0;` assigns, and only under rcp45
    path = "modules/15_climate/magicc7_ar6/datainput.gms"
    assert (path, 13) in assigned_at("s15_rcpCluster")
    row = server.idx.db.execute(
        "SELECT conditions FROM symbol_uses WHERE name = 's15_rcpCluster' AND path = ? AND line = 13", (path,)
    ).fetchone()
    assert "rcp45" in row["conditions"]


def test_declaration_with_data_assigns():
    assert ("modules/32_power/IntC/datainput.gms", 24) in assigned_at("p32_grid_factor")  # / $include ... /
    assert ("core/datainput.gms", 60) in assigned_at("f_pop")  # table + $include
    out = server.get_symbol("p32_grid_factor")
    assert "datainput.gms:24" in section(out, "Assigned")


def test_switch_in_declaration_data_is_read():
    # parameter p_new_incolearn(all_te) "..." / %cm_incolearn% /;
    row = server.idx.db.execute(
        "SELECT role FROM symbol_uses WHERE name = 'cm_incolearn' AND path = 'core/datainput.gms' AND line = 267"
    ).fetchone()
    assert row and row["role"] == "read"


# ------------------------------------------------------------------ #64: which configs set a switch (reach)
# The reviews R1–R8 answered "how many configs does this bug hit" with hand-written SQL on scenarios.settings
# (reviews/EFFICIENCY_LEARNINGS.md). Each test replays one of those queries as one get_switch call. Not covered
# by one call: several switches at once (R7's dead-switch list, R6's 21 switches: one call each, in parallel)
# and a substring of any value (R5: rows mentioning "grossEnCO2"; that is search(..., kind="scenario")).


def configs(name: str, value: str | None = None, also: str | None = None) -> str:
    out = server.get_switch(name, value=value, also=also)
    return out[out.index("## In scenario configs") :]


def remind_rows_setting(key: str) -> tuple[int, int]:
    """(REMIND config rows, rows with a cell for key), counted here without the server's code."""
    rows = [
        json.loads(r[1])
        for r in server.idx.db.execute("SELECT path, settings FROM scenarios")
        if "scenario_config_magpie" not in r[0]
    ]
    return len(rows), sum(any(k.strip().lower() == key.lower() for k in s) for s in rows)


@pytest.mark.parametrize("name", ["c_tech_CO2capturerate", "cm_emiscen", "cm_CESMkup_ind", "cm_TCssp"])
def test_switch_shows_how_many_configs_set_it(name):
    # R1: get_switch("c_tech_CO2capturerate") said nothing about config use; R7/R8 checked dead switches by SQL
    total, n = remind_rows_setting(name)
    assert f"## In scenario configs: set in {n} of {total} REMIND configs" in configs(name)


def test_config_use_in_one_file_names_the_file():
    # R1: the column exists in one csv only
    out = configs("c_tech_CO2capturerate")
    assert "(scenario_config_21_EU11_2040_target.csv 200)" in out
    assert "off (default): 187 (187 leave it empty)  [H12 131 · 21_EU11 56]" in out


def test_never_set_switch_says_all_run_with_the_default():
    # R7: cm_TCssp is set by no config (dead switch)
    assert "set in 0 of 387 REMIND configs; all run with the default (SSP2)" in configs("cm_TCssp")


def test_value_counts_by_region_mapping():
    # R5 (B23): cm_CESMkup_ind = Elec_Push in 242 rows, 200 in one file; R6/R8 (B25, B35): H12 vs EU21
    out = configs("cm_CESMkup_ind", value="Elec_Push")
    assert "cm_CESMkup_ind = Elec_Push in 242 of 387 REMIND configs" in out
    assert "scenario_config_21_EU11_2040_target.csv 200" in out and "[21_EU11 204 · H12 38]" in out
    assert "- configs: SSP2-PkBudg650 (scenario_config.csv:9)" in out


def test_long_values_stay_distinct():
    # R4 (cm_emiMktTarget, 227 rows): the values differ only after the first 60 characters (2.092 vs 2.189)
    out = configs("cm_emiMktTarget")
    assert "intraRegBunker 2.092," in out and "intraRegBunker 2.189," in out


def test_configs_split_by_a_second_setting():
    # R8 (B41): carbonprice = exogenous rows without path_gdx_carbonprice
    out = configs("carbonprice", value="exogenous", also="path_gdx_carbonprice")
    assert "carbonprice = exogenous in 3 of 387 REMIND configs; by path_gdx_carbonprice" in out
    assert "- (unset): 2  [all H12]  e.g. SSP2_dls1 (scenario_config_JustMIP.csv:6)" in out


def test_configs_split_by_two_settings():
    # R8 (B44): NDC rows whose path_gdx_ref differs from path_gdx_bau
    out = configs("carbonprice", value="NDC", also="path_gdx_ref,path_gdx_bau")
    assert "- SSP2-NPi2025 | SSP2-NPi2025: 11" in out
    assert "- SSP2-NDC | SSP2-NPi2025: 1  [all H12]  e.g. SSP2-NDC-LTS-my (scenario_config.csv:5)" in out
    # R8: cm_iterative_target_adj = 3 rows by carbonprice and cm_emiscen
    out = configs("cm_iterative_target_adj", value="3", also="carbonprice,cm_emiscen")
    assert "- NDC | 9 (default): 20" in out and "- NPi | 9 (default): 6  [all 21_EU11]" in out


def test_non_default_rows_with_other_settings():
    # R7: rows with damages or internalizeDamages switched on, with their climate realization
    out = configs("damages", also="internalizeDamages,climate")
    assert "- KotzWenz | KotzWenzItr | magicc7_ar6: 2  [all H12]" in out
    assert (
        "- KotzWenz | off (default) | magicc7_ar6: 1  [all H12]  e.g. M-SSP2-NPi-KLW (scenario_config_RIKEN.csv:5)"
        in out
    )


def test_config_column_that_is_no_switch():
    # R6/R8: regionmapping and path_gdx_* are read by the R start scripts, not main.gms
    out = server.get_switch("regionmapping", also="extramappings_historic")
    assert out.startswith("## regionmapping  scenario-config column, not a main.gms switch")
    assert "default (config/default.cfg): config/regionmappingH12.csv" in out
    assert "- ./config/regionmapping_21_EU11.csv | ./config/extramapping_EU27.csv: 256" in out
    assert 'config/regionmappingH12.csv (default) | "" (default): 131' in out
    assert "set in 3 of 387 REMIND configs" in server.get_switch("path_gdx_carbonprice")


def test_unknown_second_setting_is_flagged():
    out = configs("carbonprice", also="cm_no_such_switch")
    assert "- note: cm_no_such_switch is no main.gms switch and no config sets it" in out
