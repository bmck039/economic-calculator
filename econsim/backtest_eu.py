"""Backtest against published wealth-survey tables (euro area: ECB HFCS; check: US SCF).

Europe's wealth survey (HFCS, waves 2010, 2014, 2017, 2021) publishes, per country and
10-year age band of the household's reference person, median net wealth, the share owning
their home, median home value (owners), financial assets and debt (holders), and gross
income - but no household records. So each backtest cell starts a small synthetic
population of couples where a survey found people of one age (30, 40 or 50) in one wave:

  * five income levels (deciles 1, 3, 5, 7, 9 of a lognormal with sigma 0.6) around the
    median household income of that age;
  * at each, owners and renters: the share owning rises with income (logit slope 1 per
    log income) and averages the survey's share; owners hold the median home value and the
    median debt (scaled by the share in debt), both rising with income^0.5; financial
    assets follow income and tenure (the survey's owner / renter ratio);
  * financial assets scaled so the population's median net wealth is the survey's;
  * not every renter wants to buy, nor now: renters start trying to buy (then buy as soon
    as they can afford it) at the ages at which the same survey's older households own
    more - so many that the cohort would reach the ownership of that survey's 60-year-
    olds - and the others rent for good (start-wave data only, nothing from the later
    waves the cell is compared with); where savings alone cannot bring the median down
    to the survey's, owners owe more (up to 125% of the home's value).

Those couples then live through what actually happened in their country (econsim.history:
prices, wages, home prices, rents, rates, returns, unemployment), earning what their
cohort's households earned in the later waves (the survey's median income at the age they
had reached), with each couple's distance from the median fading as ranks do. They are
compared with the SAME birth years in each later wave: median net wealth (in years of the
cohort's median income) and the share owning. Never with another generation.

Two simplifications - medians of all households (singles too) stand for couples, and
households are rebuilt from medians - are checked by running the same method on the US
(scf_tables.csv: the SCF in the HFCS layout), where the household-level backtest
(econsim.backtest) gives the answer the table method should reproduce.

    python -m econsim.backtest_eu [--countries DEU ESP ...] [--us] [--out cells.csv]
"""
from __future__ import annotations

import argparse
import warnings
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .career import RANK_PERSISTENCE
from .engine import simulate
from .history import from_table
from .model import Earner, Plan

CENTRES = {"16-34": 29.0, "35-44": 39.5, "45-54": 49.5, "55-64": 59.5, "65-74": 69.5,
           "75+": 80.0}                  # typical age of reference persons in each band
START_AGES = (30, 40, 50)
Z = np.array([-1.2816, -0.5244, 0.0, 0.5244, 1.2816])   # deciles 1, 3, 5, 7, 9
SIGMA = 0.6                               # spread of household income within an age
OWN_SLOPE = 1.0                           # logit of owning per log income
SPLIT = 0.6
MIN_H = 2                                 # years between waves to compare
BUY_STEPS = (2, 5, 9, 14, 20, 30)         # years ahead when renters may first try to buy


def _wmed(x, w) -> float:
    """Weighted median, interpolated between households (a few dozen synthetic ones stand
    for a whole age group)."""
    x, w = np.asarray(x, float), np.asarray(w, float)
    o = np.argsort(x)
    x, w = x[o], w[o]
    c = np.cumsum(w) - w / 2
    return float(np.interp(w.sum() / 2, c, x)) if len(x) else float("nan")


class Tables:
    """One country's survey tables: statistic by age of the reference person, by wave."""

    def __init__(self, t: pd.DataFrame, iso3: str):
        t = t[t["iso3"] == iso3]
        self.iso3 = iso3
        ref = t[t["table"] == "REF"].pivot_table(index="wave", columns="indicator",
                                                 values="value")
        self.field = ref["fieldwork_mid"].to_dict() if "fieldwork_mid" in ref else {}
        self.inc_year = ref["income_year_mid"].to_dict() if "income_year_mid" in ref else {}
        a = t[(t["group"] == "Age of RP") & t["row"].isin(CENTRES)]
        self.age = {k: g.assign(c=g["row"].map(CENTRES)).sort_values("c")
                    for k, g in a.groupby(["table", "wave"])}
        c4 = t[(t["table"] == "C4") & (t["group"].isin(["Housing status", "Total population"]))]
        self.c4 = {w: g.set_index("row")["value"].to_dict() for w, g in c4.groupby("wave")}
        self.waves = sorted(w for w in self.field if ("A3", w) in self.age)

    def at(self, table: str, wave: int, age: float, log: bool = True) -> float:
        g = self.age.get((table, wave))
        if g is None:
            return float("nan")
        g = g.dropna(subset=["value"])
        g = g[g["value"] > 0] if log else g
        if len(g) < 2:
            return float("nan")
        x, y = g["c"].to_numpy(float), g["value"].to_numpy(float)
        if log:
            return float(np.exp(np.interp(age, x, np.log(y))))
        return float(np.interp(age, x, y))

    def tenure_ratio(self, wave: int) -> tuple[float, float]:
        """Financial assets of owners and of renters vs all households (holders)."""
        d = self.c4.get(wave, {})
        al = d.get("All")
        ow = [d.get(k) for k in ("Owners - outright", "Owners - with mortgage")
              if d.get(k) == d.get(k) and d.get(k) is not None]
        rt = d.get("Renters / other")
        if not al or not ow or rt is None or rt != rt:
            return 1.3, 0.4
        return float(np.mean(ow) / al), float(rt / al)


def income_path(T: Tables, w0: int, a0: float, y0: int, years: int) -> np.ndarray:
    """The cohort's median household income (nominal, thousands) in calendar years
    y0 .. y0+years-1: the survey's median income at the age the cohort had reached in each
    wave from w0 on, log-linear between the years incomes refer to."""
    f0 = T.field[w0]
    pts = []
    for w in T.waves:
        if w < w0:
            continue
        v = T.at("I1", w, a0 + (T.field[w] - f0))
        if v == v and v > 0:
            pts.append((T.inc_year.get(w, T.field[w] - 1), np.log(v)))
    if not pts:
        return np.full(years, np.nan)
    x = np.array([p[0] for p in pts])
    y = np.array([p[1] for p in pts])
    ys = np.arange(y0, y0 + years) + 0.5
    if len(pts) == 1:
        return np.exp(np.full(years, y[0]))
    out = np.interp(ys, x, y)
    lo, hi = ys < x[0], ys > x[-1]           # beyond the surveys: the nearest trend
    out[lo] = y[0] + (ys[lo] - x[0]) * (y[1] - y[0]) / (x[1] - x[0])
    out[hi] = y[-1] + (ys[hi] - x[-1]) * (y[-1] - y[-2]) / (x[-1] - x[-2])
    return np.exp(out)


def population(T: Tables, w0: int, a0: float) -> Optional[pd.DataFrame]:
    """Synthetic couples where the survey found households of age a0 in wave w0."""
    k = 1000.0
    nw = T.at("A3", w0, a0) * k
    inc = T.at("I1", w0, a0) * k
    own = T.at("B3", w0, a0, log=False) / 100
    home = T.at("B5", w0, a0) * k
    fin = T.at("C4", w0, a0) * k
    debt_share = T.at("E5", w0, a0, log=False) / 100
    debt = T.at("E6", w0, a0) * k
    if not all(v == v for v in (nw, inc, own, fin)):
        return None
    if own > 0 and home != home:
        return None
    debt = 0.0 if debt != debt or debt_share != debt_share else debt
    mort_k = min(1.0, debt_share / own) if own > 0 else 0.0
    r_own, r_rent = T.tenure_ratio(w0)
    # renters who will buy, and when: the start wave's ownership by age (a0 -> 60) says
    # how many more own at each later age; the rest rent for good
    steps = [a0 + x for x in BUY_STEPS if a0 + x <= 60]
    prof = [own] + [max(T.at("B3", w0, b, log=False) / 100, 0) for b in steps]
    prof = np.maximum.accumulate(np.nan_to_num(prof, nan=own))
    gains = np.diff(prof) / (1 - own) if own < 1 else np.zeros(len(steps))
    buy_plan = [(int(round(b)), g) for b, g in zip(steps, gains) if g > 1e-6]
    never = max(1 - sum(g for _, g in buy_plan), 0.0)
    K = np.exp(SIGMA * Z)
    # P(own | income): logistic in log income, averaging the survey's share
    lo, hi = -20.0, 20.0
    for _ in range(60):
        a = (lo + hi) / 2
        p = 1 / (1 + np.exp(-(a + OWN_SLOPE * np.log(K))))
        lo, hi = (a, hi) if p.mean() < own else (lo, a)
    rows = []
    for kk, pk in zip(K, p):
        kinds = [(True, int(round(a0)), pk)] + \
            [(False, b, (1 - pk) * g) for b, g in buy_plan] + [(False, None, (1 - pk) * never)]
        for owner, buy_age, wt in kinds:
            if wt <= 1e-9:
                continue
            V = home * kk ** 0.5 if owner else 0.0
            M = debt * kk ** 0.5 * mort_k if owner else 0.0
            F = fin * kk * (r_own if owner else r_rent)
            rows.append({"k": kk, "own": owner, "buy_age": buy_age, "w": wt / len(K),
                         "home": V, "mort": min(M, 0.9 * V), "mort_cap": 1.25 * V,
                         "fin0": F})
    d = pd.DataFrame(rows)

    def med(s, m=1.0):
        mort = np.minimum(d["mort"] * m, np.maximum(d["mort_cap"], d["mort"]))
        return _wmed(d["fin0"] * s + d["home"] - mort, d["w"])
    lo, hi = 0.02, 50.0                       # scale financial assets: the median matches
    for _ in range(60):
        s = np.sqrt(lo * hi)
        lo, hi = (s, hi) if med(s) < nw else (lo, s)
    m = 1.0
    if med(s) > nw * 1.05 + 1000:             # still too rich: owners owe more (up to
        lo, hi = 1.0, 4.0                     # 125% of the home's value)
        for _ in range(40):
            m = (lo + hi) / 2
            lo, hi = (m, hi) if med(s, m) > nw else (lo, m)
    d["mort"] = np.minimum(d["mort"] * m, np.maximum(d["mort_cap"], d["mort"]))
    d["fin"] = d["fin0"] * s
    d.attrs.update(nw=nw, inc=inc, own=own, never=never, start_nw_fit=med(s, m))
    return d


def cells_for(T: Tables, data_year: int, plan_kw: Optional[dict] = None) -> list[dict]:
    jobs = []
    for w0 in T.waves:
        later = [w for w in T.waves if w > w0 and T.field[w] - T.field[w0] >= MIN_H]
        if not later:
            continue
        y0 = int(np.floor(T.field[w0] + 0.5))
        years = int(np.floor(T.field[later[-1]] - T.field[w0] + 0.5)) + 1
        for a0 in START_AGES:
            pop = population(T, w0, a0)
            if pop is None:
                continue
            path = income_path(T, w0, a0, y0, years) * 1000
            if not np.isfinite(path).all():
                continue
            for i, r in enumerate(pop.itertuples(index=False)):
                pp = path * np.exp(np.log(r.k) * RANK_PERSISTENCE ** np.arange(years))
                jobs.append({"iso3": T.iso3, "w0": w0, "a0": a0, "y0": y0, "years": years,
                             "hh": i, "w": r.w, "own": r.own, "buy_age": r.buy_age,
                             "home": r.home,
                             "mort": r.mort, "fin": r.fin, "pay": tuple(pp),
                             "data_year": data_year, "kw": plan_kw or {}})
    return jobs


# ------------------------------------------------------------ simulation (workers)
_DS = None


def _init(data_dir):
    global _DS
    from .data import DataStore
    warnings.simplefilter("ignore")
    _DS = DataStore(data_dir)
    _DS._cd, _DS._H = {}, {}
    _DS._eu_h = pd.read_csv(Path(data_dir) / "eu_history.csv") \
        if (Path(data_dir) / "eu_history.csv").exists() else pd.DataFrame()
    _DS._us_h = pd.read_csv(Path(data_dir) / "us_history.csv")


def _one(j):
    iso = j["iso3"]
    if iso not in _DS._cd:
        _DS._cd[iso] = _DS.country(iso)
    cd = _DS._cd[iso]
    key = (iso, j["y0"])
    if key not in _DS._H:
        _DS._H[key] = from_table(_DS._us_h, j["y0"], cd.data_year) if iso == "USA" else \
            from_table(_DS._eu_h, j["y0"], cd.data_year, iso3=iso)
    a = int(round(j["a0"]))
    e = [Earner(a, pay_path=tuple(x * s for x in j["pay"])) for s in (SPLIT, 1 - SPLIT)]
    p = Plan(iso, (e[0], e[1]), start_year=j["y0"], horizon_age=a + j["years"] - 1,
             marriage_age=a, buy_house_age=a if j["own"] else
             (None if j["buy_age"] != j["buy_age"] or j["buy_age"] is None
              else int(j["buy_age"])), own_home=j["own"],
             home_value_now=j["home"] if j["own"] else None,
             mortgage_now=j["mort"] if j["own"] else None,
             starting_savings=j["fin"], starting_retirement=0.0, career_model=False,
             cohort_effects="off", typical_student_debt=False,
             **({} if iso == "USA" else {"us_citizens": False}), **j.get("kw", {}))
    try:
        y = simulate(p, cd, _DS.us_params() if iso == "USA" else None,
                     history=_DS._H[key]).years
    except Exception:   # noqa: BLE001 - a failed household is left out
        return []
    # HFCS net wealth leaves out occupational pensions; the SCF's includes retirement
    # accounts: the model's retirement account counts only in the US
    ret = y["retirement_account"] if iso == "USA" else 0.0
    nw = y["liquid"] + ret + y["home_value"] - y["mortgage_balance"] - \
        y.get("student_loan_balance", 0.0)
    return [{"iso3": iso, "w0": j["w0"], "a0": j["a0"], "hh": j["hh"], "w": j["w"],
             "t": int(t), "nw": float(nw.iloc[t]), "owned": bool(y["owned"].iloc[t])}
            for t in range(len(y))]


def run(ds, tables: pd.DataFrame, countries, workers: int = 16,
        **plan_kw) -> tuple[pd.DataFrame, dict]:
    Ts = {iso: Tables(tables, iso) for iso in countries}
    jobs = []
    for iso, T in Ts.items():
        try:
            dy = ds.country(iso).data_year
        except Exception:   # noqa: BLE001 - not a simulated country
            continue
        jobs += cells_for(T, dy, plan_kw)
    if workers and workers > 1:
        with ProcessPoolExecutor(workers, initializer=_init, initargs=(str(ds.dir),)) as ex:
            res = list(ex.map(_one, jobs, chunksize=4))
    else:
        _init(str(ds.dir))
        res = [_one(j) for j in jobs]
    return pd.DataFrame([x for r in res for x in r]), Ts


def compare(sims: pd.DataFrame, Ts: dict) -> pd.DataFrame:
    out = []
    for (iso, w0, a0), g in sims.groupby(["iso3", "w0", "a0"]):
        T = Ts[iso]
        f0 = T.field[w0]
        g0 = g[g["t"] == 0]
        for w1 in T.waves:
            if w1 <= w0 or T.field[w1] - f0 < MIN_H:
                continue
            dt = T.field[w1] - f0
            t = int(np.floor(dt + 0.5))
            gm = g[g["t"] == t]
            if len(gm) < 4:
                continue
            age = a0 + dt
            nw_s = T.at("A3", w1, age) * 1000
            inc_s = T.at("I1", w1, age) * 1000
            own_s = T.at("B3", w1, age, log=False) / 100
            if not (nw_s == nw_s and inc_s == inc_s):
                continue
            out.append({"iso3": iso, "w0": w0, "w1": w1, "h": round(dt, 1), "a0": a0,
                        "age": round(age, 1),
                        "start_model": _wmed(g0["nw"], g0["w"]),
                        "start_survey": T.at("A3", w0, a0) * 1000,
                        "wealth_model": _wmed(gm["nw"], gm["w"]), "wealth_survey": nw_s,
                        "income": inc_s,
                        "own_model": float(np.average(gm["owned"], weights=gm["w"])),
                        "own_survey": own_s})
    c = pd.DataFrame(out)
    if not c.empty:
        c["wealth_err"] = (c["wealth_model"] - c["wealth_survey"]) / c["income"]
        c["own_err"] = c["own_model"] - c["own_survey"]
    return c


def score(c: pd.DataFrame) -> dict:
    return {"cells": len(c), "countries": int(c["iso3"].nunique()),
            "wealth_bias": float(c["wealth_err"].mean()),
            "wealth_mae": float(c["wealth_err"].abs().mean()),
            "own_bias_pp": float(100 * c["own_err"].mean()),
            "own_mae_pp": float(100 * c["own_err"].abs().mean())}


def load_tables(ds, us: bool = False) -> pd.DataFrame:
    p = ds.dir / ("scf_tables.csv" if us else "hfcs_tables.csv")
    if not p.exists() and not us:
        p = Path(__file__).resolve().parents[1] / "reference" / "hfcs_tables.csv"
    return pd.read_csv(p)


def main():
    from .data import DataStore
    ap = argparse.ArgumentParser()
    ap.add_argument("--countries", nargs="*")
    ap.add_argument("--us", action="store_true", help="the same method on the SCF tables")
    ap.add_argument("--out")
    ap.add_argument("--workers", type=int, default=16)
    a = ap.parse_args()
    ds = DataStore()
    t = load_tables(ds, a.us)
    isos = ["USA"] if a.us else (a.countries or sorted(set(t["iso3"]) - {"EA"}))
    sims, Ts = run(ds, t, isos, a.workers)
    c = compare(sims, Ts)
    print(score(c))
    pd.set_option("display.width", 200)
    print(c.groupby("a0")[["wealth_err", "own_err"]].mean().round(3))
    print(c.groupby("iso3")[["wealth_err", "own_err"]].mean().round(3).to_string())
    if a.out:
        c.to_csv(a.out, index=False)


if __name__ == "__main__":
    main()
