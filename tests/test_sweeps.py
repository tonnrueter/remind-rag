"""Sweep checks: the mechanical sweeps over REMIND must re-find known defects from ../STALE_COMMENTS.md and must not
report the noise we tuned away. Reads the REMIND checkout next to rag/ (read-only). No LLM, no index needed.

    uv run pytest tests/test_sweeps.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from remind_rag import doccheck, preconditions

REMIND = Path(__file__).resolve().parents[2] / "remind"
pytestmark = pytest.mark.skipif(not (REMIND / "main.gms").exists(), reason="no REMIND checkout next to rag/")


@pytest.fixture(scope="module")
def docs():
    return {(f.check, f.where, f.claim.lower()) for f in doccheck.sweep(REMIND)}


@pytest.fixture(scope="module")
def pre():
    found, _ctx = preconditions.sweep(REMIND)
    return found


def _has(docs, check, where, claim=""):
    return any(c == check and w == where and claim.lower() in cl for c, w, cl in docs)


# ------------------------------------------------------------------ preconditions.py

def test_impossible_precondition_found(pre):
    # STALE_COMMENTS #9: temperatureNotToExceed requires climate=magicc, which no realization is called
    hits = [p for p in pre if p.severity == "impossible"]
    assert [p.where for p in hits] == ["modules/45_carbonprice/temperatureNotToExceed/datainput.gms:10"]


def test_abort_message_naming_other_realization(pre):
    # STALE_COMMENTS #56: the message names KotzWenzItr, the check sits in KotzWenzCPreg
    p = next(p for p in pre if p.where == "modules/51_internalizeDamages/KotzWenzCPreg/datainput.gms:10")
    assert "KotzWenzItr" in p.message_issue


def test_default_abort_is_information_not_defect(pre):
    # netZero needs cm_multigasscen = 2 (default 3): aborts by default, but the value exists
    p = next(p for p in pre if p.where == "modules/46_carbonpriceRegi/netZero/datainput.gms:10")
    assert p.severity == "aborts by default"


# ------------------------------------------------------------------ doccheck.py: known defects are found

@pytest.mark.parametrize("check, where, claim", [
    ("unknown name", "modules/31_fossil/timeDepGrades/declarations.gms:117", "v_fuelslack"),              # 6
    ("unknown name", "modules/45_carbonprice/NDC/declarations.gms:20", "p45_factorrescaleco2taxlimited"),  # 29
    ("unknown name", "modules/45_carbonprice/functionalForm/datainput.gms:126", "cm_taxco2inc_after"),     # 39
    ("unknown name", "modules/47_regipol/none/bounds.gms:23", "c_nopefosccdeu"),                           # 40
    ("unknown name", "main.gms:981", "cm_peakbudgyear"),                                                   # 22
    ("unknown name", "core/postsolve.gms:27", "cm_bunkerscen"),                                            # 23
    ("stated default", "main.gms:391", "initialspread10"),                                                 # 14
    ("stated default", "modules/45_carbonprice/expoLinear/datainput.gms:9", "2060"),                       # 15
    ("stated default", "main.gms:1452", "2024_cond"),                                                      # 46
    ("file pointer", "modules/15_climate/magicc7_ar6/postsolve.gms:87", "core/postsolve.gms"),             # 4
    ("file pointer", "main.gms:971", "ndc/postsolve.gms"),                                                 # 19
    ("file pointer", "modules/45_carbonprice/exogenousExpo/declarations.gms:7", "ndcexpo"),
])
def test_known_defect_found(docs, check, where, claim):
    assert _has(docs, check, where, claim)


def test_renamed_switch_points_to_rename_table():
    f = next(f for f in doccheck.sweep(REMIND) if f.claim == "cm_taxCO2inc_after_peakBudgYr")
    assert "readCheckScenarioConfig.R" in f.problem


# ------------------------------------------------------------------ doccheck.py: tuned-away noise stays away

@pytest.mark.parametrize("check, where, claim", [
    ("stated default", "modules/45_carbonprice/expoLinear/datainput.gms:9", "4.5%"),  # growth factor 1.045
    ("stated default", "main.gms:1696", ""),       # multi-word value "iteration 15, …"
    ("stated default", "main.gms:2061", ""),       # quoted value containing parentheses
    ("stated default", "main.gms:2205", ""),       # $SetGlobal spelled with a capital S
    ("unknown name", "core/declarations.gms:639", ""),  # naming-convention placeholder s_xxx_2_yyy
    ("unknown name", "modules/24_trade/capacity/equations.gms:20", ""),  # commented-out equation
    ("realization name", "modules/80_optimization/negishi/output.gms:66", ""),  # "trade = exports - imports"
])
def test_noise_not_reported(docs, check, where, claim):
    assert not _has(docs, check, where, claim)
