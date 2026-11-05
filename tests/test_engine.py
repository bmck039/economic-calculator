"""Engine tests on a synthetic country (no downloaded data needed)."""
from __future__ import annotations

import math
from dataclasses import replace

import pytest

from econsim.engine import interp_profile, nrr_for_month, simulate, to_display
from econsim.model import CountryData, Earner, JobLoss, Plan, Sourced, TaxPoint
from econsim.tax import (USParams, bracket_tax, local_rates, parse_brackets,
                         us_expat_tax)

US = USParams(
    brackets_mfj=parse_brackets("10:0;12:24800;22:100800;24:211400;32:403550;35:512450;37:768700"),
    brackets_single=parse_brackets("10:0;12:12400;22:50400;24:105700;32:201775;35:256225;37:640600"),
    std_mfj=32200, std_single=16100, ctc=2200, ctc_refundable=1700, feie=132900,
    housing_base=21264, housing_limit=39870, fica_rate=0.0765, ss_wage_base=184500)


def S(v, unit="", year=2026):
    return Sourced(v, unit, "test", year)


def country(**kw) -> CountryData:
    base = dict(
        iso3="TST", name="Testland", currency="EUR", data_year=2026,
        earnings={"median": [(25, 30000.0), (35, 40000.0), (45, 45000.0), (55, 45000.0)],
                  "p25": [(25, 24000.0), (55, 34000.0)],
                  "p75": [(25, 38000.0), (55, 60000.0)]},
        earnings_src=S(float("nan")),
        tax_points=[TaxPoint(0.5, 0.05, 0.15), TaxPoint(1.0, 0.15, 0.15),
                    TaxPoint(1.67, 0.25, 0.12)],
        avg_wage=S(40000.0), marginal_rate_top=0.45, tax_src=S(float("nan")),
        child_benefit_per_child=S(2000.0), budget={"groceries": S(600.0), "other": S(1400.0)},
        rent=S(1000.0), rent_factor_per_child=[1.1, 1.2], rent_factor_src=S(1.1),
        home_price=S(300000.0), down_payment_pct=S(20.0), transfer_tax_pct=S(3.0),
        mortgage_rate=S(4.0), wedding_cost=S(15000.0), child_extra_spending=S(400.0),
        childcare_per_child=S(6000.0), emergency_expense_size=S(1000.0),
        nrr={2: S(80.0), 6: S(70.0), 12: S(60.0), 24: S(50.0)},
        unemployment_rate=S(6.0), inflation_hist=S(2.5), real_wage_growth_hist=S(0.8),
        lcu_per_usd=S(0.9), lcu_per_intl=S(0.7), totalization=True)
    base.update(kw)
    return CountryData(**base)


def plan(**kw) -> Plan:
    base = dict(country="TST", earners=(Earner(28), Earner(27)), horizon_age=60,
                marriage_age=None, child_ages=(None, None), buy_house_age=None,
                inflation=2.0, real_wage_growth=1.0, return_nominal=5.0, us_citizens=False)
    base.update(kw)
    return Plan(**base)


# ------------------------------------------------------------------ tax
def test_bracket_tax_mfj_2026():
    # 24,800 x 10% + (50,000 - 24,800) x 12%
    assert bracket_tax(50000, US.brackets_mfj) == pytest.approx(2480 + 25200 * 0.12)
    assert bracket_tax(0, US.brackets_mfj) == 0


def test_local_rates_interpolate_and_extrapolate():
    pts = country().tax_points
    it, ssc = local_rates(40000, 40000, pts, 0.45)
    assert it == pytest.approx(6000) and ssc == pytest.approx(6000)
    it2, _ = local_rates(48000, 40000, pts, 0.45)   # 1.2 AW: between 1.0 and 1.67
    assert 0.15 < it2 / 48000 < 0.25
    it3, ssc3 = local_rates(100000, 40000, pts, 0.45)  # above the top point
    g_top = 1.67 * 40000
    assert it3 + ssc3 == pytest.approx(g_top * 0.37 + 0.45 * (100000 - g_top))


def test_feie_wipes_out_us_tax_for_median_earners():
    r = us_expat_tax([60000, 50000], [9000, 7000], True, 0, 0, US)
    assert r.residual == pytest.approx(0) and r.method == "FEIE+FTC"


def test_low_tax_country_leaves_residual_us_tax_above_feie():
    # 200k each, almost no local tax -> income above the FEIE is taxed at stacked rates
    r = us_expat_tax([200000, 200000], [1000, 1000], True, 0, 0, US)
    assert r.residual > 20000


def test_high_tax_country_ftc_covers_everything():
    r = us_expat_tax([200000, 200000], [80000, 80000], True, 0, 0, US)
    assert r.residual <= 0


def test_ftc_route_can_refund_child_credit():
    # a high-tax country: FTC route gives refundable CTC -> negative residual chosen
    r = us_expat_tax([40000, 0], [10000, 0], True, 2, 0, US)
    assert r.method == "FTC" and r.residual < 0


# ------------------------------------------------------------------ engine
def test_profile_interpolation_flat_outside():
    pts = [(25, 10.0), (35, 20.0)]
    assert interp_profile(pts, 30) == 15 and interp_profile(pts, 20) == 10
    assert interp_profile(pts, 60) == 20


def test_nrr_buckets():
    nrr = {2: 80, 6: 70, 12: 60, 24: 50}
    assert nrr_for_month(nrr, 1) == 0.8 and nrr_for_month(nrr, 3) == 0.7
    assert nrr_for_month(nrr, 13) == 0.5 and nrr_for_month(nrr, 40) == 0.5


def test_baseline_accounting_identity():
    r = simulate(plan(), country())
    d = r.years
    assert len(d) == 60 - 28 + 1
    assert (d["saving"] - (d["net_income"] - d["expenses_total"])).abs().max() < 1e-6
    # net worth = liquid when renting
    assert (d["net_worth"] - d["liquid"]).abs().max() < 1e-6
    assert d["liquid"].iloc[-1] > 0


def test_real_display_removes_inflation():
    r = simulate(plan(real_wage_growth=0.0), country())
    real = to_display(r, country(), real=True)
    # with zero real growth, real rent stays constant
    assert real["rent"].iloc[0] == pytest.approx(real["rent"].iloc[10])
    nominal = to_display(r, country(), real=False)
    assert nominal["rent"].iloc[10] == pytest.approx(nominal["rent"].iloc[0] * 1.02 ** 10)


def test_currency_conversion():
    cd = country()
    r = simulate(plan(), cd)
    usd = to_display(r, cd, real=False, currency="USD")
    assert usd["gross"].iloc[0] == pytest.approx(r.years["gross"].iloc[0] / 0.9)


def test_children_add_costs_and_benefits():
    base = simulate(plan(), country()).years
    kids = simulate(plan(child_ages=(30, 32)), country()).years
    t = 35 - 28  # both kids born
    assert kids["children"].iloc[t] > 0 and kids["childcare"].iloc[t] > 0
    assert kids["child_benefits"].iloc[t] > 0
    assert kids["liquid"].iloc[-1] < base["liquid"].iloc[-1]
    # childcare stops after age 5 of the youngest child
    assert kids["childcare"].iloc[32 + 6 - 28] == 0


def test_wedding_is_one_off():
    r = simulate(plan(marriage_age=30), country())
    d = r.years
    assert d["oneoff:wedding"].iloc[2] == pytest.approx(15000 * 1.02 ** 2)
    assert d["oneoff:wedding"].drop(index=2).sum() == 0
    assert any(e["event"] == "Marriage" for e in r.events)


def test_house_purchase_and_mortgage_amortisation():
    r = simulate(plan(buy_house_age=33, starting_savings=150000), country())
    d = r.years
    t = 33 - 28
    assert d["owned"].iloc[t] and not d["owned"].iloc[t - 1]
    assert d["rent"].iloc[t] == 0
    assert d["mortgage_balance"].iloc[t] < 300000 * 1.02 ** t * 1.005 ** t * 0.8
    # 25-year mortgage is paid off by the end of term
    assert d["mortgage_balance"].iloc[t + 25] == pytest.approx(0, abs=1)
    assert d["home_equity"].iloc[-1] > 0


def test_house_purchase_delayed_when_unaffordable():
    r = simulate(plan(buy_house_age=29, starting_savings=0), country())
    ev = [e for e in r.events if e["event"] == "House purchase"]
    assert not ev or ev[0]["age"] > 29
    assert any("delayed" in w or "never" in w for w in r.warnings)


def test_job_loss_reduces_income_by_nrr():
    base = simulate(plan(), country()).years
    jl = simulate(plan(job_loss=JobLoss(earner=0, age=35, months=6)), country()).years
    t = 35 - 28
    lost = base["net_income"].iloc[t] - jl["net_income"].iloc[t]
    normal_month = base["net_income"].iloc[t] / 12
    # months 1-2 at 80%, 3-6 at 70% -> lose 2*0.2 + 4*0.3 = 1.6 months of net income
    assert lost == pytest.approx(1.6 * normal_month, rel=0.02)
    assert jl["unemployment_months"].iloc[t] == 6


def test_us_layer_only_abroad_and_zero_for_median_couple():
    cd = country()
    r = simulate(plan(us_citizens=True), cd, US)
    assert r.years["us_tax"].abs().max() < 1  # FEIE covers median earnings
    r_us = simulate(plan(us_citizens=True), replace(cd, iso3="USA"), US)
    assert (r_us.years["us_tax"] == 0).all()


def test_percentile_ordering():
    lo = simulate(plan(earners=(Earner(28, "p25"), Earner(27, "p25"))), country()).years
    hi = simulate(plan(earners=(Earner(28, "p75"), Earner(27, "p75"))), country()).years
    assert hi["gross"].iloc[0] > lo["gross"].iloc[0]
    assert hi["liquid"].iloc[-1] > lo["liquid"].iloc[-1]


def test_retirement_stops_earnings():
    d = simulate(plan(horizon_age=70, retirement_age=65), country()).years
    assert d.loc[d["age1"] >= 65, "gross1"].sum() == 0
    assert not math.isnan(d["net_worth"].iloc[-1])


def test_high_tax_country_child_credit_becomes_refund():
    # FTC wipes out US tax first, so the child credit is unused and refundable (ACTC)
    r = us_expat_tax([90000, 60000], [25000, 15000], True, 2, 0, US)
    assert r.method == "FTC" and r.residual == pytest.approx(-2 * 1700)


def test_feie_route_gets_no_refundable_child_credit():
    r = us_expat_tax([50000, 0], [0, 0], True, 1, 0, US)
    assert r.feie_residual >= 0


def test_payment_to_income_cap_blocks_unaffordable_house():
    cd = country(home_price=S(3_000_000.0))
    r = simulate(plan(buy_house_age=30, starting_savings=2_000_000), cd)
    assert not r.years["owned"].any()
    assert any("payment" in w for w in r.warnings)
