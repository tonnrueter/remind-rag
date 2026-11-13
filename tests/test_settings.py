"""Settings (status.py) on a tiny hand-made index: defaults, a scenario's row, verdicts. No REMIND checkout."""

from __future__ import annotations

import json

import pytest

from remind_rag import store
from remind_rag.status import Settings


@pytest.fixture
def db(tmp_path):
    db = store.create(tmp_path / "t.db", 4)
    db.executemany(
        "INSERT INTO switches (name, value) VALUES (?, ?)",
        [("cm_LTSstartYr", "2030"), ("carbonRemoval", "portfolio"), ("cm_x", '"on"')],
    )
    rows = [
        (
            "S1",
            "config/scenario_config.csv",
            2,
            {"cm_LTSstartYr ": "2050", "CarbonRemoval": "off", "regionmapping": "H12"},
        ),
        ("S1", "config/scenario_config_NGFS.csv", 5, {"cm_x": "off"}),
        ("M1", "config/scenario_config_magpie_X.csv", 3, {"c56_pollutant_prices": "R2M41"}),
    ]
    db.executemany(
        "INSERT INTO scenarios (name, path, line, settings) VALUES (?, ?, ?, ?)",
        [(n, p, ln, json.dumps(s)) for n, p, ln, s in rows],
    )
    return db


def test_defaults_are_main_gms_values_without_quotes(db):
    s = Settings.defaults(db)
    assert s.values == {"cm_ltsstartyr": "2030", "carbonremoval": "portfolio", "cm_x": "on"}
    assert s.as_written["cm_LTSstartYr"] == "2030" and s.name is None


def test_scenario_row_overrides_whatever_the_header_spelling(db):
    # 16 REMIND rows have the header "cm_LTSstartYr " (trailing space); search used to miss it
    s = Settings.defaults(db).scenario("s1")
    assert s.values["cm_ltsstartyr"] == "2050" and s.values["cm_x"] == "on"  # first row only
    assert s.name == "S1" and s.source == "config/scenario_config.csv:2"
    assert s.config == {"regionmapping": "H12"}  # R-side columns apart from the GAMS settings
    assert s.selected("33_carbonRemoval", "portfolio") is False and s.module_off("33_carbonRemoval")


def test_scenario_verdicts(db):
    s = Settings.defaults(db).scenario("S1")
    u = {"module": "core", "realization": None, "conditions": json.dumps(["cm_LTSstartYr gt 2040"]), "guards": "[]"}
    assert s.use_tags(u) == [] and s.holds("cm_LTSstartYr gt 2040") is True
    tags = Settings.defaults(db).use_tags(u)
    assert len(tags) == 1 and tags[0].off and "cm_LTSstartYr gt 2040" in tags[0].text


def test_scenario_with_only_magpie_rows_or_none(db):
    with pytest.raises(ValueError, match="only MAgPIE rows .*scenario_config_magpie_X.csv:3"):
        Settings.defaults(db).scenario("M1")
    with pytest.raises(ValueError, match="unknown scenario 'nope'"):
        Settings.defaults(db).scenario("nope")
