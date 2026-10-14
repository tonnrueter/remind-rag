"""Unit tests for the pure GAMS line functions (#40): no index, no REMIND checkout, milliseconds.

`role()` has its own cases in test_tool_output.py (real REMIND lines from the v4 role fixes).
"""

from __future__ import annotations

import pytest

from remind_rag.gams import decl_start, if_guards, line_conditions, strip_comments
from remind_rag.usage import eval_expr, role

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
