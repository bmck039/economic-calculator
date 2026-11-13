"""Backtests against what actually happened, the calibration they set, and the pieces that
make every country run the same model."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from econsim.career import STATES
from econsim.engine import simulate
from econsim.history import from_table
from econsim.model import Earner
from econsim.tax import eitc

from test_engine import country, plan


# ------------------------------------------------------------------ US EITC (2026)
def test_eitc_2026_schedule():
    assert eitc(18_290, 2, True) == pytest.approx(7316, abs=1)       # plateau
    assert eitc(65_899, 2, True) == pytest.approx(0, abs=1)          # completed phase-out
    assert eitc(58_863, 1, True) == pytest.approx(0, abs=1)
    assert eitc(26_820, 0, True) == pytest.approx(0, abs=1)
    assert eitc(40_000, 1, False) < eitc(40_000, 1, True)            # joint filers later


# ------------------------------------------------------------------ history
def history_table():
    rows = []
    for y in range(2000, 2026):
        k = y - 2000
        for s, v in (("cpi", 100 * 1.02 ** k), ("wage", 20 * 1.03 ** k),
                     ("hpi", 100 * 1.05 ** k), ("rent", 100 * 1.03 ** k),
                     ("mortgage30", 6.0), ("y10", 4.0), ("equity_return", 10.0),
                     ("tbill_return", 2.0), ("tbill3m", 2.0), ("unemployment", 5.0),
                     ("cpi:groceries", 100 * 1.01 ** k)):
            rows.append({"series": s, "year": y, "value": v})
    return pd.DataFrame(rows)


def test_history_drives_the_simulation():
    H = from_table(history_table(), 2010, 2025)
    assert H.infl[0] == pytest.approx(0.02)
    assert H.wage_real[0] == pytest.approx(1.03 / 1.02 - 1)
    assert H.cpi_level == pytest.approx(1.02 ** -15)
    p = plan(start_year=2010, horizon_age=40)
    y = simulate(p, country(), history=H).years
    assert y["price_factor"].iloc[5] == pytest.approx(1.02 ** 5)
    # groceries fall 1%/yr vs all prices: their line does too
    y0 = simulate(p, country()).years
    assert (y["budget:groceries"] / y["price_factor"]).iloc[10] < \
        (y0["budget:groceries"] / y0["price_factor"]).iloc[10]


def test_a_missing_year_in_a_price_series_is_harmless():
    t = history_table()
    t = t[~((t["series"] == "cpi:groceries") & (t["year"] == 2025))]   # the latest missing
    H = from_table(t, 2010, 2026)
    assert H.rel_level["groceries"] == H.rel_level["groceries"]          # not NaN
    assert all(v == v for v in H.rel["groceries"])
    y = simulate(plan(start_year=2010, horizon_age=40), country(), history=H).years
    assert y["expenses_total"].iloc[0] > 0 and y["liquid"].notna().all()


def test_pay_path_overrides_profile():
    e = Earner(30, pay_path=tuple(50_000.0 * 1.1 ** t for t in range(40)))
    y = simulate(plan(earners=(e, Earner(30, works=False))), country()).years
    assert y["gross1"].iloc[3] == pytest.approx(50_000 * 1.1 ** 3)


def test_owned_homes_follow_the_index():
    p = plan(buy_house_age=29, starting_savings=150_000, home_real_appreciation=0.0,
             home_outlook="incomes", real_wage_growth=0.0, horizon_age=60)
    y = simulate(p, country()).years
    v = y["home_value"] / y["price_factor"]
    i = int(y.index[y["owned"]][0])
    # flat real prices: the home keeps its real value (the index includes its ageing)
    assert v.iloc[i + 15] == pytest.approx(v.iloc[i + 1], rel=0.02)


# ------------------------------------------------------------------ against real data
@pytest.fixture(scope="module")
def ds():
    from econsim.data import DataStore
    d = DataStore()
    if not (d.dir / "scf_households.csv").exists() or d.cohort.empty:
        pytest.skip("backtest data not built")
    return d


def test_every_country_runs_the_same_career_model(ds):
    for iso, _ in ds.countries():
        cd = ds.country(iso)
        assert cd.career is not None, iso
        m = cd.career.mix_at(23, "bachelor")
        assert m.sum() == pytest.approx(1) and (m >= 0).all(), iso
        r = simulate(plan(country=iso, earners=(Earner(23, education="bachelor"),
                                                Earner(23, education="bachelor")),
                          us_citizens=False), cd)
        assert "P1 situation now" in r.inputs, iso
        assert r.career and len(r.career[0].mix[0]) == len(STATES), iso


def test_typical_households_hold_more_in_the_bank(ds):
    p = ds.portfolio.set_index("iso3")
    assert (p["deposit_share_typical"] >= p["deposit_share"] - 1e-9).mean() > 0.9
    assert (p["equity_share_typical"] <= p["equity_share"] + 1e-9).mean() > 0.9


def test_spending_levels_reproduce_national_saving(ds):
    t = pd.read_csv(ds.dir / "spending_level.csv").dropna(subset=["saving_national"])
    ok = t[t["spending_level"].between(0.71, 1.99)]
    assert (abs(ok["saving_model"] - ok["saving_national"]) < 0.6).mean() > 0.9


def test_backtest_wealth_and_ownership(ds):
    """SCF couples started in past waves, run through what happened: unbiased against
    the same birth years later (small sample here; the full one is in the README)."""
    from econsim.backtest import Backtest, score
    bt = Backtest(ds)
    c = bt.compare(bt.run(sample=600, waves=(2010, 2016), workers=12), min_model=15)
    assert len(c) >= 10
    sc = score(c)
    assert abs(sc["wealth_bias"]) < 0.6           # years of the cohort's median wages
    assert abs(sc["own_bias_pp"]) < 8


# ------------------------------------------------------------------ Europe: HFCS tables
def test_hfcs_pdf_parser():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import hfcs_tables as H
    page = (" Table A3 Net wealth medians - breakdowns\n"
            "                       euro\n"
            "                       area     BE     DE     ES     FR\n"
            "Age of RP     16-34    21.2   69.1   16.3   N      22.4\n"
            "                        (2.4)  (13.4)  (2.7)  (7.6)  (2.7)\n"
            "              35-44    86.8  173.8 <0.1   82.7   84.0\n")
    rows = pd.DataFrame(H.parse_tables([page], 2021))
    v = rows.set_index(["iso3", "row"])["value"]
    assert v[("DEU", "16-34")] == 16.3 and v[("EA", "35-44")] == 86.8
    assert np.isnan(v[("ESP", "16-34")]) and v[("DEU", "35-44")] == 0.0
    assert rows.set_index(["iso3", "row"])["se"][("BEL", "16-34")] == 13.4


def test_hfcs_tables_compiled():
    from pathlib import Path
    t = pd.read_csv(Path(__file__).resolve().parents[1] / "reference" / "hfcs_tables.csv")
    assert set(t["wave"]) == {2010, 2014, 2017, 2021}
    a = t[(t["table"] == "A3") & (t["group"] == "Age of RP")].set_index(
        ["wave", "iso3", "row"])["value"]
    assert a[(2021, "DEU", "16-34")] == 16.3 and a[(2010, "ESP", "16-34")] == 72.3
    ref = t[(t["table"] == "REF") & (t["indicator"] == "fieldwork_mid")]
    assert ref.groupby("wave")["iso3"].nunique().min() >= 15


def test_synthetic_population_matches_the_survey():
    from pathlib import Path
    from econsim.backtest_eu import Tables, population
    t = pd.read_csv(Path(__file__).resolve().parents[1] / "reference" / "hfcs_tables.csv")
    T = Tables(t, "DEU")
    for w in (2010, 2021):
        d = population(T, w, 40)
        assert d.attrs["start_nw_fit"] == pytest.approx(d.attrs["nw"], rel=0.02)
        own = d.loc[d["own"], "w"].sum()
        assert own == pytest.approx(T.at("B3", w, 40, log=False) / 100, abs=0.01)


def test_scf_waves_in_their_own_dollars(ds):
    s = pd.read_csv(ds.dir / "scf_households.csv")
    med = s.groupby("year")["income"].median()
    assert med[2004] < 0.75 * med[2025]          # not all in 2025 dollars


def test_eu_history_and_starting_wealth(ds):
    from econsim.engine import simulate as sim
    from econsim.model import Plan
    h = pd.read_csv(ds.dir / "eu_history.csv")
    H = from_table(h, 2012, 2025, iso3="ESP")
    assert "groceries" in H.rel and H.home_real[0] < 0       # Spain's prices still falling
    r = sim(Plan("DEU", (Earner(40), Earner(40)), horizon_age=42), ds.country("DEU"))
    assert "HFCS" in r.inputs["starting_savings"]["source"]
    assert r.inputs["home_value_now"]["value"] > 100_000


def test_table_backtest_europe_and_its_us_check(ds):
    """The same table method on the US agrees with the household backtest; Europe's
    cells are scored like the US's (small run here; the full one is in the README)."""
    from econsim import backtest_eu as BE
    sims, Ts = BE.run(ds, BE.load_tables(ds), ["DEU", "FRA"], workers=12)
    sc = BE.score(BE.compare(sims, Ts))
    assert sc["cells"] >= 20 and abs(sc["wealth_bias"]) < 1.0
    assert abs(sc["own_bias_pp"]) < 12
