"""Careers from today's job market (US CPS), inflation path, cost trends, survey gap."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import build_econ_dataset as B
from econsim.career import JOB_TYPE_SWITCH, STATES, CareerData, career_path
from econsim.engine import simulate
from econsim.model import Earner

from test_engine import country, plan


# ------------------------------------------------------------------ CPS classification
def test_cps_career_state_order():
    d = pd.DataFrame({
        "PEMLR": [1, 1, 1, 3, 7, 1, 5],
        "PRDISC": [-1, -1, -1, -1, 1, -1, -1],
        "PEHRUSL1": [40, 40, 20, -1, -1, 40, -1],
        "PRWKSTAT": [2, 2, 6, 11, 1, 2, 1],
        "PRMJOCC1": [2, 5, 2, -1, -1, 2, -1],
        "PTIO1OCD": [1020, 4760, 1020, -1, -1, 1020, -1],
        "PESCHENR": [-1, -1, -1, -1, -1, 1, -1]})
    assert list(B.cps_career_state(d)) == ["FT_grad", "FT_nongrad", "PT", "U", "U",
                                           "student", "NILF"]


# ------------------------------------------------------------------ career paths
def career_table(pay=(5000.0, 3500.0, 1800.0)):
    rows = []
    for band in ("20-24", "25-29", "30-34", "35-39", "40-44", "45-49", "50-54", "55-59",
                 "60-64"):
        for st, v in zip(STATES, (0.5, 0.25, 0.15, 0.10)):
            rows.append(dict(kind="share", window="current", age_band=band,
                             education="bachelor", state=st, value=v, n=500))
        for st, base in zip(STATES[:3], pay):
            for q, f in zip((10, 25, 50, 75, 90), (0.6, 0.8, 1.0, 1.25, 1.6)):
                rows.append(dict(kind="earn", window="current", age_band=band,
                                 education="bachelor", state=st, q=q, value=base * f, n=200))
    T = {"FT_grad": (0.8, 0.15, 0.03, 0.02), "FT_nongrad": (0.4, 0.5, 0.05, 0.05),
         "PT": (0.2, 0.3, 0.45, 0.05), "U": (0.3, 0.2, 0.2, 0.3)}
    for g in ("20-24", "25-29", "30-34", "35-44", "45-54", "55-64"):
        for s0, ps in T.items():
            for s1, p in zip(STATES, ps):
                rows.append(dict(kind="trans", window="current", age_band=g,
                                 education="bachelor", state=s0, to_state=s1, value=p, n=500))
        rows.append(dict(kind="growth", window="current", age_band=g, education="degree",
                         value=6.0 if g == "20-24" else 3.0, n=500))
    rows.append(dict(kind="agg_growth", window="current", age_band="total", education="all",
                     value=3.0, n=12))
    t = pd.DataFrame(rows)
    t["from"], t["to"] = "2025-10", "2026-09"
    for c in ("q", "to_state"):
        if c not in t:
            t[c] = None
    return CareerData.from_table(t)


def test_career_starts_from_todays_mix_and_situation():
    c = career_table()
    p = career_path(c, 23, "bachelor", 10)
    assert p.mix[0] == pytest.approx({"FT_grad": 0.5, "FT_nongrad": 0.25, "PT": 0.15,
                                      "U": 0.10})
    assert p.unemployment[0] == pytest.approx(0.10)
    # pay when working = today's medians weighted by the working situations
    assert p.pay[0] / 12 == pytest.approx((0.5 * 5000 + 0.25 * 3500 + 0.15 * 1800) / 0.9)
    pt = career_path(c, 23, "bachelor", 10, job="PT", pay_now=1800 * 0.8)
    assert pt.quantile == pytest.approx(25) and pt.pay[0] / 12 == pytest.approx(1440)
    # the difference to today's mix closes as people move: M + (start - M) @ moves
    M = np.array([0.5, 0.25, 0.15, 0.10])
    dev = (np.array([0, 0, 1.0, 0]) - M) @ c.moves(23, "bachelor")
    assert pt.mix[0]["PT"] == 1.0 and pt.mix[1]["PT"] == pytest.approx(M[2] + dev[2])
    assert pt.mix[9]["PT"] == pytest.approx(0.15, abs=0.05)      # back toward the mix
    assert p.mix[5] == pytest.approx(p.mix[0])                   # default: today's mix


def test_job_type_switching_is_sticky_and_raises_follow_age():
    c = career_table()
    T = c.moves(30, "bachelor")
    assert T[1, 0] + T[0, 1] == pytest.approx(JOB_TYPE_SWITCH)
    assert T[1, 0] / T[0, 1] == pytest.approx(0.4 / 0.15)      # ratio kept
    assert np.allclose(T.sum(axis=1), 1)
    # raises beyond the economy: 1.06 / 1.03 at 20-24, then 1.03 / 1.03 = none
    assert c.raise_beyond_economy(23, "bachelor") == pytest.approx((1.06 / 1.03 - 1) * 100)
    assert c.raise_beyond_economy(40, "bachelor") == pytest.approx(0.0)


def test_engine_uses_the_career_path():
    cd = country(career=career_table())
    p = plan(earners=(Earner(23, education="bachelor", job="FT_nongrad", pay_now=3000),
                      Earner(23, education="bachelor")), unemployment_risk=True)
    r = simulate(p, cd)
    y = r.years
    assert y["gross1"].iloc[0] == pytest.approx(3000 * 12)
    assert "P1 situation now" in r.inputs
    off = simulate(plan(earners=(Earner(23, education="bachelor", job="FT_nongrad",
                                        pay_now=3000), Earner(23, education="bachelor")),
                        career_model=False), cd).years
    assert off["gross1"].iloc[0] == pytest.approx(3000 * 12)    # pay_now sets the level
    assert off["gross1"].iloc[5] != pytest.approx(y["gross1"].iloc[5])


# ------------------------------------------------------------------ prices
def test_inflation_path_and_cost_trends():
    cd = country(inflation_outlook={"inflation_5y": (3.0, "2026-11", "t"),
                                    "inflation_5y5y": (2.0, "2026-11", "t")},
                 price_trends={"groceries": (2.0, "2015-2025", "t")})
    y = simulate(plan(inflation=None, horizon_age=60), cd).years
    pf = y["price_factor"]
    assert pf.iloc[5] == pytest.approx(1.03 ** 5)
    assert pf.iloc[8] == pytest.approx(1.03 ** 5 * 1.02 ** 3)
    flat = simulate(plan(inflation=None, horizon_age=60, price_outlook="flat"), cd).years
    r = (y["budget:groceries"] / flat["budget:groceries"])
    assert r.iloc[10] == pytest.approx(1.02 ** 10, rel=1e-6)
    assert r.iloc[20] == pytest.approx(1.02 ** 10, rel=1e-6)


def test_survey_gap_added_back_in_full_by_default():
    cov = {"groceries": 1.44}
    a = simulate(plan(), country(budget_coverage=cov)).years
    b = simulate(plan(survey_correction=0.0), country(budget_coverage=cov)).years
    assert a["budget:groceries"].iloc[0] == pytest.approx(1.44 * b["budget:groceries"].iloc[0])
