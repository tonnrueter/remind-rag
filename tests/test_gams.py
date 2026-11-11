"""Unit tests for the pure GAMS line functions (#40): no index, no REMIND checkout, milliseconds. Some cases are
real REMIND lines, copied here, so they don't depend on the REMIND state.
"""

from __future__ import annotations

import pytest

from remind_rag.gams import decl_start, if_guards, line_conditions, strip_comments
from remind_rag.usage import eval_expr, role, switch_tests

DEFAULTS = {"cm_emiscen": "9", "cm_startyear": "2005", "carbonprice": "functionalForm", "optimization": "nash"}


# ------------------------------------------------------------------ $ifthen / $elseif / $else


def test_line_conditions_branches():
    lines = [
        '$ifthen "%carbonprice%" == "NDC"',
        "a",
        '$elseif "%carbonprice%" == "none"',
        "b",
        "$else",
        "c",
        "$endif",
        "d",
    ]
    c = line_conditions(lines)
    assert c[1] == ('"%carbonprice%" == "NDC"',)
    assert c[3] == ('not ("%carbonprice%" == "NDC") and ("%carbonprice%" == "none")',)
    assert c[5] == ('not ("%carbonprice%" == "NDC") and not ("%carbonprice%" == "none")',)
    assert c[7] == ()
    assert c[0] == c[6] == ()  # the directive lines themselves carry the enclosing conditions


def test_line_conditions_nested():
    lines = ["$ifthen %optimization% == 'nash'", "$ifthen.x %cm_emiscen% == 9", "a", "$endif.x", "b", "$endif"]
    c = line_conditions(lines)
    assert len(c[2]) == 2 and len(c[4]) == 1


# ------------------------------------------------------------------ run-time if(...) blocks


def test_if_guards_branches_and_nesting():
    lines = [
        "if(cm_emiscen eq 6,",
        "  a = 1;",
        "elseif cm_emiscen eq 9,",
        "  if(cm_startyear gt 2005,",
        "    b = 1;",
        "  );",
        "else",
        "  c = 1;",
        ");",
        "d = 1;",
    ]
    g = if_guards(lines)
    assert g[1] == ("cm_emiscen eq 6",)
    assert g[4] == ("not (cm_emiscen eq 6) and (cm_emiscen eq 9)", "cm_startyear gt 2005")
    assert g[7][0].startswith("not (cm_emiscen eq 6)") and "not (cm_emiscen eq 9)" in g[7][0]
    assert g[9] == ()


def test_if_guards_ignore_comments_and_strings():
    lines = ["* if(cm_emiscen eq 6,", 'display "if(x,";', "a = 1; !! if(y,", "b = 1;"]
    assert all(g == () for g in if_guards(lines))


# ------------------------------------------------------------------ conditions under the defaults


@pytest.mark.parametrize(
    "cond, expected",
    [
        ("cm_emiscen eq 9", True),
        ("cm_emiscen ne 9", False),
        ("cm_emiscen = 9 and cm_startyear le 2005", True),
        ("cm_emiscen eq 6 or cm_startyear gt 2020", False),
        ("not (cm_emiscen eq 6)", True),
        ('"%carbonprice%" == "functionalForm"', True),
        ("%optimization% == 'negishi'", False),
        ("cm_emiscen eq 9 and sameas(t, '2005')", None),  # depends on data
        ("cm_emiscen eq 6 and sameas(t, '2005')", False),  # decided by the switch part alone
        ("cm_unknownswitch eq 1", None),
        ("ord(t) gt 1", None),  # no switch test at all
    ],
)
def test_eval_expr(cond, expected):
    assert eval_expr(cond, DEFAULTS) is expected


@pytest.mark.parametrize(
    "cond, expected",
    [
        ("%carbonprice% == 'functionalForm'", True),
        ("not (%carbonprice% == 'NDC')", True),
        ("'%carbonprice%' == 'NDC'", False),
    ],
)
def test_single_quoted_string_tests_are_decided(cond, expected):
    # `$ifi not %damages% == 'DiceLike'` was read with an empty value and stayed undecided
    assert eval_expr(cond, DEFAULTS) is expected


# ------------------------------------------------------------------ #50: switch tests come from code only


@pytest.mark.parametrize(
    "text, expected",
    [
        # prose in comments and messages is not a test
        ("q_x(t)$(t.val ge 2010).. !! for cm_startyear = 2005 the equation should not be applied\n v_x(t) =g= 0;", []),
        ("*** requires cm_emiscen = 1\np_x = 1;", []),
        ("$ontext\nif(cm_emiscen eq 1, p_x = 1);\n$offtext\np_y = 2;", []),
        ('if(cm_emiscen ne 9, abort "only compatible with cm_startyear = 2005");', ["cm_emiscen ne 9"]),
        # code tests stay, including string tests and `!!` inside a string
        ("if(cm_emiscen eq 9, p_x = 1); !! default", ["cm_emiscen eq 9"]),
        ('$ifthen "%carbonprice%" == "functionalForm"', ['"%carbonprice%" == "functionalForm"']),
        ('p_x$(cm_startyear gt 2005) = 1; p_s = "a !! b";', ["cm_startyear gt 2005"]),
    ],
)
def test_switch_tests_ignore_comments_and_messages(text, expected):
    assert [t.split("  (default")[0] for t, _ in switch_tests(text, DEFAULTS)] == expected


# ------------------------------------------------------------------ roles, comments, declarations


@pytest.mark.parametrize(
    "line, name, expected",
    [
        ("vm_cap.fx(t,regi,te) = 0;", "vm_cap", "assigned"),
        ("p_x(t) = vm_cap.l(t);", "vm_cap", "read"),
        ("execute_loadpoint 'input' vm_cap.l = vm_cap.l;", "vm_cap", "assigned"),
    ],
)
def test_role_basics(line, name, expected):
    assert role(line, name) == expected


@pytest.mark.parametrize(
    "line, name, expected",
    [
        # index v4 role fixes, real REMIND lines. `=` inside if( / elseif( / $( / break$( is a comparison
        ("if(cm_nucscen = 5,", "cm_nucscen", "read"),
        ("elseif(c_co2captureEnergy = 3), !! no bio carbon capture:", "c_co2captureEnergy", "read"),
        ('if ( (pm_ffPolyCumEx(regi,enty,"max") = 0),', "pm_ffPolyCumEx", "read"),
        ('loop(teNoLearn(te) $ (pm_data(regi,"tech_stat",te) = 2),', "pm_data", "read"),
        (
            "break$(p47_implicitQttyTargetActive_iter(iteration2,ext_regi) = 1);",
            "p47_implicitQttyTargetActive_iter",
            "read",
        ),
        # conditional assignments with a space after `$` are assignments
        ('vm_cap.lo(t,regi,te,"1") $ (t.val >= 2030) = 1e-7;', "vm_cap", "assigned"),
        (
            'p_capCum("2015",regi,te) $ (p_capCum("2015",regi,te) = 0) = fm_dataglob("ccap0",te) / card(regi);',
            "p_capCum",
            "assigned",
        ),
        (
            'pm_cesdata_sigma(ttot,in)$ (pm_ttot_val(ttot) le 2025  AND sameAs(in, "inco")) = 0.1;',
            "pm_cesdata_sigma",
            "assigned",
        ),
        # the loop variable of a for loop is assigned
        ("for (sm_tmp = sm_tmp downto 0,", "sm_tmp", "assigned"),
        ("if(cond, pm_x(regi) = 1);", "pm_x", "assigned"),
    ],
)
def test_role(line, name, expected):
    assert role(line, name) == expected


def test_strip_comments_keeps_strings():
    text = '* comment\n$ontext\nprose\n$offtext\np_x = 1; !! tail\np_s = "a !! b";'
    assert strip_comments(text) == 'p_x = 1; \np_s = "a !! b";'


@pytest.mark.parametrize(
    "line, starts",
    [
        ("parameter", True),
        ("positive variables vm_x(t)", True),
        ("Scalars", True),
        ("sets_x(t) = yes;", False),
        ("* parameter in a comment", False),
        ("set_x(t) = 1;", False),
    ],
)
def test_decl_start(line, starts):
    assert bool(decl_start(line)) is starts
