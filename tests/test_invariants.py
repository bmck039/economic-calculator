"""Property tests on the real data: every country must respond sensibly to inputs."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from econsim.model import Earner, Plan

DATA = (Path(__file__).resolve().parent.parent / "econ_data" / "earnings_profile.csv").exists()
pytestmark = pytest.mark.skipif(not DATA, reason="run build_econ_dataset.py first")


@pytest.fixture(scope="module")
def ds():
    from econsim.data import DataStore
    return DataStore()


def base(iso, **kw):
    p = dict(country=iso, earners=(Earner(23), Earner(24)), marriage_age=28, child_ages=(30, 33),
             buy_house_age=30, horizon_age=66, us_citizens=False)
    p.update(kw)
    return Plan(**p)


def run(ds, plan, cd=None):
    from econsim.engine import simulate
    return simulate(plan, cd or ds.country(plan.country), ds.us_params())


def isos(ds):
    return [c for c, _ in ds.countries()]


def test_no_nan_inf_or_negative_costs(ds):
    for iso in isos(ds):
        y = run(ds, base(iso)).years
        num = y.select_dtypes("number")
        assert np.isfinite(num.to_numpy()).all(), iso
        costs = [c for c in y if c.startswith("budget:") or c in
                 ("rent", "childcare", "children", "health", "cars", "owner_costs")]
        assert (y[costs] >= -1e-6).all().all(), (iso, y[costs].min()[y[costs].min() < 0])
        assert (y["income_tax_local"] >= -1e-6).all() or iso == "USA", iso
        assert (y["net_income"] <= y["gross"] + y["child_benefits"] + y["leave_benefits"]
                + y["pension_income"] + y["old_age_assistance"]
                + 1e-6).all(), iso


def test_more_rent_less_wealth_and_more_pay_more_wealth(ds):
    for iso in isos(ds):
        cd = ds.country(iso)
        def nw(p, c=cd):
            """Net worth in the last working year, plus the old-age assistance needed
            later (two renters who both run out end at 0 whatever their rent)."""
            y = run(ds, p, c).years
            return y.loc[~y["retired"].astype(bool), "net_worth"].iloc[-1] - \
                y["old_age_assistance"].sum()
        p = base(iso, buy_house_age=None, horizon_age=None)
        hi_rent = replace(p, rent=cd.rent.value * 1.3)
        assert nw(hi_rent) < nw(p), iso
        if "p75" in cd.earnings:
            rich = replace(p, earners=(Earner(23, "p75"), Earner(24, "p75")))
            assert nw(rich) > nw(p), iso


def test_purchase_event_matches_ownership(ds):
    for iso in isos(ds):
        r = run(ds, base(iso))
        y = r.years
        bought = [e for e in r.events if e["event"] == "House purchase"]
        assert bool(bought) == bool(y["owned"].any()), iso
        if bought:
            first = int(y.loc[y["owned"], "age1"].iloc[0])
            assert first == bought[0]["age"], iso
            assert (y.loc[y["owned"], "home_value"] > 0).all(), iso


def test_saving_rate_does_not_trend_with_economic_growth_alone(ds):
    """With a flat career and no life events, faster economy-wide wage growth must not
    raise the saving rate over time (spending norms rise with average incomes). Not the
    US: its brackets are indexed to prices, so real growth means bracket creep."""
    from econsim.engine import simulate
    for iso in ("DEU", "POL", "FRA"):
        cd = ds.country(iso)
        p = base(iso, earners=(Earner(30, career_growth=0), Earner(30, career_growth=0)),
                 marriage_age=None, child_ages=(None, None), buy_house_age=None,
                 horizon_age=60, unemployment_risk=False)
        s = {}
        for g in (0.0, 2.0):
            y = simulate(replace(p, real_wage_growth=g, home_real_appreciation=g), cd,
                         ds.us_params()).years
            s[g] = y["saving_rate"].iloc[-1] - y["saving_rate"].iloc[1]
        assert abs(s[2.0] - s[0.0]) < 0.03, (iso, s)


def test_pension_only_keeps_savings_and_households_hold_deposits(ds):
    for iso in ("DEU", "USA", "POL"):
        cd = ds.country(iso)
        p = base(iso, horizon_age=None)
        down, keep = run(ds, p, cd), run(ds, replace(p, spend_down=False), cd)
        old = down.years["retired"].astype(bool)
        assert keep.years["net_worth"].iloc[-1] > down.years["net_worth"].iloc[-1], iso
        assert keep.years.loc[old, "expenses_recurring"].sum() < \
            down.years.loc[old, "expenses_recurring"].sum(), iso
        # households' own mix: some savings stay in the bank at (low) deposit rates
        inp = down.inputs
        assert 5 < inp["deposit_share"]["value"] < 95, iso
        assert inp["deposit_return"]["value"] < inp["return_nominal"]["value"], iso
        all_in = run(ds, replace(p, deposit_share=0.0,
                                 equity_share=inp["equity_share"]["value"] / 100), cd)
        assert all_in.years["net_worth"].iloc[-1] > down.years["net_worth"].iloc[-1], iso
