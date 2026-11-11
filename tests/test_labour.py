"""Job market by age and degree: CPS definitions, derivation rules, engine use."""
from __future__ import annotations

import pandas as pd
import pytest

import build_econ_dataset as B
from econsim.engine import edu_pay_factor, simulate, unemployment_rate_for
from econsim.model import Earner, Sourced, age_band

from test_engine import country, plan


# ------------------------------------------------------------------ CPS microdata rules
def cps(rows):
    cols = ["PRTAGE", "PEEDUCA", "PEMLR", "PRWKSTAT", "PRDISC", "PEHRWANT", "PEHRAVL",
            "PRMJOCC1", "PTIO1OCD", "PWCMPWGT"]
    return pd.DataFrame(rows, columns=cols)


def test_cps_rates_definitions():
    d = cps([
        [23, 43, 1, 2, -1, -1, -1, 2, 1020, 1.0],    # full time, professional job
        [23, 43, 1, 7, -1, 1, 1, 4, 4760, 1.0],      # part-timer wanting FT, retail sales
        [23, 43, 4, 11, -1, -1, -1, -1, -1, 1.0],    # unemployed
        [23, 43, 7, 1, 1, -1, -1, -1, -1, 1.0],      # discouraged (marginally attached)
        [23, 43, 7, 1, -1, -1, -1, -1, -1, 1.0],     # not in labour force, not counted
    ])
    r = B.cps_rates(d)
    assert r["unemployment"] == pytest.approx(100 / 3)               # 1 of 3 in the LF
    assert r["functional"] == pytest.approx(300 / 4)                 # (1+1+1) / (3+1)
    assert r["hours_underemployment"] == pytest.approx(25)
    assert r["u6"] == pytest.approx(50)            # PRWKSTAT 7 is not 'economic reasons'
    assert r["overqualification"] == pytest.approx(50)               # sales of 2 employed
    assert r["slack_extra"] == pytest.approx(50)
    d.loc[1, "PTIO1OCD"] = 4810                    # insurance agent: ISCO 3321, graduate job
    assert B.cps_rates(d)["overqualification"] == 0


def test_cps_education_mapping():
    e = B.cps_education(pd.Series([35, 39, 40, 41, 42, 43, 44, 46]))
    assert list(e) == ["below_upper_secondary", "upper_secondary", "upper_secondary",
                       "short_tertiary", "short_tertiary", "bachelor", "master_plus",
                       "master_plus"]


def test_annual_from_monthly_fills_only_missing_years():
    m = pd.DataFrame({"iso3": "USA", "age_band": "Y15T24",
                      "period": ["2025-01", "2025-02", "2026-01"], "value": [9.0, 11.0, 8.0]})
    out = B.annual_from_monthly(m, {("USA", "Y15T24", "2026")}, ["iso3", "age_band"])
    assert list(out["period"]) == ["2025"]
    assert out["value"].iloc[0] == pytest.approx(10.0) and bool(out["estimated"].iloc[0])
    assert "2 monthly values" in out["method"].iloc[0]


# ------------------------------------------------------------------ derivation (Europe)
def long_rows(rows):
    df = pd.DataFrame(rows, columns=["iso3", "series", "statistic", "age_band", "category",
                                     "unit", "value"])
    return df.assign(year=2025, estimated=False, method=None)


def test_europe_functional_rate_adds_slack_with_education_gap():
    E = B.PC_ELF
    rows = [("DEU", "lfsa_urgaed", "rate", "20-24", "all", "%", 8.0),
            ("DEU", "lfsa_urgaed", "rate", "25-29", "all", "%", 4.0),
            ("DEU", "lfsa_urgaed", "rate", "25-29", "tertiary", "%", 2.5),
            ("DEU", "lfsa_urgaed", "rate", "20-24", "tertiary", "%", 5.0),
            ("DEU", "lfsi_sla_a", "uemp_pt", "25-54", "all", E, 2.0),
            ("DEU", "lfsi_sla_a", "seek_navl", "25-54", "all", E, 9.0),   # not counted
            ("DEU", "lfsi_sla_a", "nseek_avl", "25-54", "all", E, 1.0),
            # counts (thousands) + slack %: extended labour force = 100 / 10% = 1000
            ("DEU", "lfsa_sup_edu", "slack", "25-74", "all", E, 10.0),
            ("DEU", "lfsa_sup_edu", "slack", "25-74", "all", "thousand persons", 100.0),
            ("DEU", "lfsa_sup_edu", "uemp_pt", "25-74", "all", "thousand persons", 30.0),
            ("DEU", "lfsa_sup_edu", "nseek_avl", "25-74", "all", "thousand persons", 10.0),
            ("DEU", "lfsa_sup_edu", "slack", "25-74", "tertiary", E, 5.0),
            ("DEU", "lfsa_sup_edu", "slack", "25-74", "tertiary", "thousand persons", 20.0),
            ("DEU", "lfsa_sup_edu", "uemp_pt", "25-74", "tertiary", "thousand persons", 6.0),
            ("DEU", "lfsa_sup_edu", "nseek_avl", "25-74", "tertiary", "thousand persons", 2.0),
            ("DEU", "lfsa_eoqgan", "rate", "20-34", "tertiary", "%", 20.0),
            ("DEU", "DF_LSO_NEAC_UNEMP", "rate", "25-34", "tertiary", "%", 4.0),
            ("DEU", "DF_LSO_NEAC_UNEMP", "rate", "25-34", "bachelor", "%", 5.0)]
    lm = B._LM(long_rows(rows))
    a = B._lm_europe(lm, "DEU", "DEU", 2025, "25-29", "all")
    assert a["unemployment"][0] == 4.0
    assert a["extra"][:2] == (pytest.approx(3.0), pytest.approx(2.0))  # SEEK_NAVL left out
    t = B._lm_europe(lm, "DEU", "DEU", 2025, "25-29", "tertiary")
    assert t["extra"][0] == pytest.approx(3.0 * 2.0 / 4.0)            # x education gap
    assert "extra" not in B._lm_europe(lm, "DEU", "DEU", 2025, "20-24", "all")
    assert B._lm_europe(lm, "DEU", "DEU", 2025, "20-24", "tertiary")["overqualification"][0] \
        == 20.0
    b = B._lm_europe(lm, "DEU", "DEU", 2025, "20-24", "bachelor")
    assert b["unemployment"][0] == pytest.approx(5.0 * 5.0 / 4.0)       # x EAG degree split
    assert b["unemployment"][1]                                        # flagged estimate
    # assembled: 20-24 slack = 25-29 slack x (8 / 4) - not the student-heavy 15-24 data
    df = B.derive_labour_market(long_rows(rows), None)
    f = df[(df.iso3 == "DEU") & (df.indicator == "functional")].set_index(
        ["age_band", "education"])["value"]
    assert f[("25-29", "all")] == pytest.approx(7.0)
    assert f[("20-24", "all")] == pytest.approx(8.0 + 3.0 * 2.0)


def test_education_pay_is_relative_to_employment_weighted_mean():
    rows = []
    for e, rel, share in [("below_upper_secondary", 80, 10), ("upper_secondary", 100, 50),
                          ("bachelor", 150, 40)]:
        rows += [("DEU", "DF_LSO_EARN_REL_UPPER", "index_upper_secondary_100", "25-34", e,
                  "x", rel), ("DEU", "DF_LSO_NEAC_DISTR_EA", "share", "25-34", e, "%", share),
                 ("DEU", "DF_LSO_NEAC_EMP", "rate", "25-34", e, "%", 100)]
    df = B.derive_education_pay(long_rows(rows), None)
    d = df[df["iso3"] == "DEU"].set_index("education")["factor"]
    mean = (80 * 10 + 100 * 50 + 150 * 40) / 100
    assert d["bachelor"] == pytest.approx(150 / mean, abs=1e-3)
    assert set(df[df["iso3"] == "CYP"]["education"]) >= {"bachelor"}   # EU-median fallback


# ------------------------------------------------------------------ engine
def labour_cd(**kw):
    lab = {("unemployment", "20-24", "all"): Sourced(10.0, "%"),
           ("unemployment", "20-24", "bachelor"): Sourced(4.0, "%"),
           ("unemployment", "25-29", "bachelor"): Sourced(3.0, "%")}
    pay = {("bachelor", "25-34"): 1.3, ("bachelor", "35-44"): 1.5,
           ("bachelor", "55-64"): 1.5}
    return country(labour=lab, edu_pay=pay, **kw)


def test_age_band_and_rate_lookup():
    assert [age_band(a) for a in (19, 24, 25, 29, 30, 34, 35, 60)] == \
        ["20-24", "20-24", "25-29", "25-29", "30-34", "30-34", "25-64", "25-64"]
    cd = labour_cd()
    assert unemployment_rate_for(cd, 23, "bachelor")[0] == 4.0
    assert unemployment_rate_for(cd, 23, None)[0] == 10.0
    assert unemployment_rate_for(cd, 40, "bachelor")[0] is None      # -> country fallback


def test_edu_pay_factor_interpolates_by_age():
    cd = labour_cd()
    assert edu_pay_factor(cd, "bachelor", 23) == pytest.approx(1.3)
    assert edu_pay_factor(cd, "bachelor", 35) == pytest.approx(1.4)
    assert edu_pay_factor(cd, None, 35) == 1.0
    assert edu_pay_factor(cd, "master_plus", 35) == 1.0              # no data -> neutral


def test_degree_raises_pay_and_lowers_unemployment_cost():
    cd = labour_cd()
    base = simulate(plan(earners=(Earner(23), Earner(23))), cd).years
    grad = simulate(plan(earners=(Earner(23, education="bachelor"),
                                  Earner(23, education="bachelor"))), cd).years
    assert grad["gross"].iloc[0] == pytest.approx(1.3 * base["gross"].iloc[0], rel=1e-6)
    off = simulate(plan(earners=(Earner(23, education="bachelor"),) * 2,
                        education_pay=False), cd).years
    assert off["gross"].iloc[0] == pytest.approx(base["gross"].iloc[0])
    # same pay, 4% instead of 10% unemployment risk
    assert off["unemployment_risk_cost"].iloc[0] < 0.5 * base["unemployment_risk_cost"].iloc[0]


# ------------------------------------------------------------------ US gap fills
def test_us_cpi_and_saving_gap_fill(monkeypatch):
    months = [f"{y}-{m:02d}-01" for y in (2024, 2025) for m in range(1, 13) if
              (y, m) != (2025, 10)]                                  # Oct 2025 never collected
    monkeypatch.setattr(B, "fred", lambda f, sid: pd.DataFrame(
        {"date": months, "value": [100.0 if d < "2025" else 103.0 for d in months]}))
    wb = B.frame(pd.DataFrame({"iso3": ["USA"], "period": ["2024"], "value": [150.0]}),
                 measure="price_level", statistic="cpi_index", frequency="A",
                 source="World Bank WDI")
    fill = B.cpi_us_gap_fill(None, wb)
    assert list(fill["year"]) == [2025] and fill["value"].iloc[0] == pytest.approx(154.5)
    assert bool(fill["estimated"].iloc[0]) and "11 months" in fill["method"].iloc[0]
    sav = B.saving_us_gap_fill(None, wb.assign(value=5.0))
    assert sav["value"].iloc[0] == pytest.approx(5.0 * 103 / 100)
