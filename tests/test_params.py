"""Curated parameters (econsim/params) and the modules that use them."""
from __future__ import annotations

from pathlib import Path

import pytest

from econsim import params as P
from econsim.engine import annuity_pv, leave_schedule, pension_contribution, simulate
from econsim.model import Earner, Plan
from econsim.tax import (household_income_tax, uk_sdlt_first_time, us_resident_tax,
                         youth_tax_factor)

from test_engine import US, country, plan  # synthetic country & plan helpers

ALL = ["AUT", "BEL", "BGR", "HRV", "CYP", "CZE", "DNK", "EST", "FIN", "FRA", "DEU", "GRC",
       "HUN", "ISL", "IRL", "ITA", "LVA", "LTU", "LUX", "MLT", "NLD", "NOR", "POL", "PRT",
       "ROU", "SVK", "SVN", "ESP", "SWE", "CHE", "LIE", "GBR", "CAN", "USA"]
DATA = (Path(__file__).resolve().parent.parent / "econ_data" / "earnings_profile.csv").exists()


def test_every_country_has_complete_curated_rows():
    for iso in ALL:
        assert P.housing_item(iso, "rent_new_lease") is not None, iso
        assert (P.housing_item(iso, "home_price") or P.housing_item(iso, "home_price_m2")), iso
        L, H = P.lending(iso), P.household(iso)
        for k in ("down_pct", "term", "transfer_pct", "closing_pct", "rate_longrun",
                  "fixation_years", "property_tax_pct"):
            assert L[k] == L[k], (iso, k)
        for k in ("cars", "car_price", "car_insurance", "fuel_price", "km_year", "leave1_months",
                  "school_age", "pension_nrr", "invest_tax_drag", "wedding_cost",
                  "real_wage_growth", "transit_pass", "free_childcare_from"):
            assert H[k] == H[k], (iso, k)
    srcs = P._hh_sources()
    for k in P.household("DEU"):
        assert k in srcs, f"{k} has no source in household_sources.csv"


def test_us_states_complete():
    s = P.us_states()
    assert len(s) == 52 and s.notna().all().all()
    t = P.us_state_tax()
    assert len(t) == 51 and set(t.index) == set(s.index) - {"US"}


def test_state_income_tax_examples():
    p = US
    tx = us_resident_tax([60000, 55000], True, 0, 0, p, P.us_state_tax().loc["TX"].to_dict())
    ca = us_resident_tax([60000, 55000], True, 0, 0, p, P.us_state_tax().loc["CA"].to_dict())
    assert tx.state == 0 and 2500 < ca.state < 3500
    assert tx.federal == pytest.approx(9440)                 # (115000-32200) MFJ brackets
    assert ca.state_payroll == pytest.approx(0.013 * 115000)  # CA SDI


def test_itemising_mortgage_interest_lowers_us_tax():
    st = P.us_state_tax().loc["NJ"].to_dict()
    a = us_resident_tax([90000, 80000], True, 0, 0, US, st)
    b = us_resident_tax([90000, 80000], True, 0, 0, US, st, mortgage_interest=25000,
                        property_tax=9000)
    assert b.federal < a.federal


def test_uk_stamp_duty_first_time_buyer():
    assert uk_sdlt_first_time(300_000) == 0
    assert uk_sdlt_first_time(400_000) == pytest.approx(5000)
    assert uk_sdlt_first_time(600_000) == pytest.approx(0.02 * 125_000 + 0.05 * 350_000)


def test_youth_exemptions():
    assert youth_tax_factor("POL", 24, 60_000, 90_000, 1) == 0.0
    assert youth_tax_factor("POL", 26, 60_000, 90_000, 3) == 1.0
    assert youth_tax_factor("HRV", 27, 20_000, 20_000, 4) == 0.5
    assert youth_tax_factor("PRT", 23, 20_000, 20_000, 0) == 0.0
    assert youth_tax_factor("DEU", 23, 20_000, 40_000, 0) == 1.0


def test_income_splitting_helps_unequal_couple_only():
    tp = country().tax_points
    eq = household_income_tax("DEU", [40000, 40000], True, 0, 40000, tp, 0.45)
    ind = household_income_tax("ITA", [40000, 40000], True, 0, 40000, tp, 0.45)
    assert sum(eq) == pytest.approx(sum(ind))
    une = household_income_tax("DEU", [80000, 0], True, 0, 40000, tp, 0.45)
    ind2 = household_income_tax("ITA", [80000, 0], True, 0, 40000, tp, 0.45)
    assert sum(une) < sum(ind2)


def test_swiss_occupational_pension_by_age():
    hh = P.household("CHE")
    assert pension_contribution("CHE", 80000, 24, hh) == 0
    assert pension_contribution("CHE", 80000, 30, hh) == pytest.approx((80000 - 26460) * 0.035)
    assert pension_contribution("CHE", 80000, 50, hh) == pytest.approx((80000 - 26460) * 0.075)


def test_leave_schedule_primary_and_partner():
    hh = P.household("DEU")
    p = plan(child_ages=(30, None), primary_parent=1)
    s = leave_schedule(10, [2, None], hh, p)
    on1 = sum(x is not None for x in s[1])
    on0 = sum(x is not None for x in s[0])
    assert on1 == 12 and on0 == 2                        # 2 + 10 months; father 2.5 -> 2


def test_parental_leave_cuts_income_in_birth_years():
    cd = country(hh=P.household("DEU"))
    base = simulate(plan(), cd).years
    kid = simulate(plan(child_ages=(30, None)), cd).years
    t = 30 - 28
    assert kid["leave_months"].iloc[t] > 0 and kid["leave_benefits"].iloc[t] > 0
    assert kid["gross"].iloc[t] < base["gross"].iloc[t]


def test_lti_limit_binds_in_ireland_like_rules():
    L = P.lending("IRL")
    cd = country(lend=L, home_price=country().home_price.with_value(900_000.0))
    r = simulate(plan(buy_house_age=30, starting_savings=400_000, horizon_age=45), cd)
    ev = [e for e in r.events if e["event"] == "House purchase"]
    # 4x gross (~2 x 30-40k) is far below 90% of 900k: needs a huge deposit -> later / never
    assert not ev or ev[0]["age"] > 30
    assert any("loan-to-income" in w for w in r.warnings)


def test_annuity_pv_inverts_payment():
    from econsim.engine import annuity
    pay = annuity(300_000, 0.04, 25)
    assert annuity_pv(pay, 0.04, 25) == pytest.approx(300_000)


@pytest.mark.skipif(not DATA, reason="run build_econ_dataset.py first")
def test_all_countries_and_some_states_simulate_sanely():
    from econsim.data import DataStore
    ds = DataStore()
    us = ds.us_params()
    for iso, st in [(c, None) for c, _ in ds.countries()] + [("USA", "CA"), ("USA", "TX")]:
        cd = ds.country(iso, st)
        p = Plan(country=iso, earners=(Earner(23), Earner(24)), marriage_age=28,
                 child_ages=(30, 33), buy_house_age=30, horizon_age=66, us_citizens=False,
                 state=st)
        d = simulate(p, cd, us).years
        assert d["net_income"].iloc[0] > 0, iso
        ratio = d["expenses_recurring"].iloc[0] / d["net_income"].iloc[0]
        assert 0.4 < ratio < 1.3, (iso, st, ratio)          # spending vs income at 23
        assert d.filter(like="budget:vehicle").sum().sum() == 0, iso   # car module replaces
        assert 0.4 <= d["budget_scale"].iloc[0] <= 2.0


@pytest.mark.skipif(not DATA, reason="run build_econ_dataset.py first")
def test_state_changes_prices_and_taxes():
    from econsim.data import DataStore
    ds = DataStore()
    ca, ms = ds.country("USA", "CA"), ds.country("USA", "MS")
    assert ca.home_price.value > 2 * ms.home_price.value
    assert ca.rent.value > ms.rent.value
    assert ca.state_tax["brackets_mfj"] and ds.country("USA", "TX").state_tax["brackets_mfj"] == ""
