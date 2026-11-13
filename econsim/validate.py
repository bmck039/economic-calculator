"""Does the simulation start where people of each age are now, and move as they do?

For every starting age it compares the first years of the default plan with the surveys:

  pay           the model's pay path vs today's age brackets (full-time year-round pay,
                CPS ASEC) and vs the cohort benchmark (brackets + the gap that lasts)
  unemployment  the model's (CPS monthly careers) vs the March supplement at those ages
(wealth and ownership are validated against what really happened instead, in
econsim.backtest: never against today's older couples, who are another generation)

and backtests the cohort method itself: estimated on surveys up to a cut-off year, how
well it forecast what the cohorts then did, against simply assuming people reach today's
older brackets.

    python -m econsim.validate [--country USA] [--edu bachelor] [--out report.md]
"""
from __future__ import annotations

import argparse
import warnings
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .cohort import CohortData, fit_all
from .engine import SW_EDU, simulate, start_wealth_value
from .model import Earner, Plan


@dataclass
class AgeCheck:
    age: int
    edu: str
    pay_model: list        # pay ratio to year 0, relative to economy-wide wages
    pay_brackets: list     # today's brackets (full-time year-round)
    pay_cohort: list       # brackets + lasting gap
    unemp_model: list      # %
    unemp_survey: list     # % (March, same ages, today)
    nw_model: list         # net worth (today's money)
    nw_survey: list        # median of couples at those ages now (+ cohort gap)
    buy_age: float | None
    own50_age: float | None


def _economy(y: pd.DataFrame, g_real: float) -> np.ndarray:
    return (1 + g_real / 100) ** y["t"].to_numpy() * y["price_factor"].to_numpy()


def check_age(cd, age: int, edu: str, years: int = 6, **plan_kw) -> AgeCheck:
    coh: CohortData = cd.cohort
    p = Plan(cd.iso3, (Earner(age, education=edu), Earner(age, education=edu)),
             unemployment_risk=True, **plan_kw)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = simulate(p, cd)
    y = r.years.iloc[:years]
    W = _economy(y, r.inputs["real_wage_growth"]["value"])
    g = y["gross1"].to_numpy() / W
    pay_model = list(g / g[0]) if g[0] > 0 else [np.nan] * years
    br = coh.path("earn_ftyr", edu, age, years, "off")
    co = coh.path("earn_ftyr", edu, age, years, "history")
    pay_br = list(np.exp(br - br[0]))
    pay_co = list(np.exp(co - co[0]))
    um = [100 * float(x) for x in r.career[0].unemployment[:years]] \
        if getattr(r, "career", None) else [np.nan] * years
    s = coh.get("unemp", edu)
    us = []
    for h in range(years):
        a = age + h
        lv = s.level[-1] if s is not None else None
        us.append(100 * float(np.interp(a, s.ages[~np.isnan(lv)], lv[~np.isnan(lv)]))
                  if s is not None else np.nan)
    P = y["price_factor"].to_numpy()
    nw = ((y["liquid"] + y["retirement_account"] + y["home_value"] -
           y["mortgage_balance"]) / P).to_numpy()
    nws = []
    e_sw = SW_EDU.get(edu, "all")
    for h in range(years):
        v = start_wealth_value(cd, "networth", age + h, edu)
        if v is None:
            nws.append(np.nan)
            continue
        adj = coh.adjustment("networth", e_sw, age, h, "history")
        nws.append(v[0] * float(np.exp(adj)))
    ev = [e for e in r.events if e["event"] == "House purchase"]
    buy = ev[0]["age"] if ev else (age if r.years["owned"].iloc[0] else None)
    own = cd.start_wealth
    o = own[(own["statistic"] == "scf_own") & (own["education"] == e_sw) &
            (own["household_type"] == "couple")].sort_values("age")
    v = o.set_index("age")["value"]
    own50 = next((float(a) for a in v.index
                  if all(v.get(a + k, 0) >= 0.5 for k in range(3))), None)
    ret = r.inputs["retirement_age"]["value"]
    pay_model = [x if age + h < ret else np.nan for h, x in enumerate(pay_model)]
    return AgeCheck(age, edu, pay_model, pay_br, pay_co, um, us, list(nw), nws, buy, own50)


def sweep(cd, ages=range(20, 65), edus=("bachelor", "upper_secondary"), years=6,
          **plan_kw) -> pd.DataFrame:
    rows = []
    for e in edus:
        for a in ages:
            c = check_age(cd, a, e, years, **plan_kw)
            for h in range(years):
                rows.append({"edu": e, "age0": a, "h": h, "age": a + h,
                             "pay_model": c.pay_model[h], "pay_brackets": c.pay_brackets[h],
                             "pay_cohort": c.pay_cohort[h], "unemp_model": c.unemp_model[h],
                             "unemp_survey": c.unemp_survey[h], "nw_model": c.nw_model[h],
                             "nw_survey": c.nw_survey[h], "buy_age": c.buy_age,
                             "own50_age": c.own50_age})
    return pd.DataFrame(rows)


def backtest(table: pd.DataFrame, measure: str, cut: int, horizons=(5, 8, 10)) -> list[dict]:
    """Estimate on surveys up to `cut`, forecast the cohorts' level h years on, compare
    with what the surveys then showed. Errors in the statistic's relative units."""
    t = table[table["measure"] == measure]
    fits = fit_all(t, max_year=cut)
    past = CohortData.from_table(t, fits=fits, max_year=cut)
    full = CohortData.from_table(t, fits=fits)
    out = []
    for h in horizons:
        if cut + h > t["year"].max():
            continue
        err_c, err_x = [], []
        for (m, e), s_past in past.series.items():
            s_all = full.series[(m, e)]
            for a in range(20, 65 - h):
                now = s_past.r(a + h, cut)
                actual = s_all.r(a + h, cut + h)
                adj = past.adjustment(m, e, a, h, "history")
                if now == now and actual == actual:
                    err_c.append(actual - (now + adj))
                    err_x.append(actual - now)
        if err_c:
            out.append({"measure": measure, "cut": cut, "h": h, "n": len(err_c),
                        "rmse_cohort": float(np.sqrt(np.mean(np.square(err_c)))),
                        "rmse_brackets": float(np.sqrt(np.mean(np.square(err_x)))),
                        "phi": past.phi(measure, h)})
    return out


def report(cd, table: pd.DataFrame | None = None) -> str:
    sw = sweep(cd)
    lines = ["# Cohort validation", ""]
    for e, g in sw.groupby("edu"):
        g1 = g[g["h"].between(1, 3)]
        dev_b = (g1["pay_model"] / g1["pay_brackets"] - 1).abs()
        dev_c = (g1["pay_model"] / g1["pay_cohort"] - 1).abs()
        lines += [f"## {e}", "",
                  f"- pay, years 1-3: model vs today's brackets: median "
                  f"{dev_b.median():.1%} apart (max {dev_b.max():.1%}); vs cohort "
                  f"benchmark {dev_c.median():.1%} (max {dev_c.max():.1%})",
                  f"- unemployment: model {g['unemp_model'].mean():.1f}% vs survey "
                  f"{g['unemp_survey'].mean():.1f}% on average; largest gap "
                  f"{(g['unemp_model'] - g['unemp_survey']).abs().max():.1f} pp",
                  ""]
        lines.append("")
    lines += ["Wealth and home ownership are not checked here against today's older "
              "couples (that would mix generations): `python -m econsim.backtest` starts "
              "real SCF couples in past waves and compares with the same birth years in "
              "later waves.", ""]
    if table is not None:
        lines += ["## Backtest of the cohort method", "",
                  "| statistic | estimated up to | horizon | phi | error (cohort) | "
                  "error (today's brackets) |", "|---|---|---|---|---|---|"]
        for m in ("earn_ftyr", "hh_income", "own", "fin_assets"):
            for cut in (2014, 2016):
                for b in backtest(table, m, cut):
                    lines.append(f"| {m} | {cut} | {b['h']} | {b['phi']:.2f} | "
                                 f"{b['rmse_cohort']:.3f} | {b['rmse_brackets']:.3f} |")
    return "\n".join(lines)


def main():
    from .data import DataStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--country", default="USA")
    ap.add_argument("--out")
    a = ap.parse_args()
    ds = DataStore()
    cd = ds.country(a.country)
    t = ds.cohort[ds.cohort["iso3"] == a.country]
    txt = report(cd, t)
    if a.out:
        open(a.out, "w").write(txt)
    print(txt)


if __name__ == "__main__":
    main()
