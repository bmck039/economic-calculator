"""The years ahead: retirement phase, forward-looking defaults and optional life factors
(synthetic country, no downloaded data needed)."""
from __future__ import annotations

import pytest

from econsim.engine import horizon_default, interp_nrr, life_expectancy_65, simulate
from econsim.model import Earner, Sourced

from test_engine import country, plan

LE = {("female", 2024): 86.0, ("male", 2024): 83.0, ("female", 2065): 90.0,
      ("male", 2065): 88.0}


def life_cd(**kw):
    base = dict(retirement_age=Sourced(65.0, "age", "test"), le65=LE,
                pension_nrr={0.5: 80.0, 1.0: 60.0, 2.0: 40.0},
                rates={"y10": 3.0, "short_rate": 2.5, "mortgage_rate_future": 4.5,
                       "inflation_target": 2.0})
    base.update(kw)
    return country(**base)


def test_nrr_interpolates_in_log_earnings():
    tab = {0.5: 80.0, 1.0: 60.0, 2.0: 40.0}
    assert interp_nrr(tab, 1.0) == 60.0
    assert interp_nrr(tab, 2 ** 0.5) == pytest.approx(50.0)     # half-way in log terms
    assert interp_nrr(tab, 0.2) == 80.0 and interp_nrr(tab, 5) == 40.0


def test_life_expectancy_and_horizon_follow_the_projection():
    cd = life_cd()
    assert life_expectancy_65(cd, 2044) == pytest.approx(86 + 4 * 20 / 41)
    assert life_expectancy_65(cd, 2090) == 90.0
    h = horizon_default(plan(earners=(Earner(25), Earner(25))), cd)
    # P1 turns 65 in 2066 -> women's projection 90, + 2 years for the longer-living partner
    assert h.value == 92


def test_defaults_come_from_the_country():
    cd = life_cd()
    r = simulate(plan(horizon_age=None, return_nominal=None, cash_return=None,
                      inflation=None), cd)
    assert r.summary["retirement_age"] == 65
    assert r.inputs["return_nominal"]["value"] == pytest.approx(3.0 + 0.6 * 3.5 - 0.2)
    assert r.inputs["cash_return"]["value"] == pytest.approx(2.0)
    assert r.inputs["mortgage_rate_after_fixation"]["value"] == 4.5
    assert r.years["age1"].iloc[-1] == r.summary["horizon_age"]


def test_pension_starts_at_retirement_and_follows_prices():
    cd = life_cd()
    y = simulate(plan(horizon_age=80, retirement_age=65), cd).years
    work, ret = y[y["age1"] < 65], y[y["age1"] >= 66]
    assert (work["pension_income"] == 0).all() and (ret["gross"] == 0).all()
    assert (ret["pension_income"] > 0).all()
    real = ret["pension_income"] / ret["price_factor"]
    assert real.max() == pytest.approx(real.min(), rel=1e-9)        # price-indexed
    # ~ NRR x last net pay (median earners ~1.1-1.4 x AW -> 50-55%)
    last_net = (work["net1"].iloc[-1] + work["net2"].iloc[-1]) / work["price_factor"].iloc[-1]
    assert 0.4 < real.iloc[0] / last_net < 0.75


def test_no_unsecured_borrowing_in_retirement():
    cd = life_cd(pension_nrr={1.0: 10.0})         # a pension far below living costs
    p = plan(earners=(Earner(60), Earner(60)), horizon_age=90, retirement_age=62)
    y = simulate(p, cd).years
    old = y[y["age1"] >= 63]
    assert (old["spending_squeeze"] > 0).any()     # living costs cut first ...
    assert (old["old_age_assistance"] > 0).any()   # ... then the safety net, not 9% debt
    assert old["liquid"].min() > -1.0


def test_gift_inheritance_university_and_care():
    cd = life_cd(university_cost=Sourced(5000.0, "EUR", "test", 2026))
    base = simulate(plan(horizon_age=85, retirement_age=65, child_ages=(30, None)), cd).years
    r = simulate(plan(horizon_age=85, retirement_age=65, child_ages=(30, None),
                      family_gift=20_000, family_gift_age=30, inheritance=100_000,
                      inheritance_age=55, university_share=50, ltc_years=3, ltc_cost=30_000),
                 cd)
    y = r.years
    ev = {e["event"]: e["age"] for e in r.events}
    assert ev["Family gift"] == 30 and ev["Inheritance"] == 55
    uni = y.loc[y["university"] > 0, "age1"]
    assert list(uni) == [48, 49, 50, 51]                             # child 18-21
    assert y["university"].iloc[20] / y["price_factor"].iloc[20] == pytest.approx(
        0.5 * 5000 * (1.01 ** 20), rel=1e-6)                          # rises with wages
    assert (y["long_term_care"] > 0).sum() == 3
    assert y["family_transfers"].sum() > 0
    assert y.loc[y["age1"] == 55, "family_transfers"].iloc[0] == pytest.approx(
        100_000 * y.loc[y["age1"] == 55, "price_factor"].iloc[0])
    assert r.years["net_worth"].iloc[30] > base["net_worth"].iloc[30]


def test_lasting_wage_penalty_cuts_main_parent_pay_and_pension():
    cd = life_cd()
    a = simulate(plan(horizon_age=70, retirement_age=65, child_ages=(30, None),
                      child_penalty=False), cd)
    b = simulate(plan(horizon_age=70, retirement_age=65, child_ages=(30, None),
                      child_penalty=False, child_wage_penalty=10), cd)
    t = 40 - 28
    assert b.years["gross2"].iloc[t] == pytest.approx(0.9 * a.years["gross2"].iloc[t])
    assert b.years["gross1"].iloc[t] == pytest.approx(a.years["gross1"].iloc[t])
    assert b.summary["pension_income_real"] < a.summary["pension_income_real"]


def test_typical_student_debt_by_degree():
    cd = life_cd(student_debt={"bachelor": Sourced(30_000.0, "EUR", "test", 2026)})
    p = plan(earners=(Earner(24, education="bachelor"), Earner(24)), typical_student_debt=True)
    y = simulate(p, cd).years
    assert y["student_loan"].iloc[0] > 0
    y0 = simulate(plan(earners=(Earner(24, education="bachelor"), Earner(24))), cd).years
    assert "student_loan" not in y0 or y0["student_loan"].sum() == 0


def test_home_trend_runs_ten_years_then_follows_incomes():
    cd = life_cd(home_trend=Sourced(4.0, "%/yr", "test"), rent_trend=Sourced(2.0, "%/yr", "t"))
    y = simulate(plan(horizon_age=60, home_outlook="trend", real_wage_growth=1.0), cd).years
    rent = y["rent"] / y["price_factor"]
    assert rent.iloc[10] / rent.iloc[0] == pytest.approx(1.02 ** 10, rel=1e-6)
    assert rent.iloc[15] / rent.iloc[10] == pytest.approx(1.01 ** 5, rel=1e-6)


def test_savings_split_like_households():
    cd = life_cd(portfolio={"deposit_share": 0.5, "equity_share": 0.4, "overnight_share": 0.5},
                 rates={"y10": 3.0, "short_rate": 2.5, "mortgage_rate_future": 4.5,
                        "inflation_target": 2.0, "deposit_overnight": 0.4,
                        "deposit_term": 2.0})
    r = simulate(plan(horizon_age=60, buy_house_age=None, return_nominal=None), cd)
    inp, y = r.inputs, r.years
    assert inp["deposit_share"]["value"] == 50 and inp["equity_share"]["value"] == 40
    assert inp["cash_return"]["value"] == 0.4
    assert inp["deposit_return"]["value"] == pytest.approx(1.2)
    assert inp["return_nominal"]["value"] == pytest.approx(3.0 + 0.4 * 3.5 - 0.2)
    target = 6 * y["expenses_recurring"] / 12
    free = y["cash"] + y["invest"] - target
    late = y.iloc[10:]
    assert ((late["cash"] - target[10:]) / free[10:]).round(6).eq(0.5).all()
    # investing everything ends richer (higher return) than the household mix
    all_in = simulate(plan(horizon_age=60, buy_house_age=None, deposit_share=0.0,
                           equity_share=0.6, return_nominal=None), cd).years
    assert all_in["net_worth"].iloc[-1] > y["net_worth"].iloc[-1]


def test_student_loan_rate_and_term_are_configurable():
    cd = life_cd()
    a = simulate(plan(student_loan=30_000, student_loan_rate=0.0, student_loan_years=10),
                 cd).years
    assert a["student_loan"].iloc[0] == pytest.approx(3_000)
    assert (a["student_loan"].iloc[10:] == 0).all()
    b = simulate(plan(student_loan=30_000, student_loan_rate=5.0, student_loan_years=20),
                 cd).years
    assert (b["student_loan"] > 0).sum() == 20
    assert b["student_loan"].sum() > 30_000 * 1.4



def test_portfolio_derivation_from_balance_sheets():
    import pandas as pd
    import build_econ_dataset as B
    vals = {"deposits": 50.0, "deposits_transferable": 30.0, "deposits_other": 20.0,
            "debt_securities": 5.0, "listed_shares": 10.0, "fund_shares": 15.0,
            "life_insurance": 20.0}
    rows = [{"iso3": iso, "measure": "household_assets", "statistic": k, "year": 2025,
             "value": v} for iso in ("DEU", "DNK") for k, v in vals.items()]
    df = B.derive_portfolio(pd.DataFrame(rows), None).set_index("iso3")
    assert df.loc["DEU", "deposit_share"] == pytest.approx(0.5)
    assert df.loc["DEU", "equity_share"] == pytest.approx(25 / 50)       # funds = shares
    assert df.loc["DEU", "overnight_share"] == pytest.approx(0.6)
    # Danish life insurance is labour-market pension saving: left out
    assert df.loc["DNK", "deposit_share"] == pytest.approx(50 / 80)
    assert bool(df.loc["MLT", "estimated"])                               # EU median
