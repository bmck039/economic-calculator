"""Cohorts: gaps between generations are measured, kept as far as history says they last,
and the simulation starts where people of each age are now. Also: starting wealth by
age, owning at the start, and the household's own cost of living."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import build_econ_dataset as B
from econsim.cohort import CohortData, fit_all
from econsim.engine import simulate
from econsim.model import Earner

from test_engine import country, plan

YEARS = range(2000, 2027)
AGES = range(18, 80)


def table(fn, measure="earn_ftyr", span=1, noise=0.0, seed=0) -> pd.DataFrame:
    """Synthetic survey: value = ref x exp(fn(age, year)) (+ sampling noise)."""
    rng = np.random.default_rng(seed)
    rows = [{"iso3": "TST", "measure": measure, "education": "all", "age": a, "year": y,
             "value": 50_000 * np.exp(fn(a, y) + noise * rng.standard_normal()),
             "ref": 50_000.0, "span": span, "source": "test"}
            for y in YEARS for a in AGES]
    return pd.DataFrame(rows)


def life(a):            # the same lifecycle every year
    return 0.6 * np.log(min(a, 50) / 25)


# ------------------------------------------------------------------ estimation
def test_lasting_cohort_gaps_are_recognised():
    # cohorts born after 1990 earn 15% less at every age, for good
    t = table(lambda a, y: life(a) - 0.15 * (y - a > 1990), noise=0.01)
    fits = fit_all(t)["earn_ftyr"]
    assert all(fits[h].phi > 0.9 for h in (5, 8, 10))
    c = CohortData.from_table(t, fits={"earn_ftyr": fits})
    # the example: someone now 33 (born 1993) vs today's 35-year-olds (born 1991) - both
    # after the break, no gap; vs today's 40-year-olds (born 1986): 15% behind, and stays
    assert c.adjustment("earn_ftyr", None, 33, 2) == pytest.approx(0.0, abs=0.02)
    assert c.adjustment("earn_ftyr", None, 33, 7) == pytest.approx(-0.15, abs=0.03)
    assert c.adjustment("earn_ftyr", None, 33, 10) == pytest.approx(-0.15, abs=0.03)


def test_passing_shocks_are_not_carried():
    # a bad year for the young (age x year noise, no cohort pattern) does not last
    rng = np.random.default_rng(1)
    shock = {(a, y): 0.08 * rng.standard_normal() for a in AGES for y in YEARS}
    t = table(lambda a, y: life(a) + shock[(a, y)])
    fits = fit_all(t)["earn_ftyr"]
    assert all(fits[h].phi < 0.2 for h in (5, 8, 10))


def test_short_horizons_ramp_up_and_shared_samples_are_skipped():
    t = table(lambda a, y: life(a) - 0.15 * (y - a > 1990), span=5, noise=0.01)
    fits = fit_all(t)["earn_ftyr"]
    assert min(fits) == 5                       # gap and outcome share people below 5
    c = CohortData.from_table(t, fits={"earn_ftyr": fits})
    assert c.phi("earn_ftyr", 2) == pytest.approx(fits[5].phi * 2 / 5)


def test_brackets_hold_when_there_is_no_gap():
    t = table(lambda a, y: life(a))
    c = CohortData.from_table(t)
    p = c.path("earn_ftyr", None, 25, 10)
    assert np.exp(p[5] - p[0]) == pytest.approx(np.exp(life(30) - life(25)), rel=1e-6)


def test_census_table_parses_like_json():
    body = b'[["A_AGE","PEARNVAL","A_AGE"],\n["23","41000","23"],\n["40",null,"40"]]'
    d = B.census_table(body)
    assert list(d.columns) == ["A_AGE", "PEARNVAL"]
    assert d["PEARNVAL"].iloc[0] == 41000 and np.isnan(d["PEARNVAL"].iloc[1])


# ------------------------------------------------------------------ engine
def coh_country(gap=-0.15, **kw):
    t = table(lambda a, y: -0.10 * 0 + gap * (y - a > 1996), measure="earn_ses", span=1)
    t = t[t["year"] % 4 == 2]                    # surveys every 4 years
    fits = fit_all(table(lambda a, y: gap * (y - a > 1996)))["earn_ftyr"]
    c = CohortData.from_table(t, borrow={"earn_ses": ("test", fits)})
    return country(cohort=c, **kw)


def test_engine_keeps_the_generation_gap():
    cd = coh_country()
    p = dict(earners=(Earner(25), Earner(25)), horizon_age=50)
    y_h = simulate(plan(**p), cd).years
    y_off = simulate(plan(cohort_effects="off", **p), cd).years
    assert y_h["gross1"].iloc[0] == pytest.approx(y_off["gross1"].iloc[0])  # now = data
    # born 2001: 15% behind people who were 25 a decade ago, so behind today's 35s later
    r = y_h["gross1"].iloc[10] / y_off["gross1"].iloc[10]
    assert 0.84 < r < 0.9
    r_full = simulate(plan(cohort_effects="full", **p), cd).years["gross1"].iloc[10] \
        / y_off["gross1"].iloc[10]
    assert r_full == pytest.approx(np.exp(-0.15), rel=0.02)
    assert "P1 cohort pay gap" in simulate(plan(**p), cd).inputs


def start_table():
    rows = []
    for a in range(20, 80):
        for st, v in (("renter_fin", 400 * a), ("renter_retirement", 100 * a),
                      ("owner_fin", 3000 * a), ("owner_retirement", 2000 * a),
                      ("owner_home", 300_000.0), ("owner_mortgage", max(250_000 - 6000 * (a - 30), 0)),
                      ("networth", 5000 * a), ("scf_own", min(a / 60, 0.9))):
            for e in ("all", "degree"):
                rows.append({"iso3": "TST", "statistic": st, "education": e,
                             "household_type": "couple", "age": a, "year": 2026, "value": v})
    return pd.DataFrame(rows)


def test_starting_wealth_follows_the_age():
    cd = country(start_wealth=start_table(), data_year=2026)
    r = simulate(plan(earners=(Earner(40), Earner(40))), cd)
    assert r.inputs["starting_retirement"]["value"] == pytest.approx(4000)
    assert r.inputs["starting_savings"]["value"] == pytest.approx(16000 - 4000)
    assert r.years["retirement_account"].iloc[0] > 4000
    # you can always enter yours
    r = simulate(plan(earners=(Earner(40), Earner(40)), starting_savings=0.0,
                      starting_retirement=0.0), cd)
    assert r.inputs["starting_savings"]["source"] == "user input"


def test_owning_at_the_start():
    cd = country(start_wealth=start_table(), data_year=2026)
    r = simulate(plan(earners=(Earner(45), Earner(45)), buy_house_age=35), cd)
    y = r.years
    assert r.inputs["own_home"]["value"] == 1.0
    assert y["owned"].iloc[0] and y["home_value"].iloc[0] > 290_000
    assert y["mortgage_balance"].iloc[0] < 160_000      # 160k owed, paid down in year 1
    assert not any(e["event"] == "House purchase" for e in r.events)
    assert "rent" not in y or y["rent"].iloc[0] == 0
    # a younger plan still buys later
    r2 = simulate(plan(earners=(Earner(30), Earner(30)), buy_house_age=35), cd)
    assert r2.inputs["own_home"]["value"] == 0.0


# ------------------------------------------------------------------ own cost of living
def test_own_inflation_follows_the_household():
    renter = simulate(plan(home_real_appreciation=2.0, home_outlook="incomes"), country())
    s = renter.summary
    assert s["own_inflation"] > s["cpi_inflation"]       # rents outpace prices
    y = renter.years
    assert y["cost_index"].iloc[0] == 1.0
    # an owner with a fixed mortgage: the payment does not rise
    owner = simulate(plan(buy_house_age=28, starting_savings=150_000,
                          home_real_appreciation=2.0, home_outlook="incomes"), country())
    assert owner.summary["own_inflation"] < s["own_inflation"]


# ------------------------------------------------------------------ against the surveys
@pytest.fixture(scope="module")
def us():
    from econsim.data import DataStore
    ds = DataStore()
    if ds.cohort.empty or "USA" not in set(ds.cohort["iso3"]):
        pytest.skip("cohort data not built")
    return ds


@pytest.mark.parametrize("edu", ["bachelor", "upper_secondary"])
def test_first_years_move_with_todays_age_brackets(us, edu):
    from econsim.validate import sweep
    sw = sweep(us.country("USA"), ages=range(20, 61, 4), edus=(edu,), years=4)
    g = sw[sw["h"].between(1, 3)].dropna(subset=["pay_model"])
    dev = (g["pay_model"] / g["pay_brackets"] - 1).abs()
    assert dev.median() < 0.02 and dev.max() < 0.08
    # unemployment from today's CPS careers agrees with the March supplement
    assert abs(g["unemp_model"].mean() - g["unemp_survey"].mean()) < 1.5


def test_cohort_method_beats_todays_brackets_out_of_sample(us):
    from econsim.validate import backtest
    t = us.cohort[us.cohort["iso3"] == "USA"]
    res = [b for m in ("earn_ftyr", "hh_income", "own") for cut in (2014, 2016)
           for b in backtest(t, m, cut)]
    assert res
    better = sum(b["rmse_cohort"] <= b["rmse_brackets"] * 1.001 for b in res)
    assert better >= 0.75 * len(res)
    assert np.mean([b["rmse_cohort"] / b["rmse_brackets"] for b in res]) < 1.0
